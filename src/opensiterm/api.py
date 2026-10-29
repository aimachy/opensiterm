"""High-level operations shared by the CLI and the MCP server.

Each function returns a Result: compact text for an LLM, structured data for
--json, and a process exit code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import __version__, client, paths, render
from . import ports as ports_mod
from . import rules as rules_mod
from .client import OpenSiTermError
from .profiles import PROFILES, profile_settings
from .util import session_name_for


@dataclass
class Result:
    text: str
    data: dict = field(default_factory=dict)
    code: int = 0


def _lines_text(lines: List[dict], ts: bool) -> str:
    return render.format_lines(lines, ts)


def _rx_count(lines: List[dict]) -> int:
    return sum(1 for line in lines if line["kind"] == "rx")


# --- ports & sessions ----------------------------------------------------------------

def list_ports() -> Result:
    found = ports_mod.list_serial_ports()
    in_use = {}
    for s in client.load_sessions():
        in_use[str(s.get("device") or s.get("port")).lower()] = s["name"]
    rows = []
    for p in found:
        desc = p["description"]
        if p["chip"] and p["chip"].lower() not in desc.lower():
            desc = f"{p['chip']} - {desc}" if desc else p["chip"]
        row = f"{p['device']:<14} {desc}"
        if p["vid"] is not None:
            row += f"  [USB {p['vid']:04X}:{p['pid']:04X}" + (f" SN={p['serial_number']}" if p["serial_number"] else "") + "]"
        p["session"] = in_use.get(p["device"].lower())
        if p["session"]:
            row += f"  (open in session '{p['session']}')"
        rows.append(row)
    if not rows:
        hint = {
            "linux": "check the cable, `dmesg | tail`, and that the driver (cdc_acm/ch341/cp210x/ftdi_sio) loaded",
            "darwin": "check the cable; CH34x adapters on older macOS need the WCH driver",
        }.get(sys.platform, "check the cable and the USB-serial driver in Device Manager")
        return Result(f"no serial ports found ({hint})", {"ports": []})
    return Result("\n".join(rows), {"ports": found})


def _project_defaults(project: Path) -> dict:
    path = paths.config_file(project)
    if not path.exists():
        return {}
    try:
        return rules_mod.load_config(path).get("defaults") or {}
    except Exception:
        return {}


def _status_line(st: dict) -> str:
    s = st["settings"]
    up = st["uptime_s"]
    uptime = f"{up:.0f}s" if up < 120 else f"{up / 60:.0f}m" if up < 7200 else f"{up / 3600:.1f}h"
    lines = [
        f"{st['name']}: {st['state']}  {st['device']} @ {st['baud']} {st['format']}  "
        f"profile={s['profile']} eol={s['eol']} display={s['display']}  "
        f"rx={st['rx_bytes']}B tx={st['tx_bytes']}B unread={st['unread_bytes']}B  up {uptime}",
        f"  log: {st['log']}",
    ]
    if st.get("rules_error"):
        lines.append(f"  WARNING safety config error (built-in rules still active): {st['rules_error']}")
    return "\n".join(lines)


def open_session(port: Optional[str] = None, *, name=None, match=None, baud=None, profile=None,
                 eol=None, encoding=None, prompt=None, done=None, bytesize=None, parity=None,
                 stopbits=None, rtscts=None, xonxoff=None, dtr=None, rts=None, char_delay=None,
                 timeout=None, idle=None, display=None, input=None, crc=None, frame_gap=None,
                 log_dir=None, project_dir=None, reconnect=True, buffer_mb=None) -> Result:
    project = paths.find_project_dir(project_dir)
    defaults = _project_defaults(project)
    port = port or defaults.get("port")
    baud = baud or defaults.get("baud")
    profile = profile or defaults.get("profile")
    if not port and not match:
        raise OpenSiTermError("no port given (and no [defaults] port in .opensiterm/config.toml). "
                              f"Available: {ports_mod.describe(ports_mod.list_serial_ports())}", "usage")
    try:
        if match:
            device = ports_mod.find_port(match)
        elif port.lower() == "auto":
            device = ports_mod.auto_port()
        else:
            device = port
    except LookupError as e:
        raise OpenSiTermError(str(e), "usage") from None

    overrides = {k: v for k, v in dict(
        eol=eol, encoding=encoding, prompt=prompt, done=done, char_delay=char_delay, timeout=timeout,
        idle=idle, display=display, input=input, crc=crc, frame_gap=frame_gap).items() if v is not None}
    serial_over = {k: v for k, v in dict(
        baud=baud, bytesize=bytesize, parity=parity.upper() if parity else None, stopbits=stopbits,
        rtscts=rtscts, xonxoff=xonxoff).items() if v is not None}

    sname = name or session_name_for(device)
    for s in client.load_sessions():
        if s["name"] == sname or str(s.get("device", "")).lower() == device.lower():
            if not client.is_alive(s):
                continue
            changes = dict(overrides, **serial_over)
            if profile:
                changes["profile"] = profile
            st = client.call(s, "set", changes) if changes else client.call(s, "status")
            note = "updated " + ", ".join(f"{k}={v}" for k, v in changes.items()) if changes else "reusing it"
            return Result(f"session '{s['name']}' is already open ({note})\n{_status_line(st)}",
                          {"session": st, "already_open": True})

    try:
        settings = profile_settings(profile or "generic")
    except ValueError as e:
        raise OpenSiTermError(str(e), "usage") from None
    settings.update(encoding="utf-8", char_delay=0.0)
    settings.update(overrides)
    cfg = {
        "name": sname,
        "serial": {"port": device, "match": match, "baud": int(baud or 115200), "bytesize": int(bytesize or 8),
                   "parity": (parity or "N").upper(), "stopbits": float(stopbits or 1),
                   "rtscts": bool(rtscts), "xonxoff": bool(xonxoff)},
        "settings": settings, "dtr": dtr, "rts": rts, "reconnect": reconnect,
        "project_dir": str(project), "log_dir": log_dir,
        "buffer_bytes": int(float(buffer_mb or 8) * (1 << 20)),
    }
    info = client.start_daemon(cfg)
    st = client.call(info, "status")
    text = (f"opened {st['device']} @ {st['baud']} {st['format']} as session '{st['name']}' "
            f"(profile {settings['profile']}, eol {settings['eol']})\n"
            f"log: {st['log']}\n"
            f"next: `opensiterm read --wait 2` to see output; `opensiterm send \"<command>\"` to run a command")
    return Result(text, {"session": st})


def close(name: Optional[str] = None, all: bool = False) -> Result:
    targets = client.load_sessions() if all else [client.find_session(name)]
    if not targets:
        return Result("no open sessions", {"closed": []})
    msgs, closed = [], []
    for s in targets:
        try:
            r = client.call(s, "close", timeout=5)
            client.wait_closed(s)
            msgs.append(f"closed '{s['name']}' (log: {r['log']})")
            closed.append(s["name"])
        except OpenSiTermError as e:
            msgs.append(f"'{s['name']}': {e}")
    return Result("\n".join(msgs), {"closed": closed})


def status(name: Optional[str] = None) -> Result:
    if name:
        targets = [client.find_session(name)]
    else:
        targets = client.load_sessions(check_alive=True)
    if not targets:
        return Result("no open sessions (start one with `opensiterm open <PORT>`)", {"sessions": []})
    out = []
    for s in targets:
        out.append(client.call(s, "status"))
    return Result("\n".join(_status_line(st) for st in out), {"sessions": out})


# --- reading ---------------------------------------------------------------------------

def read(name=None, *, wait=0.0, idle=0.3, max_lines=200, hex=None, raw=False, since=None,
         discard=False, ts=True, reader="default") -> Result:
    s = client.find_session(name)
    r = client.call(s, "read", dict(reader=reader, since=since, wait=wait, idle=idle, discard=discard,
                                    max_lines=max_lines, hex=hex, raw=raw), timeout=float(wait or 0) + 30)
    if discard:
        return Result(f"skipped {r['discarded_bytes']} unread bytes; cursor is now at the end", r)
    parts = []
    if r.get("dropped_bytes"):
        parts.append(f"## {r['dropped_bytes']} bytes before your cursor were dropped from the buffer; see {s['log']}")
    parts.append(_lines_text(r["lines"], ts) if r["lines"] else "(no new output)")
    if r.get("state") != "open":
        parts.append(f"## port state: {r.get('state')}")
    return Result("\n".join(parts), r)


def tail(name=None, *, lines=50, grep=None, hex=None, raw=False, ts=True) -> Result:
    s = client.find_session(name)
    r = client.call(s, "tail", dict(lines=lines, grep=grep, hex=hex, raw=raw))
    text = _lines_text(r["lines"], ts) if r["lines"] else ("(no matching lines)" if grep else "(buffer is empty)")
    return Result(text, r)


def expect(pattern: str, name=None, *, timeout=10.0, max_lines=200, hex=None, raw=False, ts=True) -> Result:
    s = client.find_session(name)
    r = client.call(s, "expect", dict(pattern=pattern, timeout=timeout, max_lines=max_lines, hex=hex, raw=raw),
                    timeout=float(timeout) + 30)
    body = _lines_text(r["lines"], ts) if r["lines"] else "(no new output)"
    if r["matched"]:
        return Result(f"{body}\n## matched: {r['match']!r}", r)
    return Result(f"{body}\n## timeout: {pattern!r} not seen within {timeout}s", r, 3)


# --- writing -----------------------------------------------------------------------------

def send(text: Optional[str] = None, name=None, *, hex=None, escapes=False, eol=None, crc=None,
         expect=None, timeout=None, idle=None, wait=True, char_delay=None, max_lines=200,
         unsafe=False, reason=None, ts=True, display_hex=None, raw=False) -> Result:
    s = client.find_session(name)
    args = dict(text=text, hex=hex, escapes=escapes, eol=eol, crc=crc, expect=expect, timeout=timeout,
                idle=idle, wait=wait, char_delay=char_delay, max_lines=max_lines, unsafe=unsafe,
                reason=reason, display_hex=display_hex, raw=raw)
    r = client.call(s, "send", args, timeout=float(timeout or 10) + 60)
    lines = r["lines"]
    pre, post = [], []
    if r.get("unread_skipped_lines"):
        pre.append(f"## note: {r['unread_skipped_lines']} unread line(s) from before this command were "
                   f"skipped (see `opensiterm tail` or the log)")
    code = 0
    done = r["done"]
    if done == "timeout":
        if r["explicit_expect"]:
            post.append(f"## timeout: {expect!r} not seen within {r['timeout']}s")
            code = 3
        elif _rx_count(lines) == 0:
            post.append(f"## no response within {r['timeout']}s (check baud rate, line ending --eol, "
                        f"wiring, or whether this command prints anything)")
        else:
            post.append(f"## still no prompt after {r['timeout']}s; the command may still be running "
                        f"(`opensiterm read --wait N` or `opensiterm expect PATTERN`)")
    elif done == "quiet":
        post.append("## device went quiet without showing its prompt/terminator; the command may still be "
                    "running (`opensiterm read --wait N`)")
    elif done == "expect":
        post.append(f"## matched: {r['matched']!r}")
    return Result("\n".join([*pre, _lines_text(lines, ts), *post]).strip(), r, code)


def control(name=None, *, dtr=None, rts=None, break_ms=None) -> Result:
    s = client.find_session(name)
    r = client.call(s, "control", dict(dtr=dtr, rts=rts, break_ms=break_ms))
    onoff = lambda v: "?" if v is None else ("on" if v else "off")
    return Result(f"DTR={onoff(r['dtr'])} RTS={onoff(r['rts'])}" + (f", sent {break_ms} ms break" if break_ms else ""), r)


def reset(name=None, *, line="rts", ms=100, wait=0.0, ts=True, max_lines=200) -> Result:
    s = client.find_session(name)
    client.call(s, "read", {"discard": True})
    r = client.call(s, "reset", dict(line=line, ms=ms))
    text = f"pulsed {line.upper()} for {ms} ms"
    if wait:
        rd = read(s["name"], wait=wait, idle=wait, max_lines=max_lines, ts=ts)
        text += "\n" + rd.text
        r["read"] = rd.data
    else:
        text += " (use `opensiterm read --wait 3` to see the boot log)"
    return Result(text, r)


def pause(name=None) -> Result:
    s = client.find_session(name)
    r = client.call(s, "pause")
    return Result(f"port released; other programs can open {s.get('device') or s['port']} now. "
                  f"Run `opensiterm resume` when done.", r)


def resume(name=None, *, timeout=10.0) -> Result:
    s = client.find_session(name)
    r = client.call(s, "resume", {"timeout": timeout}, timeout=float(timeout) + 10)
    if r["resumed"]:
        return Result(f"port {r['device']} re-acquired", r)
    return Result(f"port not back yet (state: {r['state']}); the session keeps retrying in the background", r)


def exec_released(cmd: List[str], name=None, *, resume_timeout=15.0) -> Result:
    s = client.find_session(name)
    client.call(s, "pause")
    print(f"[opensiterm] released {s.get('device') or s['port']}; running: {' '.join(cmd)}", file=sys.stderr, flush=True)
    try:
        rc = subprocess.call(cmd)
    except FileNotFoundError:
        rc = 127
        print(f"[opensiterm] command not found: {cmd[0]}", file=sys.stderr)
    finally:
        r = client.call(s, "resume", {"timeout": resume_timeout}, timeout=float(resume_timeout) + 10)
    back = "re-acquired" if r["resumed"] else f"not back yet (state {r['state']}; retrying in background)"
    return Result(f"command exited with code {rc}; port {back}. "
                  f"Next: `opensiterm read --wait 3` for the boot log", {"exit_code": rc, **r}, rc)


def set_options(name=None, **changes) -> Result:
    s = client.find_session(name)
    changes = {k: v for k, v in changes.items() if v is not None}
    if not changes:
        raise OpenSiTermError("nothing to change; pass e.g. --baud 9600 or --profile linux", "usage")
    st = client.call(s, "set", changes)
    return Result(_status_line(st), {"session": st})


# --- safety, project, skill ----------------------------------------------------------------

def rules(project_dir=None, check: Optional[str] = None) -> Result:
    project = paths.find_project_dir(project_dir)
    rs = rules_mod.RuleSet(project)
    rows = [f"config: {rs.path}" + ("" if rs.path.exists() else " (not created; run `opensiterm init`)")]
    if rs.error:
        rows.append(f"WARNING: {rs.error} (built-in rules still active)")
    for r in rs.rules:
        rows.append(f"  [{r.action}] {r.name}{' (built-in)' if r.builtin else ''}: {r.reason}")
    data = {"config": str(rs.path), "rules": [r.to_dict() for r in rs.rules], "error": rs.error}
    if check is not None:
        hit = rs.check(check)
        data["match"] = hit.to_dict() if hit else None
        rows.append(f"check {check!r}: " + (f"BLOCKED by '{hit.name}' ({hit.action}): {hit.reason}" if hit else "allowed"))
        return Result("\n".join(rows), data, 4 if hit else 0)
    return Result("\n".join(rows), data)


def init(project_dir=None) -> Result:
    project = paths.find_project_dir(project_dir)
    paths.project_state_dir(project)
    cfg = paths.config_file(project)
    if cfg.exists():
        return Result(f"{cfg} already exists", {"config": str(cfg), "created": False})
    cfg.write_text(rules_mod.CONFIG_TEMPLATE, encoding="utf-8")
    return Result(f"created {cfg}\nedit [defaults] and [[rules]], then commit it", {"config": str(cfg), "created": True})


def skill_path() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [here / "_skill" / "SKILL.md", here.parent.parent / "skills" / "opensiterm" / "SKILL.md"]
    if getattr(sys, "_MEIPASS", None):
        candidates.insert(0, Path(sys._MEIPASS) / "opensiterm" / "_skill" / "SKILL.md")
    for c in candidates:
        if c.exists():
            return c
    raise OpenSiTermError("SKILL.md not found in this installation", "error")


SKILL_TARGETS = {
    # Claude Code reads .claude/skills; Codex, opencode, oh-my-pi, Kimi, Crush, Gemini read .agents/skills
    # (Kimi, Crush and opencode read both). Antigravity CLI's user-level skills live under ~/.gemini.
    "claude": (Path.home() / ".claude" / "skills", Path(".claude") / "skills"),
    "agents": (Path.home() / ".agents" / "skills", Path(".agents") / "skills"),
    "antigravity": (Path.home() / ".gemini" / "antigravity-cli" / "skills", Path(".agents") / "skills"),
}
DEFAULT_SKILL_TARGETS = ("claude", "agents")


def install_skill(targets: Optional[List[str]] = None, project: bool = False, dest: Optional[str] = None) -> Result:
    src = skill_path()
    if dest:
        dirs = [Path(dest).expanduser()]
    else:
        names = targets or list(DEFAULT_SKILL_TARGETS)
        bad = [t for t in names if t not in SKILL_TARGETS]
        if bad:
            raise OpenSiTermError(f"unknown target(s) {bad}; choose from {list(SKILL_TARGETS)} or use --dest", "usage")
        root = paths.find_project_dir() if project else None
        dirs = [(root / SKILL_TARGETS[t][1]) if project else SKILL_TARGETS[t][0] for t in names]
    written = []
    for d in dirs:
        target = d / "opensiterm"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target / "SKILL.md")
        written.append(str(target / "SKILL.md"))
    return Result("installed skill:\n" + "\n".join(f"  {w}" for w in written), {"installed": written})


def _self_command() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    # Prefer the console script installed next to this interpreter (venv / pipx), then PATH.
    sibling = Path(sys.executable).parent / ("opensiterm.exe" if os.name == "nt" else "opensiterm")
    if sibling.exists():
        return [str(sibling)]
    exe = shutil.which("opensiterm")
    return [exe] if exe else [sys.executable, "-m", "opensiterm"]


def setup() -> Result:
    cmd = _self_command()
    exe, extra = cmd[0], cmd[1:]
    args = extra + ["mcp"]
    q = lambda p: f'"{p}"' if " " in p else p
    shell_cmd = " ".join(q(c) for c in cmd + ["mcp"])
    mcp_json = json.dumps({"mcpServers": {"opensiterm": {"command": exe, "args": args}}})
    opencode_json = json.dumps({"mcp": {"opensiterm": {"type": "local", "command": [exe, *args]}}})
    text = f"""OpenSiTerm {__version__}  ({' '.join(cmd)})

1) Agent Skill - teaches agents that have a shell how to use opensiterm:
     opensiterm skill install
   (copies SKILL.md to ~/.claude/skills and ~/.agents/skills: Claude Code, Codex, opencode,
    oh-my-pi, Kimi Code, Crush, Gemini CLI. Add --target antigravity for Antigravity CLI.
    Or: npx skills add aimachy/opensiterm;  pi: pi install git:github.com/aimachy/opensiterm)

2) MCP server - REQUIRED for Codex (its sandbox blocks serial devices and localhost for shell
   commands; MCP servers run outside it) and for chat apps without a shell:
     Claude Code : claude mcp add --transport stdio --scope user opensiterm -- {shell_cmd}
     Codex       : codex mcp add opensiterm -- {shell_cmd}
     Kimi Code   : kimi mcp add --transport stdio opensiterm -- {shell_cmd}
     Gemini CLI  : gemini extensions install https://github.com/aimachy/opensiterm   (skill + MCP)
     Qwen Code   : qwen extensions install https://github.com/aimachy/opensiterm     (skill + MCP)
     opencode    : merge into ~/.config/opencode/opencode.json:
                   {opencode_json}
     oh-my-pi    : merge into ~/.omp/agent/mcp.json:
                   {mcp_json}
     Antigravity : merge into ~/.gemini/config/mcp_config.json:
                   {mcp_json}
     Crush       : add to ~/.config/crush/crushrc:
                   mcp add opensiterm --command {q(exe)} --args {' '.join(args)}
     Others (Claude Desktop, Cherry Studio, DeepSeek clients, Cursor...): the standard form
                   {mcp_json}

3) Optional, per project: `opensiterm init` creates .opensiterm/config.toml (default port and
   safety rules). With Claude Code, allow the read-only commands to skip prompts, e.g.
   "Bash(opensiterm read:*)", "Bash(opensiterm send:*)", but never "Bash(opensiterm:*)":
   that would also auto-approve `opensiterm unsafe-send`.
"""
    return Result(text, {"command": cmd})


def profiles_text() -> Result:
    rows = [f"{name:<8} {p['description']}" for name, p in PROFILES.items()]
    return Result("\n".join(rows), {"profiles": {n: profile_settings(n) for n in PROFILES}})
