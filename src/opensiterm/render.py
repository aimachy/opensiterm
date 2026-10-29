"""Turn timeline chunks into lines an LLM (or a human) can read."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

from .timeline import EVT, RX, TX, Chunk
from .util import check_crc, clean_text, decode, fmt_datetime, fmt_time, hexdump

_SPLIT = re.compile(r"\r\n|\n|\r")
_TRAILING_EOL = re.compile(r"(?:\r\n|\n|\r)$")


def _tx_text(data: bytes, encoding: str) -> str:
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return "[hex] " + hexdump(data)
    body = _TRAILING_EOL.sub("", text) or text  # a bare line ending ("press Enter") stays visible
    cleaned = clean_text(body.replace("\r", "\\r").replace("\n", "\\n"))
    if body and cleaned.count("\\x") * 4 > len(cleaned) // 2:
        return "[hex] " + hexdump(data)  # mostly binary
    return cleaned


def render_chunks(chunks: Iterable[Chunk], *, encoding: str = "utf-8", display: str = "text",
                  keep_ansi: bool = False, crc: Optional[str] = None) -> List[dict]:
    """One dict per display line: ts, kind, text, source, cont, partial."""
    lines: List[dict] = []
    for c in chunks:
        base = {"ts": c.ts, "kind": c.kind, "source": c.source, "cont": c.cont, "partial": c.partial}
        if c.kind == RX:
            if display == "hex":
                text = hexdump(c.data)
                if crc and not c.cont and not c.partial:
                    res = check_crc(c.data, crc)
                    if res is not None:
                        text += f"  [{crc} ok]" if res[0] else f"  [{crc} BAD, expected {hexdump(res[1])}]"
                lines.append(dict(base, text=text))
                continue
            text = decode(c.data, encoding)
            if not c.partial:
                text = _TRAILING_EOL.sub("", text)
            parts = _SPLIT.split(text)
            for k, part in enumerate(parts):
                lines.append(dict(base, text=clean_text(part, keep_ansi), cont=c.cont and k == 0,
                                  partial=c.partial and k == len(parts) - 1))
        elif c.kind == TX:
            text = ("[hex] " + hexdump(c.data)) if display == "hex" else _tx_text(c.data, encoding)
            lines.append(dict(base, text=text))
        else:
            lines.append(dict(base, text=c.data.decode("utf-8", "replace")))
    return lines


def marker(line: dict) -> str:
    if line["kind"] == TX:
        src = line.get("source")
        return ">> " if src in (None, "ai") else f">> ({src}) "
    if line["kind"] == EVT:
        return "## "
    return ""


def format_line(line: dict, ts: bool = True, full_date: bool = False) -> str:
    prefix = ""
    if ts:
        prefix = (fmt_datetime if full_date else fmt_time)(line["ts"]) + " "
    return (prefix + marker(line) + line["text"]).rstrip()


def format_lines(lines: List[dict], ts: bool = True) -> str:
    return "\n".join(format_line(line, ts) for line in lines)


def limit_lines(lines: List[dict], max_lines: Optional[int], max_chars: Optional[int],
                log_path: Optional[str] = None) -> List[dict]:
    """Cap line count (keep head 20% + tail 80%) and line length, to protect the LLM's context."""
    if max_chars:
        for line in lines:
            t = line["text"]
            if len(t) > max_chars:
                line["text"] = t[:max_chars] + f" ...[+{len(t) - max_chars} chars]"
    if max_lines and len(lines) > max_lines:
        head = max(1, max_lines // 5)
        tail = max(1, max_lines - head)
        omitted = len(lines) - head - tail
        where = f"; full log: {log_path}" if log_path else ""
        note = {"ts": lines[head]["ts"], "kind": EVT, "source": None, "cont": False, "partial": False,
                "text": f"... {omitted} lines omitted{where} ..."}
        lines = lines[:head] + [note] + lines[-tail:]
    return lines
