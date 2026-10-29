"""Find running sessions, talk to them, and start new ones."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import paths


class OpenSiTermError(Exception):
    EXIT_CODES = {"usage": 2, "timeout": 3, "blocked": 4, "no_session": 5}

    def __init__(self, message: str, code: str = "error", extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}

    @property
    def exit_code(self) -> int:
        return self.EXIT_CODES.get(self.code, 1)


# --- session registry ----------------------------------------------------------

def load_sessions(check_alive: bool = False) -> List[dict]:
    out = []
    for f in sorted(paths.sessions_dir().glob("*.json")):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        info["_file"] = str(f)
        if check_alive and not is_alive(info):
            continue
        out.append(info)
    return out


def is_alive(info: dict) -> bool:
    try:
        call(info, "ping", timeout=3)
        return True
    except OpenSiTermError:
        return False


def _matches(info: dict, name: str) -> bool:
    low = name.lower()
    return low in (info["name"].lower(), str(info.get("port", "")).lower(), str(info.get("device", "")).lower())


def find_session(name: Optional[str] = None) -> dict:
    sessions = load_sessions()
    if name:
        for s in sessions:
            if _matches(s, name):
                return s
        names = ", ".join(s["name"] for s in sessions) or "none"
        raise OpenSiTermError(f"no session {name!r} (open sessions: {names}). "
                              f"Start one with `opensiterm open {name}`", "no_session")
    if len(sessions) > 1:
        sessions = load_sessions(check_alive=True)
    if len(sessions) == 1:
        return sessions[0]
    if not sessions:
        raise OpenSiTermError("no open session. Start one with `opensiterm open <PORT>` "
                              "(list ports with `opensiterm ports`)", "no_session")
    raise OpenSiTermError(f"{len(sessions)} sessions open ({', '.join(s['name'] for s in sessions)}); "
                          f"choose one with -s NAME", "usage")


def _forget(info: dict) -> None:
    try:
        Path(info["_file"]).unlink()
    except (KeyError, OSError):
        pass


# --- RPC -------------------------------------------------------------------------

def call(info: dict, op: str, args: Optional[dict] = None, timeout: Optional[float] = None):
    req = json.dumps({"token": info["token"], "op": op, "args": args or {}}) + "\n"
    try:
        sock = socket.create_connection(("127.0.0.1", int(info["tcp_port"])), timeout=3)
    except OSError:
        _forget(info)
        raise OpenSiTermError(f"session {info['name']!r} is not running (stale entry removed). "
                              f"Reopen it with `opensiterm open {info.get('port', info['name'])}`",
                              "no_session") from None
    buf = bytearray()
    try:
        with sock:
            sock.settimeout(30 if timeout is None else timeout)
            sock.sendall(req.encode("utf-8"))
            while not buf.endswith(b"\n"):
                chunk = sock.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
    except socket.timeout:
        raise OpenSiTermError(f"session {info['name']!r} did not answer in time", "error") from None
    except OSError as e:
        raise OpenSiTermError(f"lost connection to session {info['name']!r}: {e}", "error") from None
    if not buf:
        raise OpenSiTermError(f"session {info['name']!r} closed the connection", "error")
    resp = json.loads(buf.decode("utf-8"))
    if not resp.get("ok"):
        raise OpenSiTermError(resp.get("error") or "unknown error", resp.get("code") or "error", resp)
    return resp["result"]


# --- starting a daemon -------------------------------------------------------------

def daemon_command(cfg_path: Path) -> List[str]:
    if getattr(sys, "frozen", False):  # PyInstaller single-file build
        return [sys.executable, "_daemon", str(cfg_path)]
    return [sys.executable, "-m", "opensiterm", "_daemon", str(cfg_path)]


def start_daemon(cfg: dict, timeout: float = 15.0) -> dict:
    run_dir = paths.state_dir() / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{cfg['name']}-{secrets.token_hex(4)}"
    cfg_path = run_dir / f"{tag}.json"
    ready = run_dir / f"{tag}.ready"
    cfg_path.write_text(json.dumps(dict(cfg, ready_file=str(ready))), encoding="utf-8")
    if os.name == "posix":
        os.chmod(cfg_path, 0o600)
    daemon_log = paths.sessions_dir() / f"{cfg['name']}.daemon.log"

    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    if not getattr(sys, "frozen", False):
        # Make the package importable in the child even when running from a source checkout.
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    with open(daemon_log, "ab") as logf:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, cwd=cfg["project_dir"],
                      env=env, close_fds=True)
        cmd = daemon_command(cfg_path)
        if os.name == "nt":
            # Detach from the agent's console and process group so the session outlives the
            # command that started it; break away from the job object when the host allows it.
            detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
            try:
                proc = subprocess.Popen(cmd, creationflags=detached | new_group | breakaway, **kwargs)
            except OSError:
                proc = subprocess.Popen(cmd, creationflags=detached | new_group, **kwargs)
        else:
            proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.exists():
            try:
                res = json.loads(ready.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            try:
                ready.unlink()
            except OSError:
                pass
            if res.get("ok"):
                return res["info"]
            raise OpenSiTermError(res.get("error") or "daemon failed to start", "error")
        if proc.poll() is not None:
            raise OpenSiTermError(f"session daemon exited with code {proc.returncode} before it was ready.\n"
                                  f"{_tail(daemon_log)}", "error")
        time.sleep(0.05)
    raise OpenSiTermError(f"timed out waiting for the session daemon; see {daemon_log}", "error")


def _tail(path: Path, n: int = 15) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def wait_closed(info: dict, timeout: float = 3.0) -> None:
    """Block until a closing session has removed its registry entry (port released)."""
    f = Path(info.get("_file") or paths.sessions_dir() / f"{info['name']}.json")
    deadline = time.monotonic() + timeout
    while f.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)
