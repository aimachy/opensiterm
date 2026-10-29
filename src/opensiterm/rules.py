"""Safety rules: stop obviously irreversible commands before they reach the device.

These are guard rails against an agent's *mistakes*, not a security boundary:
the agent runs as the same OS user and could bypass them on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .paths import config_file

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


@dataclass
class Rule:
    name: str
    pattern: "re.Pattern"
    action: str  # "confirm" (needs unsafe-send) or "deny" (human only)
    reason: str
    builtin: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "pattern": self.pattern.pattern, "action": self.action,
                "reason": self.reason, "builtin": self.builtin}


# (name, regex, reason). Matched case-insensitively against the text being sent.
BUILTIN_RULES = [
    ("efuse",
     r"\b(?:e?fuse|otp)\s+(?:prog\w*|write|burn|blow|lock|override)\b|\bburn_?(?:e?fuse|key|block)\w*",
     "eFuse/OTP programming is irreversible"),
    ("flash-erase",
     r"\b(?:nand|sf|mmc|mtd|flash|spi|nor|emmc)\s+erase\w*|\b(?:flash_erase(?:all)?|erase_?flash|mass_?erase|chip_?erase|erase_?all)\b|^\s*erase\b",
     "erases flash storage"),
    ("flash-write",
     r"\b(?:nand|sf|mmc|mtd)\s+write\w*|\bdd\b[^\n]*\bof=/dev/|\b(?:flashcp|nandwrite|sysupgrade|fw_setenv)\b",
     "writes raw flash / firmware / block devices"),
    ("format",
     r"\bmkfs(?:\.\w+)?\b|\b(?:wipefs|fdisk|sfdisk|parted|sgdisk)\b|^\s*(?:\w+\s+)?format\b",
     "formats or repartitions storage"),
    ("factory-reset",
     r"\benv\s+default\s+-a\b|\bnvs_erase\w*|\bfactory[_ -]?reset\b|\bAT&F\d?\b",
     "wipes the device configuration"),
    ("rm-root",
     r"\brm\s+(?:-\w+\s+)*-\w*r\w*\s+(?:-\w+\s+)*(?:/|/\*|~|\*)(?:\s|$)",
     "recursively deletes the root, home or current directory"),
    ("sim-lock",
     r"\bAT\+(?:CPIN|CPWD|CLCK)=",
     "SIM PIN/lock command: a few wrong attempts lock the SIM (PUK)"),
    ("chip-lock",
     r"\b(?:readout[_ ]?protect\w*|read[_ ]?protect\w*|lock[_ ]?chip|rdp\s+(?:level\s*)?[12]|secure[_ ]?boot\s+(?:enable|burn|lock)|flash[_ ]?encryption\s+(?:enable|burn))\b",
     "enables read-out protection / secure boot (may be permanent)"),
]

CONFIG_TEMPLATE = """\
# OpenSiTerm project config. Commit this file so your team and your AI agents
# share the same safety rules. Logs in .opensiterm/logs/ are git-ignored.

[defaults]
# Used by `opensiterm open` when no port is given on the command line.
# port = "COM7"          # or "/dev/ttyUSB0", "auto", "socket://192.168.1.50:4000"
# baud = 115200
# profile = "mcu"        # generic | mcu | at | linux | uboot | binary | modbus

[safety]
# Built-in rules catch obviously irreversible commands (eFuse/OTP, flash erase,
# mkfs, dd to /dev, factory reset, SIM PIN...). Run `opensiterm rules` to list them.
builtin_rules = true
# disable = ["format"]   # switch off individual built-in rules by name

# Your own rules. `pattern` is a Python regex, case-insensitive by default,
# matched against the text the AI sends.
#   action = "confirm": the AI must re-send with `opensiterm unsafe-send --reason ...`,
#                       which your AI tool will ask you to approve.
#   action = "deny":    the AI can never send it; only a human can, from `opensiterm watch`.
#
# [[rules]]
# name = "motor"
# pattern = '\\bmotor\\s+(start|run)\\b'
# action = "confirm"
# reason = "starts the motor - make sure the fixture is clear"
"""


def load_config(path: Path) -> dict:
    if tomllib is None:
        raise RuntimeError("reading config.toml needs the 'tomli' package on Python < 3.11 (pip install tomli)")
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def build_rules(cfg: dict) -> List[Rule]:
    safety = cfg.get("safety") or {}
    rules: List[Rule] = []
    if safety.get("builtin_rules", True):
        disabled = set(safety.get("disable") or [])
        for name, pattern, reason in BUILTIN_RULES:
            if name not in disabled:
                rules.append(Rule(name, re.compile(pattern, re.I | re.M), "confirm", reason, True))
    for i, r in enumerate(cfg.get("rules") or []):
        name = r.get("name") or f"rule{i + 1}"
        action = r.get("action", "confirm")
        if action not in ("confirm", "deny"):
            raise ValueError(f"rule {name!r}: action must be 'confirm' or 'deny', not {action!r}")
        if "pattern" not in r:
            raise ValueError(f"rule {name!r}: missing 'pattern'")
        flags = re.M if r.get("case_sensitive") else re.I | re.M
        rules.append(Rule(name, re.compile(r["pattern"], flags), action, r.get("reason", ""), False))
    return rules


class RuleSet:
    """Rules for one project; config.toml is re-read whenever it changes."""

    def __init__(self, project_dir):
        self.path = config_file(project_dir)
        self._mtime = None
        self.config: dict = {}
        self.rules: List[Rule] = []
        self.error: Optional[str] = None
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        self.error = None
        cfg: dict = {}
        if mtime is not None:
            try:
                cfg = load_config(self.path)
            except Exception as e:
                self.error = f"{self.path}: {e}"
        try:
            self.rules = build_rules(cfg)
        except Exception as e:
            # A broken config must not switch safety off: fall back to built-ins.
            self.error = f"{self.path}: {e}"
            self.rules = build_rules({})
        self.config = cfg

    def check(self, text: str) -> Optional[Rule]:
        """First matching rule; a 'deny' match wins over 'confirm'."""
        self.reload()
        hit = None
        for rule in self.rules:
            if rule.pattern.search(text):
                if rule.action == "deny":
                    return rule
                hit = hit or rule
        return hit
