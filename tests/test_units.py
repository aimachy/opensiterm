import json
import re
from pathlib import Path

import pytest

from opensiterm import __version__, render, util
from opensiterm.rules import RuleSet, build_rules
from opensiterm.timeline import EVT, RX, TX, Timeline


# --- util ------------------------------------------------------------------------

def test_crc_vectors():
    assert util.crc_bytes(bytes.fromhex("01030000000A"), "modbus") == bytes.fromhex("C5CD")
    assert util.crc_bytes(b"123456789", "ccitt") == bytes.fromhex("29B1")
    assert util.crc_bytes(b"123456789", "xmodem") == bytes.fromhex("31C3")
    assert util.crc_bytes(b"123456789", "crc8") == bytes.fromhex("F4")
    ok, _ = util.check_crc(bytes.fromhex("01030000000AC5CD"), "modbus")
    assert ok
    ok, expected = util.check_crc(bytes.fromhex("01030000000AC5CE"), "modbus")
    assert not ok and expected == bytes.fromhex("C5CD")


def test_parse_hex_and_escapes():
    assert util.parse_hex("01 03 0x0A,ff") == b"\x01\x03\x0a\xff"
    assert util.parse_hex("01030A") == b"\x01\x03\x0a"
    with pytest.raises(ValueError):
        util.parse_hex("0")
    assert util.parse_escapes(r"a\r\n\x03\\b") == b"a\r\n\x03\\b"
    assert util.parse_escapes("中") == "中".encode()


def test_clean_text_strips_ansi_and_escapes_controls():
    assert util.clean_text("\x1b[32mOK\x1b[0m\x07") == "OK\\x07"
    assert util.clean_text("\x1b[32mOK", keep_ansi=True) == "\x1b[32mOK"


def test_utf8_complete_len():
    s = "ab中".encode()
    assert util.utf8_complete_len(s) == len(s)
    assert util.utf8_complete_len(s[:-1]) == 2
    assert util.utf8_complete_len(s[:-2]) == 2


def test_session_names():
    assert util.session_name_for("COM7") == "COM7"
    assert util.session_name_for("/dev/ttyUSB0") == "ttyUSB0"
    assert util.session_name_for("socket://10.0.0.5:4000") == "socket_10.0.0.5_4000"


# --- timeline --------------------------------------------------------------------

def test_lines_split_across_chunks_and_crlf():
    closed = []
    tl = Timeline(on_close=closed.append)
    tl.add_rx(b"hel")
    tl.add_rx(b"lo\r")
    tl.add_rx(b"\nworld\n> ")
    texts = [bytes(e.data) for e in tl.entries]
    assert texts == [b"hello\r\n", b"world\n", b"> "]
    assert [bytes(e.data) for e in closed] == [b"hello\r\n", b"world\n"]
    assert tl.entries[-1].closed is False


def test_cursor_reads_only_new_data():
    tl = Timeline()
    tl.add_rx(b"one\n")
    chunks, pos, dropped = tl.snapshot(0)
    assert [c.data for c in chunks] == [b"one\n"] and dropped == 0
    tl.add_rx(b"two\n")
    chunks, pos2, _ = tl.snapshot(pos)
    assert [c.data for c in chunks] == [b"two\n"]
    assert tl.snapshot(pos2)[0] == []


def test_partial_line_continuation_and_utf8_hold_back():
    tl = Timeline()
    data = "进度".encode()
    tl.add_rx(data[:4])  # ends inside the second character
    chunks, pos, _ = tl.snapshot(0, utf8_safe=True)
    assert chunks[0].data == data[:3] and chunks[0].partial
    tl.add_rx(data[4:] + b"\n")
    chunks, _, _ = tl.snapshot(pos, utf8_safe=True)
    assert chunks[0].cont and chunks[0].data == data[3:] + b"\n"


def test_tx_closes_open_line_and_events():
    tl = Timeline()
    tl.add_rx(b"uart:~$ ")
    tx = tl.add_tx(b"help\r", "ai")
    tl.add_event("hello")
    kinds = [e.kind for e in tl.entries]
    assert kinds == [RX, TX, EVT]
    assert tl.entries[0].closed and tx.end == tl.entries[2].pos


def test_binary_frames_split_on_gap():
    tl = Timeline(split_lines=False, frame_gap=0.01)
    tl.add_rx(b"\x01\x0a\x02", ts=100.0)
    tl.add_rx(b"\x03", ts=100.005)
    tl.add_rx(b"\x04", ts=100.1)
    assert [bytes(e.data) for e in tl.entries] == [b"\x01\x0a\x02\x03", b"\x04"]


def test_trim_reports_dropped_bytes():
    tl = Timeline(max_bytes=100)
    for i in range(50):
        tl.add_rx(b"line %02d\n" % i)
    chunks, _, dropped = tl.snapshot(0)
    assert dropped > 0 and tl.first_pos > 0
    assert chunks[-1].data == b"line 49\n"


# --- render ----------------------------------------------------------------------

def test_render_and_limit():
    tl = Timeline()
    for i in range(300):
        tl.add_rx(b"\x1b[1mrow %d\x1b[0m\r\n" % i)
    chunks, _, _ = tl.snapshot(0)
    lines = render.render_chunks(chunks)
    assert lines[0]["text"] == "row 0"
    limited = render.limit_lines(lines, 100, None, "x.log")
    assert len(limited) == 101
    assert "200 lines omitted" in limited[20]["text"] and "x.log" in limited[20]["text"]
    assert limited[-1]["text"] == "row 299"


def test_render_hex_with_crc_check():
    tl = Timeline(split_lines=False)
    tl.add_rx(bytes.fromhex("01030000000AC5CD"))
    tl.flush()
    chunks, _, _ = tl.snapshot(0)
    (line,) = render.render_chunks(chunks, display="hex", crc="modbus")
    assert line["text"] == "01 03 00 00 00 0A C5 CD  [modbus ok]"


def test_format_line_markers():
    base = {"ts": 0.0, "cont": False, "partial": False}
    assert render.format_line(dict(base, kind=TX, source="ai", text="ls"), ts=False) == ">> ls"
    assert render.format_line(dict(base, kind=TX, source="human", text="ls"), ts=False) == ">> (human) ls"
    assert render.format_line(dict(base, kind=EVT, source=None, text="x"), ts=False) == "## x"


# --- rules -----------------------------------------------------------------------

@pytest.mark.parametrize("cmd,rule", [
    ("sf erase 0 100000", "flash-erase"),
    ("nand erase.chip", "flash-erase"),
    ("espefuse.py burn_efuse FLASH_CRYPT_CNT", "efuse"),
    ("efuse burn 0 1", "efuse"),
    ("mkfs.ext4 /dev/mmcblk0p2", "format"),
    ("dd if=fw.bin of=/dev/mtdblock0", "flash-write"),
    ("rm -rf /", "rm-root"),
    ("AT+CPIN=1234", "sim-lock"),
    ("env default -a", "factory-reset"),
])
def test_builtin_rules_block(cmd, rule):
    rules = build_rules({})
    hits = [r.name for r in rules if r.pattern.search(cmd)]
    assert rule in hits


@pytest.mark.parametrize("cmd", ["help", "ls -la /", "AT+CPIN?", "printenv", "md 0x80000000",
                                 "rm -rf build", "efuse read", "sf probe", "version"])
def test_builtin_rules_allow_normal_commands(cmd):
    assert not [r.name for r in build_rules({}) if r.pattern.search(cmd)]


def test_project_rules_and_disable(tmp_path):
    (tmp_path / ".opensiterm").mkdir()
    (tmp_path / ".opensiterm" / "config.toml").write_text(
        '[safety]\ndisable = ["format"]\n\n[[rules]]\nname = "motor"\npattern = \'\\bmotor\\s+start\\b\'\n'
        'action = "deny"\nreason = "fixture"\n', encoding="utf-8")
    rs = RuleSet(tmp_path)
    assert rs.error is None
    assert rs.check("MOTOR start").name == "motor"
    assert rs.check("MOTOR start").action == "deny"
    assert rs.check("mkfs.vfat /dev/sda1") is None
    assert rs.check("sf erase 0 10").name == "flash-erase"


def test_packaging_manifests_are_consistent():
    root = Path(__file__).resolve().parent.parent
    gemini = json.loads((root / "gemini-extension.json").read_text(encoding="utf-8"))
    assert gemini["name"] == "opensiterm" and gemini["version"] == __version__
    assert gemini["mcpServers"]["opensiterm"] == {"command": "opensiterm", "args": ["mcp"]}
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert market["plugins"][0]["name"] == plugin["name"] == "opensiterm"
    skill = (root / "skills" / "opensiterm" / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"^name: opensiterm$", skill, re.M)


def test_broken_config_keeps_builtins(tmp_path):
    (tmp_path / ".opensiterm").mkdir()
    (tmp_path / ".opensiterm" / "config.toml").write_text("[[rules]]\npattern = '('\n", encoding="utf-8")
    rs = RuleSet(tmp_path)
    assert rs.error
    assert rs.check("sf erase 0 10") is not None
