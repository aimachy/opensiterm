"""End-to-end tests against pyserial's loop:// port (everything sent comes straight back)."""

import json
import os
import subprocess
import sys

import pytest

from opensiterm import api, client
from opensiterm.client import OpenSiTermError


def test_open_send_read_close(env):
    r = api.open_session("loop://", name="lp")
    assert "opened loop://" in r.text
    assert (env / ".opensiterm" / ".gitignore").exists()

    r = api.send("hello 世界", "lp")
    assert r.code == 0
    lines = r.text.splitlines()
    assert lines[0].endswith(">> hello 世界") and lines[1].endswith(" hello 世界")
    assert r.data["done"] == "idle"

    # The reply was consumed by send, so read has nothing new.
    assert api.read("lp").text == "(no new output)"

    st = api.status("lp").data["sessions"][0]
    assert st["state"] == "open" and st["tx_bytes"] == len("hello 世界".encode()) + 1

    log = st["log"]
    r = api.close("lp")
    assert "closed 'lp'" in r.text
    content = open(log, encoding="utf-8").read()
    assert ">> hello 世界" in content and "## session closed" in content
    with pytest.raises(OpenSiTermError) as e:
        api.read("lp")
    assert e.value.code == "no_session"


def test_open_is_idempotent_and_updates_settings(env):
    api.open_session("loop://", name="lp")
    r = api.open_session("loop://", name="lp", baud=9600, eol="crlf")
    assert r.data["already_open"]
    st = r.data["session"]
    assert st["baud"] == 9600 and st["settings"]["eol"] == "crlf"


def test_expect_and_prompt_detection(env):
    api.open_session("loop://", name="lp", prompt=r"done>\s?$")
    # A line that merely echoes the command must not count as the prompt...
    r = api.send("done> ", "lp", eol="none", timeout=3)
    assert r.data["done"] == "quiet"
    # ...but a prompt printed after the command's output does.
    r = api.send(r"work\r\ndone> ", "lp", eol="none", escapes=True)
    assert r.data["done"] == "prompt"
    r = api.send("ready OK", "lp", expect=r"\bOK\b")
    assert r.data["done"] == "expect" and r.code == 0
    r = api.send("nope", "lp", expect="NEVER", timeout=0.5)
    assert r.code == 3 and "timeout" in r.text


def test_read_wait_and_tail_grep(env):
    api.open_session("loop://", name="lp")
    api.send("alpha", "lp", wait=False)
    api.send("beta", "lp", wait=False)
    r = api.read("lp", wait=1)
    assert "alpha" in r.text and "beta" in r.text
    r = api.tail("lp", lines=10, grep="bet")
    assert "beta" in r.text and "alpha" not in r.text


def test_safety_rules_block_and_unsafe_send(env):
    api.open_session("loop://", name="lp")
    with pytest.raises(OpenSiTermError) as e:
        api.send("sf erase 0 100000", "lp")
    assert e.value.code == "blocked" and e.value.exit_code == 4
    with pytest.raises(OpenSiTermError):
        api.send("sf erase 0 100000", "lp", unsafe=True)  # needs a reason
    r = api.send("sf erase 0 100000", "lp", unsafe=True, reason="user asked to wipe the bootloader area")
    assert ">> (unsafe) sf erase" in r.text
    audit = (env / ".opensiterm" / "logs" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    actions = [json.loads(line)["action"] for line in audit]
    assert actions == ["blocked", "sent"]


def test_hex_and_modbus_profile(env):
    api.open_session("loop://", name="mb", profile="modbus")
    r = api.send("01 03 00 00 00 0A", "mb")
    assert "01 03 00 00 00 0A C5 CD  [modbus ok]" in r.text


def test_pause_resume_and_control(env):
    api.open_session("loop://", name="lp")
    assert api.pause("lp").data["state"] == "paused"
    with pytest.raises(OpenSiTermError):
        api.send("x", "lp")
    assert api.resume("lp").data["resumed"]
    r = api.control("lp", dtr=False, rts=True)
    assert r.data == {"dtr": False, "rts": True}
    assert "pulsed RTS" in api.reset("lp").text


def test_cli_exit_codes_and_json(env):
    def cli(*args):
        return subprocess.run([sys.executable, "-m", "opensiterm", *args], capture_output=True, text=True,
                              encoding="utf-8", env=os.environ.copy())

    assert cli("open", "loop://", "--name", "lp").returncode == 0
    p = cli("send", "hi", "--json")
    assert p.returncode == 0 and json.loads(p.stdout)["done"] == "idle"
    assert cli("send", "mkfs.ext4 /dev/sda1").returncode == 4
    assert cli("expect", "zzz", "-t", "0.3").returncode == 3
    assert cli("rules", "--check", "efuse burn 1").returncode == 4
    assert cli("-s", "nope", "read").returncode == 5
    assert cli("close", "--all").returncode == 0


def test_mcp_roundtrip(env):
    proc = subprocess.Popen([sys.executable, "-m", "opensiterm", "mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env=os.environ.copy())

    def rpc(mid, method, params=None):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n").encode())
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
        assert init["result"]["protocolVersion"] == "2025-06-18"
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        tools = {t["name"] for t in rpc(2, "tools/list")["result"]["tools"]}
        assert {"serial_open", "serial_send", "serial_unsafe_send", "serial_read"} <= tools
        r = rpc(3, "tools/call", {"name": "serial_open", "arguments": {"port": "loop://", "name": "lp"}})
        assert not r["result"]["isError"], r
        r = rpc(4, "tools/call", {"name": "serial_send", "arguments": {"text": "ping"}})
        assert "ping" in r["result"]["content"][0]["text"] and not r["result"]["isError"]
        r = rpc(5, "tools/call", {"name": "serial_send", "arguments": {"text": "nand erase.chip"}})
        assert r["result"]["isError"] and "BLOCKED" in r["result"]["content"][0]["text"]
        assert rpc(6, "nope")["error"]["code"] == -32601
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
