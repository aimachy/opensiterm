"""Small helpers shared by the daemon, CLI and MCP server."""

from __future__ import annotations

import re
import time

# CSI (colors, cursor moves), OSC (window titles) and other two-byte escapes.
ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])"
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CTRL_NO_ESC_RE = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")


def clean_text(s: str, keep_ansi: bool = False) -> str:
    """Strip ANSI escapes and make other control characters visible as \\xNN."""
    if keep_ansi:
        return _CTRL_NO_ESC_RE.sub(lambda m: "\\x%02x" % ord(m.group()), s)
    s = ANSI_RE.sub("", s)
    return _CTRL_RE.sub(lambda m: "\\x%02x" % ord(m.group()), s)


def decode(data: bytes, encoding: str) -> str:
    return data.decode(encoding, errors="backslashreplace")


def utf8_complete_len(data) -> int:
    """Length of the longest prefix of data that doesn't end inside a UTF-8 sequence."""
    n = len(data)
    for back in range(1, min(4, n) + 1):
        c = data[n - back]
        if c & 0xC0 == 0x80:
            continue  # continuation byte, keep looking for the lead byte
        if c >= 0xC0:
            need = 2 if c < 0xE0 else 3 if c < 0xF0 else 4
            return n - back if back < need else n
        return n
    return n


_ESCAPES = {
    "n": b"\n", "r": b"\r", "t": b"\t", "0": b"\x00", "e": b"\x1b",
    "a": b"\x07", "b": b"\x08", "\\": b"\\",
}


def parse_escapes(text: str, encoding: str = "utf-8") -> bytes:
    r"""Encode text, interpreting \r \n \t \0 \e \a \b \\ and \xHH."""
    out = bytearray()
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            if n == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[i + 2:i + 4]):
                out.append(int(text[i + 2:i + 4], 16))
                i += 4
                continue
            if n in _ESCAPES:
                out += _ESCAPES[n]
                i += 2
                continue
        out += c.encode(encoding)
        i += 1
    return bytes(out)


def parse_hex(s: str) -> bytes:
    """Accept '01 03 00 0A', '0x01,0x03', '01030A' and similar."""
    compact = re.sub(r"0[xX]", "", s)
    compact = re.sub(r"[\s,;:\-]", "", compact)
    if not compact:
        raise ValueError("empty hex string")
    if len(compact) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", compact):
        raise ValueError(f"invalid hex: {s!r} (use pairs of hex digits, e.g. '01 03 00 0A')")
    return bytes.fromhex(compact)


def hexdump(data: bytes) -> str:
    return " ".join("%02X" % b for b in data)


# --- checksums -------------------------------------------------------------
# LLMs are bad at computing CRCs by hand, so the tool does it for them.

def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _crc16_msb(data: bytes, init: int) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _xor8(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


# name -> (function, width in bytes, byte order of the appended checksum)
CRCS = {
    "modbus": (_crc16_modbus, 2, "little"),
    "ccitt": (lambda d: _crc16_msb(d, 0xFFFF), 2, "big"),
    "xmodem": (lambda d: _crc16_msb(d, 0x0000), 2, "big"),
    "crc8": (_crc8, 1, "big"),
    "sum8": (lambda d: sum(d) & 0xFF, 1, "big"),
    "xor8": (_xor8, 1, "big"),
}


def crc_bytes(data: bytes, name: str) -> bytes:
    try:
        fn, width, order = CRCS[name]
    except KeyError:
        raise ValueError(f"unknown checksum {name!r}; choose from {', '.join(CRCS)}") from None
    return fn(data).to_bytes(width, order)


def check_crc(frame: bytes, name: str):
    """Return (ok, expected_checksum_bytes), or None if the frame is too short."""
    width = CRCS[name][1]
    if len(frame) <= width:
        return None
    expected = crc_bytes(frame[:-width], name)
    return frame[-width:] == expected, expected


# --- misc --------------------------------------------------------------------

def session_name_for(port: str) -> str:
    """COM7 -> COM7, /dev/ttyUSB0 -> ttyUSB0, socket://host:4000 -> socket_host_4000."""
    if "://" in port:
        base = port.replace("://", "_")
    else:
        base = re.split(r"[\\/]", port)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("_.") or "serial"


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) + ".%03d" % (int(ts * 1000) % 1000)


def fmt_datetime(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) + ".%03d" % (int(ts * 1000) % 1000)
