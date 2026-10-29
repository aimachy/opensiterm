"""Device presets: line ending, how to tell a reply is finished, display mode."""

from __future__ import annotations

EOLS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n", "none": b""}

# prompt: regex tested against the last, unterminated line once the device goes quiet.
# done:   regex (multiline) searched in the reply; a match ends the wait immediately.
_BASE = dict(
    eol="cr", prompt=None, done=None, timeout=5.0, idle=0.5,
    display="text", input="text", split_lines=True, frame_gap=None, crc=None,
)

PROFILES = {
    "generic": dict(
        description="Unknown device: CR line ending; a reply ends after 0.5 s of silence.",
    ),
    "mcu": dict(
        eol="crlf",
        prompt=r"(?:^|\s)\S{0,40}[>$#]\s?$",
        description="MCU shells (Zephyr, RT-Thread, NuttX, ESP-IDF console, FreeRTOS CLI, "
                    "Arduino sketches): CRLF; waits for the shell prompt.",
    ),
    "at": dict(
        eol="cr",
        done=r"^(?:OK|ERROR|\+CM[ES] ERROR:.*|NO CARRIER|BUSY|NO ANSWER|NO DIALTONE|SEND OK|SEND FAIL)\s*$",
        prompt=r"^>\s?$",
        timeout=10.0,
        description="AT-command modules (4G/NB-IoT/Wi-Fi/BT): CR; a reply ends at OK/ERROR "
                    "or the '>' data prompt.",
    ),
    "linux": dict(
        eol="cr",
        prompt=r"(?:[#$]|login:|[Pp]assword:)\s?$",
        timeout=10.0,
        description="Linux / OpenWrt / BusyBox console: CR; waits for the shell or login prompt.",
    ),
    "uboot": dict(
        eol="cr",
        prompt=r"(?:=>|U-Boot>)\s?$",
        timeout=10.0,
        description="U-Boot: CR (never CRLF: an empty line repeats the last command); "
                    "waits for the '=>' prompt.",
    ),
    "binary": dict(
        eol="none", display="hex", input="hex", split_lines=False, frame_gap=0.02,
        timeout=2.0, idle=0.1,
        description="Binary protocols: hex in and out; frames split on 20 ms gaps.",
    ),
    "modbus": dict(
        eol="none", display="hex", input="hex", split_lines=False, frame_gap=0.02,
        timeout=2.0, idle=0.1, crc="modbus",
        description="Modbus RTU: like binary, plus CRC16 appended to sends and checked on received frames.",
    ),
}


def profile_settings(name: str) -> dict:
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; choose from {', '.join(PROFILES)}")
    settings = dict(_BASE)
    settings.update({k: v for k, v in PROFILES[name].items() if k != "description"})
    settings["profile"] = name
    return settings
