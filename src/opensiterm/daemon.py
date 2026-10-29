"""Background process that owns one serial port.

`opensiterm open` starts it detached. Clients talk to it over a
token-protected TCP socket on 127.0.0.1: one JSON request line in, one JSON
response line out, per connection.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import signal
import socketserver
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import serial

from . import __version__, paths, ports, render
from .profiles import EOLS, profile_settings
from .rules import RuleSet
from .timeline import RX, Chunk, Timeline
from .util import CRCS, crc_bytes, hexdump, parse_escapes, parse_hex

SETTING_KEYS = ("eol", "prompt", "done", "timeout", "idle", "display", "input",
                "split_lines", "frame_gap", "crc", "encoding", "char_delay")
SERIAL_KEYS = ("baud", "bytesize", "parity", "stopbits", "rtscts", "xonxoff")
PROMPT_SETTLE = 0.15   # device must be quiet this long before a prompt counts
QUIET_FALLBACK = 2.0   # with a prompt/terminator configured, give up waiting after this much silence


class OpError(Exception):
    def __init__(self, message: str, code: str = "error", **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def validate_settings(s: dict) -> None:
    if s.get("eol") not in EOLS:
        raise OpError(f"eol must be one of {', '.join(EOLS)}", "usage")
    if s.get("display") not in ("text", "hex") or s.get("input") not in ("text", "hex"):
        raise OpError("display and input must be 'text' or 'hex'", "usage")
    if s.get("crc") not in (None, "none", *CRCS):
        raise OpError(f"crc must be one of none, {', '.join(CRCS)}", "usage")
    for key in ("prompt", "done"):
        if s.get(key):
            try:
                re.compile(s[key])
            except re.error as e:
                raise OpError(f"invalid {key} regex {s[key]!r}: {e}", "usage") from None
    try:
        "".encode(s.get("encoding") or "utf-8")
    except LookupError:
        raise OpError(f"unknown encoding {s.get('encoding')!r}", "usage") from None


def open_error_hint(exc: Exception, device: str) -> str:
    msg = str(exc)
    low = msg.lower()
    if os.name == "nt" and ("permissionerror" in low or "access is denied" in low or "拒绝访问" in msg):
        return (f"{device} is in use by another program (serial terminal, IDE serial monitor, "
                f"flasher, or another opensiterm session). Close it and retry.")
    if "permission denied" in low or "errno 13" in low:
        return (f"no permission for {device}: add your user to the 'dialout' group (Debian/Ubuntu/"
                f"Raspberry Pi OS) or 'uucp' (Arch/Fedora), then log out and back in.")
    if "busy" in low or "errno 16" in low or "lock" in low:
        return f"{device} is busy: another program has it open."
    if ("filenotfounderror" in low or "no such file" in low or "cannot find" in low
            or "找不到" in msg or "does not exist" in low):
        return f"{device} not found. Run `opensiterm ports` to list ports (USB devices may have re-enumerated)."
    return ""


def _stopbits(v) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


class Session:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.settings = dict(cfg["settings"])
        validate_settings(self.settings)
        self.project_dir = Path(cfg["project_dir"])
        self.logs_dir = paths.logs_dir(self.project_dir, cfg.get("log_dir"))
        self.log_path = self.logs_dir / f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self.audit_path = self.logs_dir / "audit.jsonl"
        self._logf = open(self.log_path, "a", encoding="utf-8", newline="\n", buffering=1)
        self.timeline = Timeline(max_bytes=int(cfg.get("buffer_bytes") or 8 << 20),
                                 split_lines=self.settings["split_lines"],
                                 frame_gap=self.settings["frame_gap"],
                                 on_close=self._log_entry)
        self.rules = RuleSet(self.project_dir)
        self.token = secrets.token_hex(16)
        self.ser = None
        self.device: Optional[str] = None
        self.state = "starting"
        self.paused = False
        self.reconnect = cfg.get("reconnect", True)
        self.dtr = cfg.get("dtr")
        self.rts = cfg.get("rts")
        self.port_lock = threading.RLock()
        self.stopping = threading.Event()
        self.cursors: dict = {}
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.started = time.time()
        self.server: Optional[socketserver.ThreadingTCPServer] = None
        self.info: dict = {}
        self.session_file = paths.sessions_dir() / f"{self.name}.json"
        self._waiting_logged = False

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        try:
            self._open_port()
        except Exception as e:
            hint = open_error_hint(e, self.cfg["serial"]["port"])
            self._logf.close()
            try:
                if self.log_path.stat().st_size == 0:
                    self.log_path.unlink()
            except OSError:
                pass
            raise RuntimeError(f"cannot open {self.cfg['serial']['port']}: {e}" + (f"\nhint: {hint}" if hint else "")) from None
        s = self.cfg["serial"]
        self.timeline.add_event(
            f"opened {self.device} @ {s['baud']} {s['bytesize']}{s['parity']}{_stopbits(s['stopbits'])} "
            f"(profile {self.settings['profile']}, opensiterm {__version__})")
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.session = self
        self.info = {
            "name": self.name, "port": self.cfg["serial"]["port"], "device": self.device,
            "match": self.cfg["serial"].get("match"), "pid": os.getpid(),
            "tcp_port": self.server.server_address[1], "token": self.token,
            "log": str(self.log_path), "project_dir": str(self.project_dir),
            "started": self.started, "version": __version__,
        }
        tmp = self.session_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.info, indent=1), encoding="utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.session_file)
        threading.Thread(target=self._reader, name="serial-reader", daemon=True).start()
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=self.shutdown, daemon=True).start())
            except ValueError:
                pass

    def serve(self) -> None:
        self.server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.timeline.add_event("session closed")
        time.sleep(0.1)  # let the reader thread leave read()
        self._close_port()
        self.timeline.flush()
        try:
            info = json.loads(self.session_file.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                self.session_file.unlink()
        except (OSError, ValueError):
            pass
        try:
            self._logf.close()
        except OSError:
            pass
        if self.server is not None:
            self.server.shutdown()

    # --- serial port ---------------------------------------------------------

    def _resolve_device(self) -> str:
        s = self.cfg["serial"]
        if s.get("match"):
            return ports.find_port(s["match"])
        return s["port"]

    def _open_port(self) -> None:
        dev = self._resolve_device()
        s = self.cfg["serial"]
        ser = serial.serial_for_url(dev, do_not_open=True)
        ser.baudrate = s["baud"]
        ser.bytesize = s["bytesize"]
        ser.parity = s["parity"]
        ser.stopbits = s["stopbits"]
        ser.xonxoff = s["xonxoff"]
        ser.rtscts = s["rtscts"]
        ser.timeout = 0.05
        ser.write_timeout = 10
        if self.dtr is not None:
            ser.dtr = self.dtr
        if self.rts is not None:
            ser.rts = self.rts
        if os.name == "posix" and "://" not in dev:
            try:
                ser.exclusive = True
            except Exception:
                pass
        ser.open()
        with self.port_lock:
            self.ser = ser
            self.device = dev
            self.state = "open"

    def _close_port(self) -> None:
        with self.port_lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def _reader(self) -> None:
        while not self.stopping.is_set():
            if self.paused:
                if self.ser is not None:
                    self._close_port()
                self.state = "paused"
                self.stopping.wait(0.05)
                continue
            if self.ser is None:
                if self.state == "disconnected":
                    self.stopping.wait(0.5)
                    continue
                try:
                    self._open_port()
                    self.timeline.add_event(f"port {self.device} reopened")
                    self._waiting_logged = False
                except Exception as e:
                    self.state = "reconnecting"
                    if not self._waiting_logged:
                        self.timeline.add_event(f"waiting for {self.cfg['serial']['port']} to come back ({e})")
                        self._waiting_logged = True
                    self.stopping.wait(0.5)
                continue
            ser = self.ser
            try:
                n = ser.in_waiting
                data = ser.read(n if n > 0 else 1)
            except Exception as e:
                if self.stopping.is_set() or self.paused:
                    continue
                self._close_port()
                if self.reconnect:
                    self.state = "reconnecting"
                    self.timeline.add_event(f"port lost ({e}); reconnecting")
                else:
                    self.state = "disconnected"
                    self.timeline.add_event(f"port lost ({e}); auto-reconnect is off, use `opensiterm resume`")
                continue
            if data:
                self.rx_bytes += len(data)
                self.timeline.add_rx(data)
            else:
                self.timeline.tick()

    def _require_port(self):
        if self.ser is None:
            raise OpError(f"port is {self.state}; cannot do that now"
                          + (" (run `opensiterm resume`)" if self.state in ("paused", "disconnected") else ""),
                          "not_open")
        return self.ser

    # --- logging -------------------------------------------------------------

    def _log_entry(self, e) -> None:
        chunk = Chunk(e.ts, e.kind, bytes(e.data), e.source, False, False)
        for line in self._render([chunk]):
            self._logf.write(render.format_line(line, ts=True, full_date=True) + "\n")

    def _audit(self, action: str, source: str, text: str, **extra) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session": self.name, "device": self.device,
               "action": action, "source": source, "text": text}
        rec.update({k: v for k, v in extra.items() if v is not None})
        try:
            with open(self.audit_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # --- helpers -------------------------------------------------------------

    def _utf8_safe(self, hex=None) -> bool:
        """Hold back a trailing partial UTF-8 character? Only when showing UTF-8 text."""
        if hex or (hex is None and self.settings["display"] == "hex"):
            return False
        return self.settings["encoding"].lower().replace("-", "").replace("_", "") == "utf8"

    def _render(self, chunks, hex=None, raw=False, max_lines=None, max_chars=None):
        display = "hex" if hex else "text" if hex is False else self.settings["display"]
        lines = render.render_chunks(chunks, encoding=self.settings["encoding"], display=display,
                                     keep_ansi=raw, crc=self.settings["crc"] if self.settings["crc"] != "none" else None)
        return render.limit_lines(lines, max_lines, max_chars, str(self.log_path))

    def _rx_lines(self, start: int, hex=None):
        chunks, _, _ = self.timeline.snapshot(start)
        return self._render([c for c in chunks if c.kind == RX], hex)

    def _cursor(self, reader: str) -> int:
        return self.cursors.get(reader, self.timeline.first_pos)

    def _count_rx_lines(self, a: int, b: int) -> int:
        return sum(1 for e in self.timeline.entries if e.kind == RX and a <= e.pos < b)

    def _wait_quiet(self, pos: int, wait: float, idle: float) -> None:
        """Wait (lock held) for data after pos, then until the line goes quiet for `idle`."""
        tl = self.timeline
        deadline = time.monotonic() + wait
        while tl.end_pos <= pos:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return
            tl.cond.wait(rem)
        last, quiet_since = tl.end_pos, time.monotonic()
        while True:
            now = time.monotonic()
            if now - quiet_since >= idle or now >= deadline:
                return
            tl.cond.wait(min(idle - (now - quiet_since), deadline - now))
            if tl.end_pos != last:
                last, quiet_since = tl.end_pos, time.monotonic()

    def _wait_reply(self, start: int, expect: Optional[str], done: Optional[str], prompt: Optional[str],
                    timeout: float, idle: float, cmd_text: str):
        """Wait (lock held) for the reply to a command. Returns (reason, matched_text)."""
        tl = self.timeline
        exp = re.compile(expect or done, re.M) if (expect or done) else None
        pr = re.compile(prompt) if prompt and not expect else None
        quiet_limit = None if expect else (max(idle, QUIET_FALLBACK) if (exp or pr) else idle)
        deadline = time.monotonic() + timeout
        seen = start
        last_change = None
        exp_checked = prompt_checked = -1
        while True:
            now = time.monotonic()
            if tl.end_pos != seen:
                seen, last_change = tl.end_pos, now
            if last_change is not None:
                quiet = now - last_change
                if exp is not None and exp_checked != seen:
                    exp_checked = seen
                    m = exp.search("\n".join(line["text"] for line in self._rx_lines(start)))
                    if m:
                        return "expect" if expect else "done", m.group(0)
                if pr is not None and prompt_checked != seen and quiet >= PROMPT_SETTLE:
                    prompt_checked = seen
                    lines = self._rx_lines(start)
                    if lines and lines[-1]["partial"]:
                        last = lines[-1]["text"].strip()
                        echoing = bool(last) and bool(cmd_text) and cmd_text.startswith(last)
                        if pr.search(lines[-1]["text"]) and not echoing:
                            return "prompt", lines[-1]["text"]
                if quiet_limit is not None and quiet >= quiet_limit:
                    return ("idle" if not (exp or pr) else "quiet"), None
            if now >= deadline:
                return "timeout", None
            step = deadline - now
            if last_change is not None:
                quiet = now - last_change
                if pr is not None and prompt_checked != seen:
                    step = min(step, PROMPT_SETTLE - quiet)
                if quiet_limit is not None:
                    step = min(step, quiet_limit - quiet)
            tl.cond.wait(min(max(step, 0.005), 0.5))

    # --- operations (called over RPC as op_<name>) -------------------------------

    def op_ping(self):
        return {"name": self.name, "version": __version__, "pid": os.getpid()}

    def op_status(self, reader: str = "default"):
        s = self.cfg["serial"]
        with self.timeline.cond:
            unread = self.timeline.end_pos - self._cursor(reader)
        return {
            "name": self.name, "port": s["port"], "device": self.device, "match": s.get("match"),
            "baud": s["baud"], "format": f"{s['bytesize']}{s['parity']}{_stopbits(s['stopbits'])}",
            "rtscts": s["rtscts"], "xonxoff": s["xonxoff"], "dtr": self.dtr, "rts": self.rts,
            "state": self.state, "settings": self.settings, "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes, "unread_bytes": unread, "log": str(self.log_path),
            "audit": str(self.audit_path), "project_dir": str(self.project_dir), "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started, 1), "version": __version__,
            "rules_error": self.rules.error,
        }

    def op_read(self, reader="default", since=None, wait=0.0, idle=0.3, discard=False,
                max_lines=200, max_chars=2000, hex=None, raw=False):
        tl = self.timeline
        with tl.cond:
            pos = int(since) if since is not None else self._cursor(reader)
            if discard:
                self.cursors[reader] = tl.end_pos
                return {"lines": [], "cursor": tl.end_pos, "discarded_bytes": max(0, tl.end_pos - pos),
                        "state": self.state}
            if wait and float(wait) > 0:
                self._wait_quiet(pos, float(wait), float(idle))
            chunks, new_pos, dropped = tl.snapshot(pos, self._utf8_safe(hex))
            self.cursors[reader] = new_pos
        lines = self._render(chunks, hex, raw, max_lines, max_chars)
        return {"lines": lines, "cursor": new_pos, "dropped_bytes": dropped, "state": self.state}

    def op_tail(self, lines=50, grep=None, hex=None, raw=False, max_chars=2000):
        try:
            rx = re.compile(grep) if grep else None
        except re.error as e:
            raise OpError(f"invalid regex {grep!r}: {e}", "usage") from None
        out = []
        for e in reversed(self.timeline.all_entries()):
            chunk = Chunk(e.ts, e.kind, bytes(e.data), e.source, False, not e.closed)
            for line in reversed(self._render([chunk], hex, raw)):
                if rx is None or rx.search(line["text"]):
                    out.append(line)
            if len(out) >= lines:
                break
        out = out[:lines]
        out.reverse()
        return {"lines": render.limit_lines(out, None, max_chars), "state": self.state}

    def op_send(self, text=None, hex=None, escapes=False, eol=None, crc=None, source="ai",
                unsafe=False, reason=None, force=False, wait=True, expect=None, timeout=None,
                idle=None, reader="default", char_delay=None, max_lines=200, max_chars=2000,
                display_hex=None, raw=False):
        st = self.settings
        enc = st["encoding"]
        if source not in ("ai", "human"):
            raise OpError("source must be 'ai' or 'human'", "usage")
        try:
            if hex is not None:
                body, binary = parse_hex(hex), True
            elif text is not None:
                if st["input"] == "hex":
                    body, binary = parse_hex(text), True
                else:
                    body, binary = (parse_escapes(text, enc) if escapes else text.encode(enc)), False
            else:
                raise OpError("nothing to send: give text or hex", "usage")
        except ValueError as e:
            raise OpError(str(e), "usage") from None
        crc_name = crc if crc is not None else st["crc"]
        if crc_name and crc_name != "none":
            try:
                body += crc_bytes(body, crc_name)
            except ValueError as e:
                raise OpError(str(e), "usage") from None
        eol_name = eol if eol is not None else (None if binary else st["eol"])
        if eol_name is not None and eol_name not in EOLS:
            raise OpError(f"eol must be one of {', '.join(EOLS)}", "usage")
        payload = body + (EOLS[eol_name] if eol_name else b"")
        if not payload:
            raise OpError("refusing to send an empty payload (use --eol to send just a line ending)", "usage")

        shown = body.decode(enc, "replace") if not binary else hexdump(body)
        if unsafe:
            if not (reason and str(reason).strip()):
                raise OpError("unsafe-send requires a --reason explaining why the user wants this", "usage")
            source = "unsafe"
        rule = self.rules.check(shown) if shown.strip() else None
        if rule is not None:
            if source == "human":
                if not force:
                    raise OpError(f"matches safety rule '{rule.name}': {rule.reason}", "confirm_human",
                                  rule=rule.to_dict())
            elif rule.action == "deny" or source == "ai":
                self._audit("blocked", source, shown, rule=rule.name, reason=reason)
                self.timeline.add_event(f"blocked by rule '{rule.name}': {shown.strip()[:200]}")
                if rule.action == "deny":
                    msg = (f"BLOCKED by safety rule '{rule.name}' ({rule.reason}). This rule is 'deny': "
                           f"an AI agent may not send it. Ask the user to send it themselves from "
                           f"`opensiterm watch`.")
                else:
                    msg = (f"BLOCKED by safety rule '{rule.name}' ({rule.reason}). Do not work around this. "
                           f"Only if the user explicitly asked for this exact command, re-send it with "
                           f"`opensiterm unsafe-send --reason \"<why the user wants it>\" ...` "
                           f"(MCP: serial_unsafe_send); the user will be asked to approve it.")
                raise OpError(msg, "blocked", rule=rule.to_dict())

        tl = self.timeline
        with tl.cond:
            unread = self._count_rx_lines(self._cursor(reader), tl.end_pos)
        with self.port_lock:
            self._require_port()
            tx = tl.add_tx(payload, source)
            try:
                self._write(payload, char_delay if char_delay is not None else st["char_delay"])
            except Exception as e:
                tl.add_event(f"write failed: {e}")
                raise OpError(f"write failed: {e}", "error") from None
        self.tx_bytes += len(payload)
        self._audit("sent", source, shown, rule=rule.name if rule else None, reason=reason,
                    hex=hexdump(payload) if binary else None)

        done, matched = "nowait", None
        to = float(timeout if timeout is not None else st["timeout"])
        with tl.cond:
            if wait:
                done, matched = self._wait_reply(
                    tx.end, expect, None if expect else st["done"], None if expect else st["prompt"],
                    to, float(idle if idle is not None else st["idle"]), shown.strip())
            if display_hex is None and binary:
                display_hex = True  # a binary request almost always gets a binary reply
            chunks, new_pos, _ = tl.snapshot(tx.pos, self._utf8_safe(display_hex))
            if wait:
                # The caller saw the reply; anything unread before the command is reported as skipped.
                self.cursors[reader] = new_pos
            else:
                # Fire-and-forget: leave the cursor alone so the next read shows the replies.
                chunks, unread = [c for c in chunks if c.kind != RX], 0
        lines = self._render(chunks, display_hex, raw, max_lines, max_chars)
        return {"lines": lines, "cursor": new_pos, "done": done, "matched": matched, "timeout": to,
                "explicit_expect": expect is not None, "unread_skipped_lines": unread,
                "rule": rule.name if rule else None}

    def _write(self, payload: bytes, char_delay: float) -> None:
        with self.port_lock:
            ser = self._require_port()
            if char_delay:
                for b in payload:
                    ser.write(bytes((b,)))
                    ser.flush()
                    time.sleep(char_delay)
            else:
                ser.write(payload)
                ser.flush()

    def op_expect(self, pattern, timeout=10.0, reader="default", max_lines=200, max_chars=2000,
                  hex=None, raw=False):
        try:
            rx = re.compile(pattern, re.M)
        except re.error as e:
            raise OpError(f"invalid regex {pattern!r}: {e}", "usage") from None
        tl = self.timeline
        deadline = time.monotonic() + float(timeout)
        m = None
        with tl.cond:
            pos = self._cursor(reader)
            checked = -1
            while True:
                if tl.end_pos != checked:
                    checked = tl.end_pos
                    chunks, new_pos, dropped = tl.snapshot(pos, self._utf8_safe(hex))
                    text = "\n".join(line["text"] for line in self._render([c for c in chunks if c.kind == RX], hex))
                    m = rx.search(text)
                    if m:
                        break
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                tl.cond.wait(min(rem, 0.5))
            self.cursors[reader] = new_pos
        lines = self._render(chunks, hex, raw, max_lines, max_chars)
        return {"matched": m is not None, "match": m.group(0) if m else None, "lines": lines,
                "cursor": new_pos, "dropped_bytes": dropped, "timeout": float(timeout)}

    def op_control(self, dtr=None, rts=None, break_ms=None):
        with self.port_lock:
            ser = self._require_port()
            if dtr is not None:
                ser.dtr = self.dtr = bool(dtr)
            if rts is not None:
                ser.rts = self.rts = bool(rts)
            if break_ms:
                ser.send_break(float(break_ms) / 1000)
        parts = [f"{k}={'on' if v else 'off'}" for k, v in (("dtr", dtr), ("rts", rts)) if v is not None]
        if break_ms:
            parts.append(f"break {break_ms} ms")
        self.timeline.add_event("control: " + (", ".join(parts) or "no change"))
        return {"dtr": self.dtr, "rts": self.rts}

    def op_reset(self, line="rts", ms=100):
        if line not in ("rts", "dtr"):
            raise OpError("line must be 'rts' or 'dtr'", "usage")
        hold = float(ms) / 1000
        with self.port_lock:
            ser = self._require_port()
            if line == "rts":
                # ESP32-style auto-reset: RTS drives EN; keep DTR (IO0/BOOT) released for a normal boot.
                ser.dtr = False
                ser.rts = False
                time.sleep(0.02)
                ser.rts = True
                time.sleep(hold)
                ser.rts = False
            else:
                # Arduino-style: the DTR assert edge pulses RESET through a capacitor.
                ser.dtr = False
                time.sleep(0.05)
                ser.dtr = True
                time.sleep(hold)
                ser.dtr = False
            self.dtr, self.rts = ser.dtr, ser.rts
        self.timeline.add_event(f"reset: pulsed {line.upper()} for {ms} ms")
        return {"line": line, "ms": ms}

    def op_pause(self):
        self.paused = True
        deadline = time.monotonic() + 3
        while self.ser is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.ser is not None:
            raise OpError("could not release the port", "error")
        self.state = "paused"
        self.timeline.add_event("port released (paused); other programs may use it now")
        return {"state": self.state}

    def op_resume(self, timeout=10.0):
        if self.state == "disconnected":
            self.state = "reconnecting"
        self.paused = False
        self._waiting_logged = False
        deadline = time.monotonic() + float(timeout)
        while (self.ser is None or self.state != "open") and time.monotonic() < deadline:
            time.sleep(0.05)
        return {"state": self.state, "device": self.device, "resumed": self.ser is not None}

    def op_set(self, **kw):
        unknown = set(kw) - set(SETTING_KEYS) - set(SERIAL_KEYS) - {"profile"}
        if unknown:
            raise OpError(f"unknown setting(s): {', '.join(sorted(unknown))}", "usage")
        new = dict(self.settings)
        if kw.get("profile"):
            try:
                prof = profile_settings(kw["profile"])
            except ValueError as e:
                raise OpError(str(e), "usage") from None
            new.update(prof)
        new.update({k: kw[k] for k in SETTING_KEYS if kw.get(k) is not None})
        validate_settings(new)
        self.settings = new
        with self.timeline.cond:
            self.timeline.split_lines = new["split_lines"]
            self.timeline.frame_gap = new["frame_gap"]
        changed = [f"{k}={kw[k]}" for k in kw if kw[k] is not None]
        serial_changes = {k: kw[k] for k in SERIAL_KEYS if kw.get(k) is not None}
        if serial_changes:
            self.cfg["serial"].update(serial_changes)
            with self.port_lock:
                if self.ser is not None:
                    attr = {"baud": "baudrate"}
                    for k, v in serial_changes.items():
                        setattr(self.ser, attr.get(k, k), v)
        if changed:
            self.timeline.add_event("settings: " + ", ".join(changed))
        return self.op_status()

    def op_rules(self, check=None):
        self.rules.reload()
        hit = self.rules.check(check) if check else None
        return {"rules": [r.to_dict() for r in self.rules.rules], "error": self.rules.error,
                "config": str(self.rules.path), "match": hit.to_dict() if hit else None}

    def op_close(self):
        threading.Thread(target=self.shutdown, daemon=True).start()
        return {"closed": True, "log": str(self.log_path)}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        session: Session = self.server.session
        try:
            req = json.loads(self.rfile.readline(1 << 22))
            if not hmac.compare_digest(str(req.get("token", "")), session.token):
                raise OpError("bad token", "auth")
            op = str(req.get("op", ""))
            fn = getattr(session, "op_" + op, None) if op.isidentifier() else None
            if fn is None:
                raise OpError(f"unknown operation {op!r}", "usage")
            resp = {"ok": True, "result": fn(**(req.get("args") or {}))}
        except OpError as e:
            resp = {"ok": False, "error": str(e), "code": e.code, **e.extra}
        except TypeError as e:
            resp = {"ok": False, "error": f"bad arguments: {e}", "code": "usage"}
        except Exception as e:
            traceback.print_exc()
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}", "code": "error"}
        try:
            self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def _write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def run(config_path: str) -> int:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    try:
        os.remove(config_path)
    except OSError:
        pass
    ready = Path(cfg["ready_file"])
    try:
        session = Session(cfg)
        session.start()
    except Exception as e:
        traceback.print_exc()
        _write_json_atomic(ready, {"ok": False, "error": str(e)})
        return 1
    _write_json_atomic(ready, {"ok": True, "info": session.info})
    try:
        session.serve()
    finally:
        session.shutdown()
    return 0
