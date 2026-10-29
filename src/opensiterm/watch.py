"""`opensiterm watch`: live view for humans, and a way to type into the shared port.

Input is only accepted from a real terminal, so an agent can't pipe commands
through here to get around the safety rules by accident.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from . import client, render
from .client import OpenSiTermError

HELP = """\
local commands:
  /ctrl <key>     send a control character, e.g. /ctrl c  (Ctrl+C)
  /hex <bytes>    send raw bytes, e.g. /hex 01 03 00 00 00 0a
  /esc <text>     send text with \\r \\n \\t \\xHH escapes and no line ending
  /pause /resume  release / re-acquire the port
  /reset [rts|dtr]  pulse a control line to reset the board
  /quit           leave watch (the session keeps running)
  //text          send a line that starts with '/'
anything else is sent as a line, with the session's line ending appended."""


class _Printer:
    def __init__(self, ts: bool):
        self.ts = ts
        self.partial = False
        self.lock = threading.Lock()

    def lines(self, lines) -> None:
        with self.lock:
            out = sys.stdout
            for ln in lines:
                if ln["cont"] and self.partial:
                    out.write(ln["text"])
                else:
                    if self.partial:
                        out.write("\n")
                    out.write(render.format_line(ln, self.ts))
                self.partial = ln["partial"]
                if not self.partial:
                    out.write("\n")
            out.flush()

    def note(self, msg: str) -> None:
        with self.lock:
            if self.partial:
                sys.stdout.write("\n")
                self.partial = False
            sys.stdout.write(f"-- {msg}\n")
            sys.stdout.flush()


def _send_human(session: dict, args: dict, printer: _Printer) -> None:
    args = dict(args, source="human", wait=False)
    try:
        client.call(session, "send", args)
    except OpenSiTermError as e:
        if e.code != "confirm_human":
            raise
        printer.note(f"{e}. Send anyway? [y/N]")
        if sys.stdin.readline().strip().lower() in ("y", "yes"):
            client.call(session, "send", dict(args, force=True))
        else:
            printer.note("not sent")


def _local_command(session: dict, line: str, printer: _Printer, stop: threading.Event) -> None:
    cmd, _, rest = line[1:].partition(" ")
    cmd = cmd.lower()
    if cmd in ("q", "quit", "exit"):
        stop.set()
    elif cmd in ("h", "help", "?"):
        printer.note(HELP)
    elif cmd == "ctrl":
        key = rest.strip().upper()
        if len(key) != 1 or not ("@" <= key <= "_"):
            printer.note("usage: /ctrl c")
            return
        _send_human(session, {"hex": "%02X" % (ord(key) - 64), "eol": "none"}, printer)
    elif cmd == "hex":
        _send_human(session, {"hex": rest}, printer)
    elif cmd == "esc":
        _send_human(session, {"text": rest, "escapes": True, "eol": "none"}, printer)
    elif cmd == "pause":
        client.call(session, "pause")
        printer.note("port released")
    elif cmd == "resume":
        r = client.call(session, "resume", {"timeout": 10}, timeout=20)
        printer.note("port re-acquired" if r["resumed"] else f"port not back yet ({r['state']})")
    elif cmd == "reset":
        client.call(session, "reset", {"line": (rest.strip() or "rts").lower()})
    else:
        printer.note(f"unknown command /{cmd} (try /help)")


def _input_loop(session: dict, printer: _Printer, stop: threading.Event) -> None:
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        try:
            if line.startswith("//"):
                _send_human(session, {"text": line[1:]}, printer)
            elif line.startswith("/"):
                _local_command(session, line, printer, stop)
            else:
                _send_human(session, {"text": line}, printer)
        except OpenSiTermError as e:
            printer.note(str(e))
        if stop.is_set():
            return
    stop.set()


def _stdin_is_terminal() -> bool:
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if os.name == "nt":
        # isatty() is also true for NUL; only a real console accepts GetConsoleMode.
        import ctypes
        import msvcrt
        try:
            mode = ctypes.c_uint32()
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        except Exception:
            return False
    return True


def run_watch(name=None, history: int = 20, raw: bool = False, ts: bool = True) -> int:
    try:
        session = client.find_session(name)
        hist = client.call(session, "tail", {"lines": history, "raw": raw})
        reader = f"watch-{os.getpid()}"
        client.call(session, "read", {"reader": reader, "discard": True})
    except OpenSiTermError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    printer = _Printer(ts)
    interactive = _stdin_is_terminal()
    printer.note(f"watching '{session['name']}' ({session.get('device')}); log: {session.get('log')}")
    printer.note("type a line + Enter to send it; /help for commands; Ctrl+C to quit" if interactive
                 else "read-only (stdin is not a terminal); Ctrl+C to quit")
    printer.lines(hist["lines"])

    stop = threading.Event()
    if interactive:
        threading.Thread(target=_input_loop, args=(session, printer, stop), daemon=True).start()
    try:
        while not stop.is_set():
            try:
                r = client.call(session, "read", {"reader": reader, "wait": 1.0, "idle": 0.05,
                                                  "max_lines": 5000, "max_chars": 20000, "raw": raw},
                                timeout=40)
            except OpenSiTermError as e:
                if e.code == "no_session":
                    printer.note("session closed")
                    return 0
                printer.note(str(e))
                time.sleep(1)
                continue
            printer.lines(r["lines"])
    except KeyboardInterrupt:
        pass
    printer.note("bye (the session keeps running; `opensiterm close` ends it)")
    return 0
