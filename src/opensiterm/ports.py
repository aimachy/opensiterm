"""Serial port discovery and selection."""

from __future__ import annotations

import sys
from typing import List

from serial.tools import list_ports

# (vid, pid) -> what it usually is. pid None matches any product of that vendor.
KNOWN_USB = {
    (0x1A86, 0x7523): "CH340", (0x1A86, 0x7522): "CH340K", (0x1A86, 0x55D3): "CH343",
    (0x1A86, 0x55D4): "CH9102", (0x1A86, 0x5523): "CH341",
    (0x10C4, 0xEA60): "CP210x", (0x10C4, 0xEA70): "CP2105",
    (0x0403, 0x6001): "FT232R", (0x0403, 0x6010): "FT2232", (0x0403, 0x6011): "FT4232",
    (0x0403, 0x6014): "FT232H", (0x0403, 0x6015): "FT231X",
    (0x067B, 0x2303): "PL2303",
    (0x303A, 0x1001): "Espressif USB-Serial/JTAG (ESP32-S3/C3/C6)",
    (0x0483, 0x5740): "STM32 USB CDC", (0x0483, 0x374B): "ST-LINK/V2-1 VCP",
    (0x0483, 0x374E): "ST-LINK/V3 VCP", (0x0483, 0x374F): "ST-LINK/V3 VCP", (0x0483, 0x3753): "ST-LINK/V3 VCP",
    (0x2E8A, 0x000A): "Raspberry Pi Pico (RP2040) CDC", (0x2E8A, 0x0005): "MicroPython board",
    (0x0D28, 0x0204): "DAPLink VCP",
    (0x2341, None): "Arduino", (0x2A03, None): "Arduino", (0x1366, None): "SEGGER J-Link VCP",
    (0x239A, None): "Adafruit board", (0x2886, None): "Seeed board", (0x1915, None): "Nordic board",
    (0x303A, None): "Espressif board",
}


def chip_name(vid, pid) -> str:
    if vid is None:
        return ""
    return KNOWN_USB.get((vid, pid)) or KNOWN_USB.get((vid, None)) or ""


def list_serial_ports() -> List[dict]:
    ports = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        ports.append({
            "device": p.device,
            "description": p.description if p.description != "n/a" else "",
            "hwid": p.hwid,
            "vid": p.vid,
            "pid": p.pid,
            "serial_number": p.serial_number,
            "manufacturer": p.manufacturer,
            "location": p.location,
            "chip": chip_name(p.vid, p.pid),
            "usb": p.vid is not None,
        })
    if sys.platform == "darwin":
        # /dev/tty.* blocks waiting for carrier detect; /dev/cu.* is the one to use.
        cu = {p["device"][len("/dev/cu."):] for p in ports if p["device"].startswith("/dev/cu.")}
        ports = [p for p in ports if not (p["device"].startswith("/dev/tty.") and p["device"][len("/dev/tty."):] in cu)]
    return ports


def _haystack(p: dict) -> str:
    fields = (p["device"], p["description"], p["hwid"], p["serial_number"], p["manufacturer"], p["chip"])
    return " ".join(f for f in fields if f).lower()


def find_port(match: str) -> str:
    """The one port whose name/description/hwid/serial/chip contains `match`."""
    ports = list_serial_ports()
    hits = [p for p in ports if match.lower() in _haystack(p)]
    if len(hits) == 1:
        return hits[0]["device"]
    if not hits:
        raise LookupError(f"no serial port matches {match!r}; available: {describe(ports)}")
    raise LookupError(f"{len(hits)} ports match {match!r} ({describe(hits)}); be more specific")


def auto_port() -> str:
    """The only USB serial port, if there is exactly one."""
    ports = list_serial_ports()
    usb = [p for p in ports if p["usb"]]
    if len(usb) == 1:
        return usb[0]["device"]
    if not usb:
        raise LookupError(f"no USB serial port found; available: {describe(ports)}")
    raise LookupError(f"several USB serial ports ({describe(usb)}); name one explicitly")


def describe(ports: List[dict]) -> str:
    if not ports:
        return "(none)"
    return ", ".join(f"{p['device']} [{p['chip'] or p['description'] or '?'}]" for p in ports)
