# OpenSiTerm

**Serial port access for AI coding agents.** Let Claude Code, Codex, opencode, oh-my-pi, Gemini CLI,
DeepSeek and other agents read your device's logs and type into its console themselves, instead of
you copy-pasting serial output into the chat.

[中文文档](README.zh-CN.md)

```text
you   > The board reboots every few minutes. Find out why.
agent > $ opensiterm open COM7 --profile linux
        $ opensiterm expect "Rebooting|watchdog|Kernel panic" -t 600
        $ opensiterm tail -n 80
        The kernel hits an OOM in `sensord` 2 s before the watchdog fires; `free` shows ...
```

## Why a background session?

When an agent runs a shell command, that command is a fresh process. If each command opened the port
itself:

- everything the device printed **between** commands would be lost, including the crash log you need;
- opening the port toggles DTR/RTS, which **resets** Arduino and ESP32 boards every time;
- a port can only be open in one program, so the second open fails.

OpenSiTerm keeps **one background session per port**. The session holds the port, timestamps
everything into memory and a log file, and reconnects when a USB device re-enumerates. Agents reach
it through a small CLI, an MCP server or an Agent Skill:

```text
Claude Code / Codex / opencode ──shell──► opensiterm CLI ──┐
MCP clients (Codex, chat apps) ──MCP────► opensiterm mcp ──┼─ 127.0.0.1 + token ─► session ──► COM7 / ttyUSB0 / socket://
you, in a terminal ─────────────────────► opensiterm watch ┘                          ├─ timestamped history (8 MB)
                                                                                      ├─ .opensiterm/logs/<port>-<time>.log
                                                                                      └─ .opensiterm/logs/audit.jsonl
```

## Features

- **Nothing gets lost.** Every byte is timestamped. `read` returns only what's new since your last
  read. `tail --grep` and the log file cover older output.
- **Replies come back complete.** `send` waits for the device's prompt, an `--expect` regex, or
  silence, so the agent doesn't have to guess `sleep` times.
- **Small outputs.** ANSI colors are stripped and control bytes are shown as `\xNN`. Long output is
  cut down to head + tail, with a pointer to the full log.
- **Device profiles** for MCU shells, AT modems, Linux, U-Boot, binary protocols and Modbus RTU.
  Hex in/out, frames split on time gaps, and CRCs computed and verified by the tool (`modbus`,
  `ccitt`, `xmodem`, `crc8`, `sum8`, `xor8`), because LLMs get CRCs wrong.
- **Hardware workflow.** DTR/RTS reset pulses, break signals, and `exec -- <flasher>`, which
  releases the port, flashes, re-acquires the port and captures the boot log. It reconnects
  automatically after a USB re-enumeration.
- **Safety rules.** Irreversible commands are blocked by default: eFuse/OTP, flash erase, `mkfs`,
  `dd` to `/dev`, factory reset, SIM PIN and others. Add your own rules per project. Everything sent
  is audited.
- **Humans stay in the loop.** `opensiterm watch` shows the session live and lets you type to the
  device. It replaces your serial terminal, which can't open the port while the session holds it.
- **Cross-platform.** Windows, macOS and Linux. Network serial (ser2net / RFC 2217) works through
  `socket://` and `rfc2217://`. The only dependency is pyserial.

## Who is it for?

**In short: anyone who debugs devices over a serial port can let an AI read the logs and type the
commands.**

### Main users

| Who | Typical work | How OpenSiTerm helps |
|---|---|---|
| **MCU / embedded firmware engineers**<br>(STM32, ESP32, GD32, nRF, RT-Thread, FreeRTOS, Zephyr) | The edit, flash, read-logs loop; making sense of HardFault and assert dumps | The agent flashes its own changes, catches the boot log and judges the result, so you stop pasting logs; crash output is kept in full, with timestamps |
| **Embedded Linux / BSP / driver engineers**<br>(U-Boot, kernel bring-up, Buildroot, Yocto, OpenWrt) | Boards that hang at boot, kernel panics, failed driver probes, U-Boot environment changes | Thousand-line boot logs reach the agent as head + tail, with the full log available to grep; U-Boot and Linux prompts are recognized; dangerous flash writes are blocked by default |
| **Raspberry Pi / dev-board / maker users**<br>(Raspberry Pi, Arduino, ESP32, MicroPython) | Cryptic errors; no idea what the board is doing | Ask in plain language ("why does my board keep rebooting?") and the agent checks the port, queries the device and explains what it found |
| **IoT / cellular-module engineers**<br>(4G/5G, NB-IoT, Wi-Fi, Bluetooth modules) | AT-command debugging: network registration, signal quality, connections | The agent knows the vendors' AT command sets and reads `OK`/`ERROR` itself, working through a back-and-forth; SIM PIN commands that can lock the card are blocked by default |
| **Hardware engineers** | Powering up new boards: is the UART alive, did the system boot? | You don't need the software commands: describe what to check and the agent runs it |
| **Test and production engineers** | Regression runs, overnight soak tests, checking several firmware versions | The agent can watch all night for `watchdog`, `panic` and the like; every line is timestamped and every action audited |
| **Industrial automation engineers**<br>(Modbus RTU, RS-485, PLCs, meters) | Binary protocols, where hand-computed CRCs go wrong | Hex in and out, with CRCs computed and verified by the tool; the agent only writes the frame contents |
| **Network engineers** | Configuring switches and routers through the console port | The agent sends the configuration line by line and checks each echo |
| **FAEs / technical support** | Reproducing customer issues and collecting logs | Debug boards attached to another machine (ser2net / RFC 2217) and hand complete logs to R&D |
| **Teachers and students** | Stuck on "why doesn't the serial port answer?" | The agent explains against the real output: wrong baud rate, wrong line ending, TX/RX swapped |
| **Robotics and drone developers** | Serial protocols of flight controllers, motor drivers and sensors | Binary mode, frames split on time gaps, several ports at once |

### The pain they share

1. **Copying logs is tedious.** Serial output has to be pasted into the chat piece by piece before an
   AI can see it, and the crucial lines are always the ones that get missed.
2. **The port is exclusive.** An agent and your own terminal program can't open the same port.
3. **The AI can't see timing.** When the device rebooted, or how long passed between two lines,
   is lost in pasted text.
4. **Fear of the AI breaking something.** Some mistakes on real hardware can't be undone.

How OpenSiTerm deals with each:

- a background session records everything, so the agent sees all the output itself;
- `watch` lets you and the agent share one port;
- every line carries a millisecond timestamp;
- dangerous commands are blocked by default, and every action is audited.

### Not a good fit

- **It is not a JTAG/SWD debugger.** For breakpoints, single-stepping or reading memory, use OpenOCD,
  J-Link or GDB. OpenSiTerm only handles serial ports.
- **Not supported yet:** CAN bus, J-Link RTT, and signal-level work such as logic analyzers or
  oscilloscopes.
- **Not for high-speed data streams**, such as sustained multi-MB/s acquisition. It is built for
  debugging-scale traffic; the in-memory buffer defaults to 8 MB.
- **Don't let it run production equipment or dangerous machinery unattended.** The safety rules
  catch an agent's slips; they don't replace human supervision.

## Install

**With Python 3.9+** (recommended):

```bash
pipx install opensiterm          # or: uv tool install opensiterm
# straight from GitHub:
pipx install git+https://github.com/aimachy/opensiterm
```

**Without Python:** download the single-file executable for your OS from
[Releases](https://github.com/aimachy/opensiterm/releases), rename it to `opensiterm`
(`opensiterm.exe` on Windows) and put it on your `PATH`. The binaries are not code-signed yet:

- **Windows:** SmartScreen may warn on first run. Click *More info → Run anyway*.
- **macOS:** `xattr -d com.apple.quarantine opensiterm && chmod +x opensiterm`
- **Linux:** `chmod +x opensiterm`. Your user needs to be in the `dialout` group (`uucp` on Arch/Fedora).

Then run **`opensiterm setup`**. It prints the exact commands to connect each AI tool, using the
real path of your installation.

## Connect your AI tool

Most agents need two pieces. The **Skill** teaches an agent with a shell how to use the CLI. The
**MCP server** gives tools to agents that can't, or shouldn't, run shell commands.

```bash
opensiterm skill install   # copies SKILL.md to ~/.claude/skills/ and ~/.agents/skills/
```

| Tool | Skill | MCP server |
|---|---|---|
| **Claude Code** | `opensiterm skill install`, or as a plugin: `/plugin marketplace add aimachy/opensiterm` then `/plugin install opensiterm@opensiterm` | optional: `claude mcp add --transport stdio --scope user opensiterm -- opensiterm mcp` |
| **Codex CLI** | `opensiterm skill install` (reads `~/.agents/skills`) | **required:** `codex mcp add opensiterm -- opensiterm mcp` (see the note below) |
| **opencode** | picked up from `~/.claude/skills` or `~/.agents/skills` | add to `~/.config/opencode/opencode.json`: `{"mcp": {"opensiterm": {"type": "local", "command": ["opensiterm", "mcp"]}}}` |
| **pi** | `pi install git:github.com/aimachy/opensiterm` | not needed: pi has no MCP by design and drives the CLI through the skill |
| **oh-my-pi** | `/marketplace add aimachy/opensiterm`, then `omp plugin install opensiterm@opensiterm` (or picked up from `~/.agents/skills`) | add to `~/.omp/agent/mcp.json`: `{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` |
| **Gemini CLI** | `gemini extensions install https://github.com/aimachy/opensiterm` installs both, as an extension | (included) |
| **Qwen Code** | `qwen extensions install https://github.com/aimachy/opensiterm` (Qwen Code converts the Gemini extension) | (included) |
| **Kimi Code CLI** | picked up from `~/.claude/skills` or `~/.agents/skills` | `kimi mcp add --transport stdio opensiterm -- opensiterm mcp` |
| **Antigravity CLI** (`agy`) | `opensiterm skill install --target antigravity` | add to `~/.gemini/config/mcp_config.json`: `{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` |
| **Crush** | picked up from `~/.agents/skills` or `~/.claude/skills` | in `~/.config/crush/crushrc`: `mcp add opensiterm --command opensiterm --args mcp` |
| **Grok Build** (xAI), **ZCode** (Z.ai), **DeepSeek Harness** (`dsh`) | these support Agent Skills and MCP; point them at `~/.agents/skills` or the MCP entry, per their docs | `{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` or the tool's own equivalent |
| **Chat apps** (Claude Desktop, Cherry Studio, Chatbox, Cursor, ...) | n/a | `{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` |

You can also install the skill with the cross-agent installer:
`npx skills add aimachy/opensiterm`.

> **Models vs. tools:** DeepSeek, Kimi, Qwen, GLM and Grok are *models*; the serial port is driven
> by the *tool* (harness) that runs them. Any model works in any tool above. For example, DeepSeek,
> Kimi K2 and GLM all officially support running inside Claude Code, opencode or Codex, so follow
> that tool's row.

> **Codex users:** Codex's sandbox hides serial devices and blocks localhost for shell commands on
> Linux and macOS, so `opensiterm` shell calls fail inside it. MCP servers run outside the sandbox,
> so register the MCP server; the agent then uses the `serial_*` tools.

> **Claude Code permissions:** to stop the approval prompts for routine use, allow the everyday
> subcommands, but keep `unsafe-send` on "ask". Don't allow `Bash(opensiterm:*)`: that would
> auto-approve `unsafe-send` too.
>
> ```json
> { "permissions": {
>     "allow": ["Bash(opensiterm ports:*)", "Bash(opensiterm status:*)", "Bash(opensiterm open:*)",
>               "Bash(opensiterm read:*)", "Bash(opensiterm tail:*)", "Bash(opensiterm expect:*)",
>               "Bash(opensiterm send:*)"],
>     "ask":   ["Bash(opensiterm unsafe-send:*)", "Bash(opensiterm exec:*)"] } }
> ```

## Quick start

```bash
opensiterm ports                         # COM7  CH340 - USB-SERIAL CH340 (COM7)  [USB 1A86:7523]
opensiterm open COM7 -b 115200 --profile linux
opensiterm read --wait 3                 # whatever the device printed
opensiterm send "uname -a"               # one command, its complete reply
opensiterm expect "login:" -t 60         # wait for a pattern
opensiterm watch                         # (you) live view in another terminal; type to send
opensiterm close
```

Output is compact and timestamped. `>>` marks what was sent and `##` marks an event:

```text
20:34:34.700 >> uname -a
20:34:34.703 uname -a
20:34:34.711 Linux buildroot 5.10.110 #1 SMP armv7l GNU/Linux
20:34:34.712 ~ #
```

## Example: debugging a board with Claude Code

1. **Install and connect** (once):

   ```bash
   uv tool install git+https://github.com/aimachy/opensiterm   # or: pipx install git+https://...
   opensiterm skill install     # skill goes to ~/.claude/skills/ and ~/.agents/skills/
   ```

   If `opensiterm` isn't found, run `uv tool update-shell` (or `pipx ensurepath`) and open a new
   terminal. The executable lives in `~/.local/bin/`; on Windows that's
   `C:\Users\<you>\.local\bin\opensiterm.exe`.

2. **Restart Claude Code** in a new terminal and session, so the new PATH and the skill are loaded.

3. **Free the port.** Close any serial terminal (MobaXterm, PuTTY, ...) connected to it, since a port
   can only be open in one program. To watch along live, run `opensiterm watch` in another terminal.

4. **Just ask in plain language**, for example:

   > The board on COM7 runs Linux at 115200 baud. Check the OS version, memory and disk usage.

   The agent calls `opensiterm open / send / read` itself. Logs go to `.opensiterm/logs/` in the
   project directory you started Claude Code in.

**Keep in mind:**

- The default rules block flash erase, format, `rm -rf /` and the like, but not `reboot` or config
  changes. Tell the agent up front what it may and may not do.
- Claude Code asks you to approve every `opensiterm` command by default. To cut the prompts, allow
  the everyday subcommands as shown under "Claude Code permissions" above; `unsafe-send` keeps asking.
- With Codex, also register the MCP server: `codex mcp add opensiterm -- opensiterm mcp`. Use the
  full path, such as `C:\Users\<you>\.local\bin\opensiterm.exe`, if it isn't on your PATH.

## Commands

| Command | What it does |
|---|---|
| `ports` | List serial ports with chip names (CH340, CP210x, FTDI, ESP32-S3 USB, ST-LINK...) and which ones are open |
| `open [PORT]` | Start a session. PORT can be `COM7`, `/dev/ttyUSB0`, `auto`, `socket://host:port` or `rfc2217://...`; `--match TEXT` picks a port by description or USB id. Idempotent |
| `read [--wait S]` | Output since your last read |
| `send TEXT` | Send a line and print the reply. Options: `--expect RE`, `--hex`, `--crc`, `--ctrl c`, `-e` (escapes), `--eol`, `--no-wait` |
| `expect RE [-t S]` | Wait for a regex in new output |
| `tail [-n N] [--grep RE]` | Recent history; doesn't move the read cursor |
| `watch` | Live view for humans; type to send (`/help` lists local commands) |
| `reset [--line rts\|dtr] [--wait S]` | Pulse a control line to reset the board, then collect the boot log |
| `control [--dtr on\|off] [--rts on\|off] [--break MS]` | Set modem control lines |
| `exec -- CMD...` | Release the port, run a flasher, re-acquire the port |
| `pause` / `resume` | Release / re-acquire the port by hand |
| `set --baud 9600 ...` | Change settings of a running session |
| `unsafe-send --reason "..." TEXT` | Send a command a safety rule blocked (meant to be approved by you) |
| `rules [--check TEXT]` | Show the safety rules in effect, or test a command against them |
| `status` / `close [--all]` | Show / end sessions |
| `init` | Create `.opensiterm/config.toml` in the project |
| `skill show\|install`, `setup`, `mcp`, `profiles` | Integration helpers |

Global options: `-s NAME` picks the session (needed when more than one is open), `--json` gives
machine-readable output, `--no-ts` drops timestamps. Exit codes: `0` ok, `1` error, `2` usage,
`3` timeout/no match, `4` blocked by a safety rule, `5` no session.

## Profiles

| Profile | Line ending | A reply ends at |
|---|---|---|
| `generic` (default) | `\r` (what PuTTY sends for Enter) | 0.5 s of silence |
| `mcu`: Zephyr, RT-Thread, NuttX, ESP-IDF console, FreeRTOS CLI, Arduino | `\r\n` | the shell prompt |
| `at`: 4G / NB-IoT / Wi-Fi / BT modules | `\r` | `OK`, `ERROR`, `+CME ERROR`, `>` |
| `linux`: Linux, OpenWrt, BusyBox | `\r` | `#`, `$`, `login:`, `Password:` |
| `uboot` | `\r`, never `\r\n` (U-Boot repeats the last command on an empty line) | `=>` |
| `binary` | none: hex in and out, frames split on 20 ms gaps | silence |
| `modbus` | like `binary`, plus a CRC16 appended to sends and checked on received frames | silence |

Override any of these with `--eol`, `--prompt`, `--done`, `--encoding gbk`, `--timeout` and so on.

## Safety model

Sending commands to real hardware isn't like editing code: some mistakes can't be rolled back.

- **Built-in rules** block obviously irreversible commands:
  - eFuse/OTP burning
  - flash erase (`sf/nand/mmc/mtd erase`, `erase_flash`...)
  - `mkfs`, format and repartitioning
  - `dd` to `/dev`, `sysupgrade`, `mtd write`, `fw_setenv`
  - factory reset and `env default -a`
  - `rm -rf /`
  - SIM PIN and lock commands
  - read-out protection and secure-boot enable

  A blocked command fails with exit code 4. The agent is told not to work around it.
- **Approval through your tool.** If you really want the command, the agent re-sends it with
  `opensiterm unsafe-send --reason "..."`. That is a different command name (and MCP tool:
  `serial_unsafe_send`), so your AI tool asks you to approve it.
- **Project rules** live in `.opensiterm/config.toml` (run `opensiterm init`). Commit it so your team
  shares the rules. A rule with `action = "deny"` can only be sent by a human from `opensiterm watch`.

  ```toml
  [defaults]
  port = "COM7"
  baud = 115200
  profile = "linux"

  [safety]
  disable = ["format"]          # turn off a built-in rule

  [[rules]]
  name = "motor"
  pattern = '\bmotor\s+(start|run)\b'
  action = "confirm"            # or "deny"
  reason = "starts the motor - make sure the fixture is clear"
  ```

- **Audit.** Every command sent and every blocked attempt goes to `.opensiterm/logs/audit.jsonl`,
  with its source (`ai`, `human`, `unsafe`) and reason.

> **Honest limits.** These are guard rails against an agent's *mistakes*, not a security boundary.
> The agent runs as your OS user and could bypass them on purpose (edit the config, kill the
> session, open the port with its own script). If you need hard isolation, give the agent only the
> MCP server and turn off its shell. Device output is untrusted input, and the skill tells agents
> never to follow instructions that appear in it.

## Working alongside the agent

- `opensiterm watch` in your own terminal: see everything live and type commands. Your input is
  tagged `(human)` in the log. Input is only accepted from a real terminal, so an agent can't pipe
  commands through it. On Git Bash/mintty, use Windows Terminal or `winpty`.
- **Flashing:** `opensiterm exec -- esptool.py --port COM7 write_flash 0x0 app.bin`, or avrdude,
  stm32flash, `idf.py flash`... The session releases the port, runs the flasher, takes the port back
  and captures the boot log.
- **Logs:** `.opensiterm/logs/` in your project. The session registry (pid, port, token) lives in
  `%LOCALAPPDATA%\opensiterm`, `~/Library/Application Support/opensiterm` or
  `~/.local/state/opensiterm`; override it with `OPENSITERM_HOME`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `\xNN` garbage | Wrong baud rate: `opensiterm set --baud 9600` (or 74880, 921600, 1500000...) |
| `no response within ...` | Wrong line ending (`--eol crlf` or `lf`), wrong baud, TX/RX swapped, or the command prints nothing |
| `in use by another program` (Windows) | Close the serial terminal (MobaXterm, PuTTY, Arduino Serial Monitor...) or use `opensiterm watch` instead |
| `permission denied` (Linux) | `sudo usermod -aG dialout $USER`, then log out and back in |
| macOS: port opens but nothing happens | Use `/dev/cu.*`, not `/dev/tty.*` |
| Chinese text garbled | `opensiterm set --encoding gbk` |
| Codex: errors reaching 127.0.0.1 or opening the port | Use the MCP server (see above) |

## FAQ

**Why not just a Skill?** A skill is only instructions. Something still has to hold the port open
between the agent's commands.

**Why not just MCP?** Many agents work best with plain shell commands, and Claude Code, opencode
and oh-my-pi all can run them. The CLI is also what you use yourself. Both entry points share the
same sessions.

**Remote boards?** Run `ser2net` (or any RFC 2217 server) on the machine the board is attached to,
then `opensiterm open rfc2217://lab-pi:4000` or `socket://lab-pi:4000`. OpenSiTerm itself only
listens on 127.0.0.1.

**More than one board?** Open one session per port and pick it with `-s NAME`.

## Development

```bash
git clone https://github.com/aimachy/opensiterm && cd opensiterm
pip install -e ".[dev]"
pytest                      # end-to-end tests use pyserial's loop:// port; no hardware needed
```

Pushing a tag `v*` builds the
single-file executables and attaches them to a GitHub release (`.github/workflows/release.yml`).

## License

MIT. OpenSiTerm is an independent project. It is not affiliated with OpenAI, Anthropic, DeepSeek,
Google or any other vendor named here.
