"""Ordered record of everything that crossed the serial port.

Every byte gets a global position. Received bytes are grouped into entries
(one per line, or one per frame in binary mode); each write and each event is
an entry of its own. Readers keep a position cursor, so "what's new since I
last looked" is simply "everything after my cursor".
"""

from __future__ import annotations

import bisect
import re
import threading
import time
from typing import Callable, List, NamedTuple, Optional, Tuple

from .util import utf8_complete_len

RX, TX, EVT = "rx", "tx", "evt"
_EOL = re.compile(rb"[\r\n]")


class Entry:
    __slots__ = ("pos", "ts", "last_ts", "kind", "data", "closed", "source")

    def __init__(self, pos: int, ts: float, kind: str, source: Optional[str] = None):
        self.pos = pos
        self.ts = ts
        self.last_ts = ts
        self.kind = kind
        self.data = bytearray()
        self.closed = False
        self.source = source

    @property
    def end(self) -> int:
        return self.pos + len(self.data)


class Chunk(NamedTuple):
    """Immutable copy of (part of) an entry, safe to render outside the lock."""
    ts: float
    kind: str
    data: bytes
    source: Optional[str]
    cont: bool      # starts in the middle of its entry
    partial: bool   # the entry is still open (line not terminated yet)


class Timeline:
    def __init__(self, *, max_bytes: int = 8 << 20, max_line: int = 4096,
                 split_lines: bool = True, frame_gap: Optional[float] = None,
                 on_close: Optional[Callable[[Entry], None]] = None):
        self.max_bytes = max_bytes
        self.max_line = max_line
        self.split_lines = split_lines
        self.frame_gap = frame_gap
        self.on_close = on_close
        self.entries: List[Entry] = []
        self._starts: List[int] = []
        self.end_pos = 0
        self._size = 0
        self._cr_pending = False
        self.cond = threading.Condition()  # RLock-based, so methods may nest

    @property
    def first_pos(self) -> int:
        return self.entries[0].pos if self.entries else self.end_pos

    # --- writers -------------------------------------------------------------

    def add_rx(self, data: bytes, ts: Optional[float] = None) -> None:
        if not data:
            return
        ts = time.time() if ts is None else ts
        with self.cond:
            i, n = 0, len(data)
            if self._cr_pending:
                # "\r" ended the previous chunk; a "\n" now belongs to that same line.
                self._cr_pending = False
                last = self.entries[-1] if self.entries else None
                if data[0] == 0x0A and last is not None and last.kind == RX:
                    self._append(last, data[:1], ts)
                    i = 1
            while i < n:
                e = self._open_rx()
                if e is not None and self.frame_gap is not None and ts - e.last_ts > self.frame_gap:
                    self._close(e)
                    e = None
                if e is None:
                    e = self._new(RX, ts)
                stop = min(n, i + self.max_line - len(e.data))
                m = _EOL.search(data, i, stop) if self.split_lines else None
                if m:
                    j = m.end()
                    if data[j - 1] == 0x0D:
                        if j < n and data[j] == 0x0A:
                            j += 1
                        elif j >= n:
                            self._cr_pending = True
                    self._append(e, data[i:j], ts)
                    self._close(e)
                    i = j
                    continue
                self._append(e, data[i:stop], ts)
                i = stop
                if len(e.data) >= self.max_line:
                    self._close(e)
            self._trim()
            self.cond.notify_all()

    def add_tx(self, data: bytes, source: Optional[str] = None, ts: Optional[float] = None) -> Entry:
        with self.cond:
            e = self._add_whole(TX, data, source, ts)
            self.cond.notify_all()
            return e

    def add_event(self, text: str, ts: Optional[float] = None) -> Entry:
        with self.cond:
            e = self._add_whole(EVT, text.encode("utf-8"), None, ts)
            self.cond.notify_all()
            return e

    def tick(self, now: Optional[float] = None) -> None:
        """Close a binary frame once the line has been quiet for frame_gap."""
        if self.frame_gap is None:
            return
        now = time.time() if now is None else now
        with self.cond:
            e = self._open_rx()
            if e is not None and now - e.last_ts > self.frame_gap:
                self._close(e)

    def flush(self) -> None:
        """Close any open line (used at shutdown so it reaches the log)."""
        with self.cond:
            e = self._open_rx()
            if e is not None:
                self._close(e)

    # --- readers -------------------------------------------------------------

    def snapshot(self, pos: int, utf8_safe: bool = False) -> Tuple[List[Chunk], int, int]:
        """Chunks after `pos`, the new cursor, and how many bytes were lost to trimming."""
        with self.cond:
            dropped = 0
            if pos < self.first_pos:
                dropped = self.first_pos - pos
                pos = self.first_pos
            pos = min(pos, self.end_pos)
            idx = max(0, bisect.bisect_right(self._starts, pos) - 1)
            chunks: List[Chunk] = []
            new_pos = pos
            for e in self.entries[idx:]:
                off = pos - e.pos if pos > e.pos else 0
                data = bytes(e.data[off:])
                if not data:
                    continue
                if utf8_safe and not e.closed and e.kind == RX:
                    # Don't hand out half a multi-byte character; it arrives with the next read.
                    data = data[:utf8_complete_len(data)]
                    if not data:
                        break
                chunks.append(Chunk(e.ts, e.kind, data, e.source, off > 0, not e.closed))
                new_pos = e.pos + off + len(data)
            return chunks, new_pos, dropped

    def all_entries(self) -> List[Entry]:
        with self.cond:
            return list(self.entries)

    # --- internals -----------------------------------------------------------

    def _open_rx(self) -> Optional[Entry]:
        if self.entries:
            e = self.entries[-1]
            if e.kind == RX and not e.closed:
                return e
        return None

    def _new(self, kind: str, ts: float, source: Optional[str] = None) -> Entry:
        e = Entry(self.end_pos, ts, kind, source)
        self.entries.append(e)
        self._starts.append(e.pos)
        return e

    def _append(self, e: Entry, data: bytes, ts: float) -> None:
        e.data += data
        e.last_ts = ts
        self.end_pos += len(data)
        self._size += len(data)

    def _close(self, e: Entry) -> None:
        if not e.closed:
            e.closed = True
            if self.on_close is not None:
                try:
                    self.on_close(e)
                except Exception:
                    pass

    def _add_whole(self, kind: str, data: bytes, source: Optional[str], ts: Optional[float]) -> Entry:
        ts = time.time() if ts is None else ts
        open_rx = self._open_rx()
        if open_rx is not None:
            self._close(open_rx)
        self._cr_pending = False
        e = self._new(kind, ts, source)
        self._append(e, data, ts)
        self._close(e)
        self._trim()
        return e

    def _trim(self) -> None:
        if self._size <= self.max_bytes:
            return
        target = self.max_bytes * 0.8
        k, size = 0, self._size
        while k < len(self.entries) - 1 and size > target:
            size -= len(self.entries[k].data)
            k += 1
        del self.entries[:k]
        del self._starts[:k]
        self._size = size
