"""Command-line interface. Run `opensiterm -h`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, api
from .client import OpenSiTermError
from .profiles import EOLS, PROFILES
from .util import CRCS

EPILOG = """\
typical session:
  opensiterm ports                      list serial ports
  opensiterm open COM7 -b 115200        start a background session (Linux: /dev/ttyUSB0)
  opensiterm read --wait 2              new output since the last read
  opensiterm send "help"                send a line and print the reply
  opensiterm expect "boot done" -t 30   wait for a pattern
  opensiterm watch                      (humans) live view; type to send
  opensiterm close                      end the session

exit codes: 0 ok, 1 error, 2 usage, 3 timeout/no match, 4 blocked by a safety rule, 5 no session
docs: https://github.com/aimachy/opensiterm
"""


def _onoff(v: str) -> bool:
    low = v.lower()
    if low in ("on", "1", "true", "yes", "high", "assert"):
        return True
    if low in ("off", "0", "false", "no", "low", "deassert"):
        return False
    raise argparse.ArgumentTypeError(f"expected on/off, got {v!r}")


def _ms(v: str) -> float:
    return float(v) / 1000.0


def _add_line_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("line settings")
    g.add_argument("-b", "--baud", type=int, help="baud rate (default 115200)")
    g.add_argument("--bytesize", type=int, choices=(5, 6, 7, 8))
    g.add_argument("--parity", choices=("N", "E", "O", "M", "S", "n", "e", "o", "m", "s"))
    g.add_argument("--stopbits", type=float, choices=(1, 1.5, 2))
    g.add_argument("--rtscts", type=_onoff, metavar="on|off", help="hardware flow control")
    g.add_argument("--xonxoff", type=_onoff, metavar="on|off", help="software flow control")
    g = p.add_argument_group("behaviour")
    g.add_argument("--profile", choices=list(PROFILES), help="device preset (see `opensiterm profiles`)")
    g.add_argument("--eol", choices=list(EOLS), help="line ending appended to sent text")
    g.add_argument("--encoding", help="text encoding, e.g. utf-8 (default) or gbk")
    g.add_argument("--prompt", help="regex for the device's input prompt (ends a reply)")
    g.add_argument("--done", help="regex (multiline) that marks the end of a reply, e.g. '^OK$'")
    g.add_argument("--timeout", type=float, help="default seconds to wait for a reply")
    g.add_argument("--idle", type=float, help="seconds of silence that end a reply")
    g.add_argument("--display", choices=("text", "hex"))
    g.add_argument("--input", choices=("text", "hex"), help="how `send TEXT` is interpreted")
    g.add_argument("--crc", choices=["none", *CRCS], help="checksum appended to every send")
    g.add_argument("--frame-gap", type=_ms, metavar="MS", help="split received binary frames on gaps of MS")
    g.add_argument("--char-delay", type=_ms, metavar="MS", help="pause between sent bytes (slow devices)")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", "--session", default=argparse.SUPPRESS,
                        help="session name or port (default: the only open session)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    common.add_argument("--no-ts", action="store_true", default=argparse.SUPPRESS, help="omit timestamps")

    p = argparse.ArgumentParser(
        prog="opensiterm",
        description="Serial port access for AI agents: a background session per port, plus a CLI, "
                    "an MCP server and an Agent Skill.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"opensiterm {__version__}")
    p.add_argument("-s", "--session", default=None, help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-ts", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, help_, **kw):
        return sub.add_parser(name, help=help_, description=help_, parents=[common], **kw)

    add("ports", "list serial ports")

    sp = add("open", "start a background session on a port (idempotent)")
    sp.add_argument("port", nargs="?", help="COM7, /dev/ttyUSB0, auto, socket://host:port, rfc2217://..., loop://")
    sp.add_argument("--name", help="session name (default: derived from the port)")
    sp.add_argument("--match", help="pick the port whose description/USB id/serial contains this text; "
                                    "re-resolved on every reconnect")
    sp.add_argument("--dtr", type=_onoff, metavar="on|off", help="initial DTR state")
    sp.add_argument("--rts", type=_onoff, metavar="on|off", help="initial RTS state")
    sp.add_argument("--log-dir", help="where to write logs (default: <project>/.opensiterm/logs)")
    sp.add_argument("--project-dir", help="project root (default: nearest dir with .opensiterm/ or .git)")
    sp.add_argument("--no-reconnect", action="store_true", help="don't reopen the port when it disappears")
    sp.add_argument("--buffer-mb", type=float, help="in-memory history size (default 8)")
    _add_line_options(sp)

    sp = add("close", "end a session and release the port")
    sp.add_argument("--all", action="store_true", help="close every session")

    add("status", "show sessions")

    sp = add("read", "print output received since your last read")
    sp.add_argument("-w", "--wait", type=float, default=0.0, metavar="S",
                    help="wait up to S seconds for new output (returns once it goes quiet)")
    sp.add_argument("--idle", type=float, default=0.3, metavar="S", help="silence that ends --wait (default 0.3)")
    sp.add_argument("-n", "--max-lines", type=int, default=200, help="truncate long output (default 200)")
    sp.add_argument("--hex", action="store_true", default=None, help="show bytes as hex")
    sp.add_argument("--raw", action="store_true", help="keep ANSI escape codes")
    sp.add_argument("--since", type=int, metavar="CURSOR", help="read from this cursor position")
    sp.add_argument("--discard", action="store_true", help="mark everything as read without printing it")
    sp.add_argument("--reader", default="default", help=argparse.SUPPRESS)

    sp = add("tail", "print the most recent lines (does not move the read cursor)")
    sp.add_argument("-n", "--lines", type=int, default=50)
    sp.add_argument("-g", "--grep", metavar="REGEX", help="only lines matching REGEX")
    sp.add_argument("--hex", action="store_true", default=None)
    sp.add_argument("--raw", action="store_true")

    for name, help_ in (("send", "send a line (or bytes) and print the reply"),
                        ("unsafe-send", "send a command that a safety rule blocked; needs --reason "
                                        "and is meant to be approved by the user")):
        sp = add(name, help_)
        sp.add_argument("text", nargs="*", help="text to send (quote it); omit to send just the line ending")
        sp.add_argument("-x", "--hex", metavar="BYTES", help="send raw bytes, e.g. '01 03 00 00 00 0A'")
        sp.add_argument("-e", "--escapes", action="store_true", help=r"interpret \r \n \t \xHH in TEXT")
        sp.add_argument("--ctrl", metavar="KEY", help="send a control character, e.g. --ctrl c for Ctrl+C")
        sp.add_argument("--eol", choices=list(EOLS), help="line ending for this send")
        sp.add_argument("--crc", choices=["none", *CRCS], help="append a checksum")
        sp.add_argument("--expect", metavar="REGEX", help="wait until the reply matches REGEX (multiline)")
        sp.add_argument("-t", "--timeout", type=float, metavar="S", help="max seconds to wait for the reply")
        sp.add_argument("--idle", type=float, metavar="S", help="silence that ends the reply")
        sp.add_argument("--no-wait", action="store_true", help="don't wait for a reply")
        sp.add_argument("--char-delay", type=_ms, metavar="MS", help="pause between bytes")
        sp.add_argument("-n", "--max-lines", type=int, default=200)
        sp.add_argument("--raw", action="store_true", help="keep ANSI escape codes")
        if name == "unsafe-send":
            sp.add_argument("--reason", required=True, help="why the user wants this command")

    sp = add("expect", "wait until new output matches a regex")
    sp.add_argument("pattern")
    sp.add_argument("-t", "--timeout", type=float, default=10.0)
    sp.add_argument("-n", "--max-lines", type=int, default=200)
    sp.add_argument("--hex", action="store_true", default=None)

    sp = add("control", "set modem control lines / send a break")
    sp.add_argument("--dtr", type=_onoff, metavar="on|off")
    sp.add_argument("--rts", type=_onoff, metavar="on|off")
    sp.add_argument("--break", dest="break_ms", type=float, metavar="MS", help="send a break of MS milliseconds")

    sp = add("reset", "reset the board by pulsing RTS (ESP32-style) or DTR (Arduino-style)")
    sp.add_argument("--line", choices=("rts", "dtr"), default="rts")
    sp.add_argument("--ms", type=float, default=100, help="pulse length (default 100)")
    sp.add_argument("-w", "--wait", type=float, default=0.0, metavar="S", help="then collect output for S seconds")

    add("pause", "release the port temporarily (e.g. for a flasher)")
    sp = add("resume", "re-acquire a paused port")
    sp.add_argument("-t", "--timeout", type=float, default=10.0)

    sp = add("exec", "release the port, run a command (e.g. a flasher), then re-acquire the port")
    sp.add_argument("--resume-timeout", type=float, default=15.0)
    sp.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD [ARGS...]")

    sp = add("set", "change settings of a running session (baud, profile, eol...)")
    _add_line_options(sp)

    sp = add("watch", "live view for humans; type lines to send them")
    sp.add_argument("--history", type=int, default=20, help="lines of history to show first")
    sp.add_argument("--raw", action="store_true", help="keep ANSI colors")

    sp = add("rules", "list the safety rules in effect, or test a command against them")
    sp.add_argument("--check", metavar="TEXT", help="would TEXT be blocked?")
    sp.add_argument("--project-dir")

    sp = add("init", "create .opensiterm/config.toml (default port, safety rules) in this project")
    sp.add_argument("--project-dir")

    add("profiles", "list device presets")

    sp = add("skill", "show or install the Agent Skill (SKILL.md)")
    sp.add_argument("action", choices=("show", "path", "install"))
    sp.add_argument("--target", action="append", choices=("claude", "agents", "antigravity"),
                    help="claude = ~/.claude/skills, agents = ~/.agents/skills, antigravity = "
                         "~/.gemini/antigravity-cli/skills (default: claude + agents)")
    sp.add_argument("--project", action="store_true", help="install into this project instead of your home dir")
    sp.add_argument("--dest", help="install into this skills directory instead")

    add("setup", "print how to register opensiterm with Claude Code, Codex, opencode, oh-my-pi, ...")
    add("mcp", "run the MCP server on stdio (for MCP clients)")

    sp = sub.add_parser("_daemon")  # internal: the background session process
    sp.add_argument("config")
    return p


def _setup_stdout() -> None:
    # Agents read our stdout through a pipe and expect UTF-8, whatever the console code page is.
    enc = os.environ.get("OPENSITERM_ENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding=enc, errors="replace")
            except Exception:
                pass


def _send_args(a) -> dict:
    text = " ".join(a.text) if a.text else None
    hex_, eol = a.hex, a.eol
    if a.ctrl:
        key = a.ctrl.strip().upper()
        if len(key) != 1 or not ("@" <= key <= "_"):
            raise OpenSiTermError("--ctrl takes one letter, e.g. --ctrl c", "usage")
        hex_, text, eol = "%02X" % (ord(key) - 64), None, eol or "none"
    if text is None and hex_ is None:
        text = ""  # just the line ending: "press Enter"
    return dict(text=text, hex=hex_, escapes=a.escapes, eol=eol, crc=a.crc, expect=a.expect,
                timeout=a.timeout, idle=a.idle, wait=not a.no_wait, char_delay=a.char_delay,
                max_lines=a.max_lines, raw=a.raw)


def _line_kwargs(a) -> dict:
    return dict(baud=a.baud, bytesize=a.bytesize, parity=a.parity, stopbits=a.stopbits, rtscts=a.rtscts,
                xonxoff=a.xonxoff, profile=a.profile, eol=a.eol, encoding=a.encoding, prompt=a.prompt,
                done=a.done, timeout=a.timeout, idle=a.idle, display=a.display, input=a.input, crc=a.crc,
                frame_gap=a.frame_gap, char_delay=a.char_delay)


def _run(a) -> api.Result:
    s, ts = a.session, not a.no_ts
    cmd = a.cmd
    if cmd == "ports":
        return api.list_ports()
    if cmd == "open":
        return api.open_session(a.port, name=a.name, match=a.match, dtr=a.dtr, rts=a.rts, log_dir=a.log_dir,
                                project_dir=a.project_dir, reconnect=not a.no_reconnect,
                                buffer_mb=a.buffer_mb, **_line_kwargs(a))
    if cmd == "close":
        return api.close(s, all=a.all)
    if cmd == "status":
        return api.status(s)
    if cmd == "read":
        return api.read(s, wait=a.wait, idle=a.idle, max_lines=a.max_lines, hex=a.hex, raw=a.raw,
                        since=a.since, discard=a.discard, ts=ts, reader=a.reader)
    if cmd == "tail":
        return api.tail(s, lines=a.lines, grep=a.grep, hex=a.hex, raw=a.raw, ts=ts)
    if cmd in ("send", "unsafe-send"):
        kw = _send_args(a)
        if cmd == "unsafe-send":
            kw.update(unsafe=True, reason=a.reason)
        return api.send(name=s, ts=ts, **kw)
    if cmd == "expect":
        return api.expect(a.pattern, s, timeout=a.timeout, max_lines=a.max_lines, hex=a.hex, ts=ts)
    if cmd == "control":
        return api.control(s, dtr=a.dtr, rts=a.rts, break_ms=a.break_ms)
    if cmd == "reset":
        return api.reset(s, line=a.line, ms=a.ms, wait=a.wait, ts=ts)
    if cmd == "pause":
        return api.pause(s)
    if cmd == "resume":
        return api.resume(s, timeout=a.timeout)
    if cmd == "exec":
        command = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not command:
            raise OpenSiTermError("usage: opensiterm exec -- CMD [ARGS...]", "usage")
        return api.exec_released(command, s, resume_timeout=a.resume_timeout)
    if cmd == "set":
        return api.set_options(s, **_line_kwargs(a))
    if cmd == "rules":
        return api.rules(a.project_dir, a.check)
    if cmd == "init":
        return api.init(a.project_dir)
    if cmd == "profiles":
        return api.profiles_text()
    if cmd == "skill":
        if a.action == "install":
            return api.install_skill(a.target, a.project, a.dest)
        path = api.skill_path()
        return api.Result(str(path) if a.action == "path" else path.read_text(encoding="utf-8"), {"path": str(path)})
    if cmd == "setup":
        return api.setup()
    raise OpenSiTermError(f"unknown command {cmd!r}", "usage")


def main(argv=None) -> int:
    _setup_stdout()
    parser = build_parser()
    a = parser.parse_args(argv)
    if not a.cmd:
        parser.print_help()
        return 0
    if a.cmd == "_daemon":
        from .daemon import run
        return run(a.config)
    if a.cmd == "mcp":
        from .mcp import serve
        return serve()
    if a.cmd == "watch":
        from .watch import run_watch
        return run_watch(a.session, history=a.history, raw=a.raw, ts=not a.no_ts)
    try:
        result = _run(a)
    except OpenSiTermError as e:
        if a.json:
            print(json.dumps({"ok": False, "error": str(e), "code": e.code}, ensure_ascii=False))
        else:
            print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    if a.json:
        print(json.dumps(result.data, ensure_ascii=False, default=str))
    else:
        print(result.text)
    return result.code
