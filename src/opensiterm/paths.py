"""Where OpenSiTerm keeps its files.

Session registry (pid, tcp port, token) lives in a per-user state dir so any
shell can find the running sessions. Logs and the safety config live in the
project's .opensiterm/ dir so agents can grep them without leaving the repo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIRNAME = ".opensiterm"


def state_dir() -> Path:
    env = os.environ.get("OPENSITERM_HOME")
    if env:
        p = Path(env)
    elif os.name == "nt":
        p = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "opensiterm"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "opensiterm"
    else:
        p = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "opensiterm"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sessions_dir() -> Path:
    d = state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_project_dir(explicit=None) -> Path:
    """Nearest ancestor holding .opensiterm/ or .git, else the current directory."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("OPENSITERM_PROJECT")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / PROJECT_DIRNAME).is_dir() or (d / ".git").exists():
            return d
    return cwd


def project_state_dir(project: Path) -> Path:
    d = Path(project) / PROJECT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    gitignore = d / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# OpenSiTerm: commit config.toml, ignore logs\nlogs/\n", encoding="utf-8")
    return d


def logs_dir(project: Path, override=None) -> Path:
    d = Path(override).expanduser().resolve() if override else project_state_dir(project) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file(project: Path) -> Path:
    return Path(project) / PROJECT_DIRNAME / "config.toml"
