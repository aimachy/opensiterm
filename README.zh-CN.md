# OpenSiTerm

**让 AI 编程助手直接读写串口。** Claude Code、Codex、opencode、oh-my-pi、Gemini CLI、DeepSeek 等 AI 可以自己读取设备日志、往设备控制台里敲指令，你不用再把串口日志一段段复制进对话框。

[English](README.md)

```text
你   > 板子每隔几分钟就重启一次，查一下原因。
AI   > $ opensiterm open COM7 --profile linux
       $ opensiterm expect "Rebooting|watchdog|Kernel panic" -t 600
       $ opensiterm tail -n 80
       看门狗触发前 2 秒，sensord 进程发生了 OOM；free 显示……
```

## 为什么需要后台会话？

AI 每执行一条 shell 命令，都会启动一个新进程。如果每条命令都自己打开一次串口：

- 两条命令**之间**设备打印的内容会全部丢失，而你要找的崩溃日志往往就在这里面；
- 打开串口会翻转 DTR/RTS，Arduino、ESP32 等板子每次都会被**复位**；
- 串口是独占的，同一时刻只能一个程序打开，第二次打开会直接失败。

OpenSiTerm 为**每个串口维护一个后台会话**。会话持有串口，把收发内容打上时间戳，存进内存和日志文件；USB 设备重新枚举后会自动重连。AI 通过命令行（CLI）、MCP 服务或 Agent Skill 来访问它：

```text
Claude Code / Codex / opencode ──shell──► opensiterm CLI ──┐
MCP 客户端（Codex、聊天应用）──MCP────► opensiterm mcp ──┼─ 127.0.0.1 + token ─► 会话 ──► COM7 / ttyUSB0 / socket://
你（在终端里）──────────────────────────► opensiterm watch ┘                       ├─ 带时间戳的历史（8 MB）
                                                                                   ├─ .opensiterm/logs/<串口>-<时间>.log
                                                                                   └─ .opensiterm/logs/audit.jsonl
```

## 特性

- **不丢数据**：每个字节都带时间戳。`read` 只返回上次读取之后的新内容；更早的内容用 `tail --grep` 或日志文件查。
- **回复收得全**：`send` 会一直等到设备提示符出现、`--expect` 正则匹配或者输出安静下来才返回，AI 不用再猜该 `sleep` 几秒。
- **输出精简**：去掉 ANSI 颜色码，控制字符显示为 `\xNN`；输出过长时只保留开头和结尾，并注明完整日志的路径。
- **设备预设**：覆盖 MCU 命令行、AT 模组、Linux、U-Boot、二进制协议和 Modbus RTU。支持十六进制收发、按时间间隔分帧。校验和由工具计算并校验（`modbus`、`ccitt`、`xmodem`、`crc8`、`sum8`、`xor8`），因为大模型手算 CRC 很容易算错。
- **贴合硬件开发流程**：
  - 用 DTR/RTS 脉冲复位板子，发送 break 信号；
  - `exec -- <烧录命令>` 会先释放串口、执行烧录、再重新接管串口，并接住开机日志；
  - USB 重新枚举后自动重连。
- **安全规则**：默认拦截不可逆的指令，例如 eFuse/OTP 烧写、擦除 flash、`mkfs`、用 `dd` 写 `/dev`、恢复出厂设置、修改 SIM 卡 PIN 码等。每个项目可以追加自己的规则，所有发送都会记入审计日志。
- **人始终在场**：`opensiterm watch` 实时显示会话内容，也能直接打字发给设备。会话占住串口后，你原来的串口终端就打不开这个口了，watch 就是它的替代品。
- **跨平台**：支持 Windows、macOS、Linux。网络串口（ser2net / RFC 2217）可以通过 `socket://`、`rfc2217://` 连接。唯一的依赖是 pyserial。

## 适合谁

**一句话：凡是需要盯着串口调试设备的人，都可以让 AI 替你看日志、敲指令。**

### 主要用户

| 人群 | 典型场景 | OpenSiTerm 帮上的忙 |
|---|---|---|
| **MCU / 嵌入式固件工程师**<br>（STM32、ESP32、GD32、nRF、RT-Thread、FreeRTOS、Zephyr） | 改代码、烧录、看日志，反复循环；分析 HardFault 和断言打印出来的信息 | AI 改完代码自己烧录，接住开机日志并判断结果，你不用再复制粘贴日志；崩溃现场带时间戳完整保留，不会漏掉 |
| **嵌入式 Linux / BSP / 驱动工程师**<br>（U-Boot、内核移植、Buildroot、Yocto、OpenWrt） | 板子启动卡住、kernel panic、驱动 probe 失败、修改 U-Boot 环境变量 | 几千行的启动日志，AI 只看关键的开头和结尾，需要时再去 grep 完整日志；U-Boot、Linux 提示符自动识别；危险的 flash 擦写指令默认拦截 |
| **树莓派 / 开发板 / 创客玩家**<br>（Raspberry Pi、Arduino、ESP32、MicroPython） | 看不懂报错，不知道板子在干什么 | 用大白话问 AI“板子为什么一直重启”，它会自己去串口上看、去问设备，再用你听得懂的话解释 |
| **物联网 / 通信模组工程师**<br>（4G/5G、NB-IoT、Wi-Fi、蓝牙模组） | AT 指令调试：注册网络、查信号、建立连接 | AI 熟悉各家模组的 AT 指令集，能自动判断 `OK`/`ERROR`，一来一回地排查；容易锁死 SIM 卡的 PIN 码指令默认拦截 |
| **硬件工程师** | 新板上电调试，确认串口通不通、系统起没起来 | 不太熟悉软件命令也没关系，用自然语言描述要检查什么，AI 去执行 |
| **测试 / 产线工程师** | 回归测试、通宵稳定性测试、批量验证不同固件版本 | AI 可以通宵守着，等 `watchdog`、`panic` 之类的关键字出现；日志全程带时间戳，所有操作都有审计记录，方便追溯 |
| **工业自动化 / 工控工程师**<br>（Modbus RTU、RS-485、PLC、仪表） | 调试二进制协议，手算 CRC 很容易出错 | 十六进制收发，CRC 由工具自动计算和校验，AI 只负责组织帧的内容 |
| **网络工程师** | 通过 Console 口配置交换机、路由器 | AI 按配置要求逐条下发命令并核对回显 |
| **FAE / 技术支持** | 复现客户现场的问题、收集日志 | 板子接在别的电脑上也能远程调试（通过 ser2net/RFC 2217）；完整日志可以直接发给研发 |
| **高校师生 / 嵌入式教学** | 学习时卡在“为什么串口没反应” | AI 会结合实际输出讲解，比如波特率不对、行尾不对、TX/RX 接反 |
| **机器人 / 无人机开发者** | 调试飞控、电机驱动、传感器的串口协议 | 支持二进制协议模式、按时间间隔分帧、同时调试多个串口 |

### 这些人的共同痛点

1. **复制日志太累**：串口日志要一段段复制进对话框，AI 才能看到，而且总漏掉最关键的那几行。
2. **串口被独占**：调试助手和自己的终端软件不能同时打开一个串口。
3. **AI 不知道时序**：设备什么时候重启的、两条日志之间隔了多久，复制过去的文本里都看不出来。
4. **怕 AI 闯祸**：真实硬件上的误操作可能没法回滚。

OpenSiTerm 对这四点的解决办法：
- 后台会话持续记录，AI 自己就能看到全部输出；
- 人和 AI 通过 `watch` 共用同一个串口；
- 每一行都带毫秒级时间戳；
- 高危指令默认拦截，所有操作都有审计。

### 不适合的场景

- **不是 JTAG/SWD 调试器**：打断点、单步执行、读内存，请用 OpenOCD、J-Link、GDB。OpenSiTerm 只管串口。
- **暂不支持** CAN 总线、J-Link RTT、逻辑分析仪或示波器这类信号级调试。
- **不适合高速数据流**：比如持续每秒数 MB 的数据采集。它面向的是调试级别的数据量，内存缓冲默认 8 MB。
- **不要无人值守地操作生产设备或危险机械**：安全规则只能防止 AI 手滑，不能代替人的监督。

## 安装

**有 Python 3.9+**（推荐）：

```bash
pipx install opensiterm          # 或：uv tool install opensiterm
# 也可以直接从 GitHub 安装：
pipx install git+https://github.com/aimachy/opensiterm
```

**没有 Python**：到 [Releases](https://github.com/aimachy/opensiterm/releases) 下载对应系统的单文件可执行程序，重命名为 `opensiterm`（Windows 上是 `opensiterm.exe`），放进 `PATH`。这些程序暂时没有代码签名：

- **Windows**：首次运行时 SmartScreen 可能会拦截，点“更多信息 → 仍要运行”。
- **macOS**：执行 `xattr -d com.apple.quarantine opensiterm && chmod +x opensiterm`
- **Linux**：执行 `chmod +x opensiterm`，并把当前用户加入 `dialout` 组（Arch/Fedora 上是 `uucp` 组）。

装好后运行 **`opensiterm setup`**，它会按你的实际安装路径，打印出接入各个 AI 工具的具体命令。

## 接入你的 AI 工具

多数工具需要接入两部分：
- **Skill** 教会有 shell 的 AI 怎么使用这个命令行工具；
- **MCP 服务** 给不能（或不应该）执行 shell 命令的 AI 提供工具调用。

```bash
opensiterm skill install   # 把 SKILL.md 复制到 ~/.claude/skills/ 和 ~/.agents/skills/
```

| 工具 | Skill | MCP 服务 |
|---|---|---|
| **Claude Code** | 运行 `opensiterm skill install`；或者装插件：先 `/plugin marketplace add aimachy/opensiterm`，再 `/plugin install opensiterm@opensiterm` | 可选：`claude mcp add --transport stdio --scope user opensiterm -- opensiterm mcp` |
| **Codex CLI** | 运行 `opensiterm skill install`（Codex 读取 `~/.agents/skills`） | **必须配置**：`codex mcp add opensiterm -- opensiterm mcp`（原因见下方说明） |
| **opencode** | 自动读取 `~/.claude/skills` 或 `~/.agents/skills` | 在 `~/.config/opencode/opencode.json` 中加入：`{"mcp": {"opensiterm": {"type": "local", "command": ["opensiterm", "mcp"]}}}` |
| **pi** | `pi install git:github.com/aimachy/opensiterm` | 不需要：pi 本身不支持 MCP，通过 Skill 调用命令行 |
| **oh-my-pi** | 先 `/marketplace add aimachy/opensiterm`，再 `omp plugin install opensiterm@opensiterm`（也会自动读取 `~/.agents/skills`） | 在 `~/.omp/agent/mcp.json` 中加入：`{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` |
| **Gemini CLI** | `gemini extensions install https://github.com/aimachy/opensiterm`，以扩展形式一次装好 Skill 和 MCP | （已包含） |
| **Qwen Code** | `qwen extensions install https://github.com/aimachy/opensiterm`（Qwen Code 会自动转换 Gemini 扩展） | （已包含） |
| **Kimi Code CLI** | 自动读取 `~/.claude/skills` 或 `~/.agents/skills` | `kimi mcp add --transport stdio opensiterm -- opensiterm mcp` |
| **Antigravity CLI**（`agy`） | `opensiterm skill install --target antigravity` | 在 `~/.gemini/config/mcp_config.json` 中加入：`{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` |
| **Crush** | 自动读取 `~/.agents/skills` 或 `~/.claude/skills` | 在 `~/.config/crush/crushrc` 中加入：`mcp add opensiterm --command opensiterm --args mcp` |
| **Grok Build**（xAI）、**ZCode**（智谱 Z.ai）、**DeepSeek Harness**（`dsh`） | 都支持 Agent Skills 和 MCP，按各自文档指向 `~/.agents/skills` 或添加 MCP 服务 | `{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` 或该工具的等价写法 |
| **聊天客户端**（Claude Desktop、Cherry Studio、Chatbox、Cursor 等） | 不适用 | `{"mcpServers": {"opensiterm": {"command": "opensiterm", "args": ["mcp"]}}}` |

也可以用跨工具的 Skill 安装器：`npx skills add aimachy/opensiterm`。

> **模型和工具的区别**：DeepSeek、Kimi、Qwen、GLM、Grok 是**模型**，真正操作串口的是运行它们的**工具**。上表中任何一个工具都可以换用任意模型。比如 DeepSeek、Kimi K2、GLM 都官方支持在 Claude Code、opencode 或 Codex 中使用，按对应工具那一行配置即可。

> **Codex 用户注意**：在 Linux 和 macOS 上，Codex 的沙箱会对 shell 命令隐藏串口设备，并禁止访问本机网络，所以在沙箱里执行 `opensiterm` 命令会失败。MCP 服务运行在沙箱外面，因此请注册 MCP 服务，AI 会改用 `serial_*` 工具。

> **Claude Code 权限设置**：想减少日常使用中的审批弹窗，可以只放行常用子命令，同时让 `unsafe-send` 保持“每次询问”。千万不要放行 `Bash(opensiterm:*)`，那样会连 `unsafe-send` 也一起自动通过。
>
> ```json
> { "permissions": {
>     "allow": ["Bash(opensiterm ports:*)", "Bash(opensiterm status:*)", "Bash(opensiterm open:*)",
>               "Bash(opensiterm read:*)", "Bash(opensiterm tail:*)", "Bash(opensiterm expect:*)",
>               "Bash(opensiterm send:*)"],
>     "ask":   ["Bash(opensiterm unsafe-send:*)", "Bash(opensiterm exec:*)"] } }
> ```

## 快速上手

```bash
opensiterm ports                         # COM7  CH340 - USB-SERIAL CH340 (COM7)  [USB 1A86:7523]
opensiterm open COM7 -b 115200 --profile linux
opensiterm read --wait 3                 # 看设备输出了什么
opensiterm send "uname -a"               # 发一条指令，拿到完整回复
opensiterm expect "login:" -t 60         # 等待某个关键字出现
opensiterm watch                         # （给你用）在另一个终端实时查看，也可以打字发送
opensiterm close
```

输出紧凑且带时间戳。`>>` 表示发出的内容，`##` 表示事件：

```text
20:34:34.700 >> uname -a
20:34:34.703 uname -a
20:34:34.711 Linux buildroot 5.10.110 #1 SMP armv7l GNU/Linux
20:34:34.712 ~ #
```

## 使用示例：在 Claude Code 里调试一块板子

1. **安装并接入**（只需一次）：

   ```bash
   uv tool install git+https://github.com/aimachy/opensiterm   # 或 pipx install git+https://...
   opensiterm skill install     # Skill 装到 ~/.claude/skills/ 和 ~/.agents/skills/
   ```

   如果提示找不到 `opensiterm` 命令，执行 `uv tool update-shell`（或 `pipx ensurepath`）后重开终端。可执行文件在 `~/.local/bin/`，Windows 上是 `C:\Users\<用户名>\.local\bin\opensiterm.exe`。

2. **重启 Claude Code**，开新终端和新会话，让 PATH 和 Skill 生效。

3. **释放串口**：关掉其他串口终端软件（MobaXterm、PuTTY 等）对该串口的连接，因为串口同一时刻只能被一个程序占用。想自己实时看着，就在另一个终端运行 `opensiterm watch`。

4. **直接用自然语言提需求**，例如：

   > COM7 上接的是一块 Linux 板子，波特率 115200。帮我看一下系统版本、内存和磁盘使用情况。

   AI 会自己调用 `opensiterm open / send / read`。日志保存在你启动 Claude Code 的项目目录下的 `.opensiterm/logs/` 里。

**需要注意：**

- 默认规则会拦截擦除 flash、格式化、`rm -rf /` 这类危险指令，但 `reboot`、修改配置之类的指令不会被拦。哪些操作允许做，最好在需求里先跟 AI 说清楚。
- 每条 `opensiterm` 命令，Claude Code 默认都会弹窗请你审批。嫌烦的话，可以按上文“Claude Code 权限设置”只放行日常命令，`unsafe-send` 仍保留每次询问。
- 改用 Codex 时，还需要注册 MCP：`codex mcp add opensiterm -- opensiterm mcp`。如果 `opensiterm` 不在 PATH 里，就写完整路径，如 `C:\Users\<用户名>\.local\bin\opensiterm.exe`。

## 命令一览

| 命令 | 作用 |
|---|---|
| `ports` | 列出串口，识别芯片型号（CH340、CP210x、FTDI、ESP32-S3 USB、ST-LINK 等），并标出哪些已被会话占用 |
| `open [PORT]` | 启动会话。PORT 可以是 `COM7`、`/dev/ttyUSB0`、`auto`、`socket://主机:端口`、`rfc2217://...`；`--match 文本` 可以按描述或 USB ID 匹配串口。重复执行是安全的 |
| `read [--wait S]` | 读取上次之后的新输出 |
| `send 文本` | 发送一行并打印回复。可选参数：`--expect 正则`、`--hex`、`--crc`、`--ctrl c`、`-e`（转义）、`--eol`、`--no-wait` |
| `expect 正则 [-t S]` | 等待新输出中出现指定模式 |
| `tail [-n N] [--grep 正则]` | 查看最近的历史，不移动读取游标 |
| `watch` | 给人用的实时窗口，可以打字发送（输入 `/help` 查看本地命令） |
| `reset [--line rts\|dtr] [--wait S]` | 用控制线脉冲复位板子，并收集开机日志 |
| `control [--dtr on\|off] [--rts on\|off] [--break MS]` | 设置 DTR/RTS 线或发送 break 信号 |
| `exec -- 命令...` | 释放串口，执行烧录命令，然后重新接管串口 |
| `pause` / `resume` | 手动释放 / 重新接管串口 |
| `set --baud 9600 ...` | 修改运行中会话的参数 |
| `unsafe-send --reason "..." 文本` | 发送被安全规则拦截的指令（需要你审批） |
| `rules [--check 文本]` | 查看当前生效的规则，或测试某条指令会不会被拦截 |
| `status` / `close [--all]` | 查看会话状态 / 关闭会话 |
| `init` | 在项目中生成 `.opensiterm/config.toml` |
| `skill show\|install`、`setup`、`mcp`、`profiles` | 接入相关的辅助命令 |

全局选项：
- `-s 名称`：指定会话（同时打开多个会话时必须指定）；
- `--json`：输出机器可读格式；
- `--no-ts`：不显示时间戳。

退出码：`0` 成功；`1` 错误；`2` 用法错误；`3` 超时或未匹配；`4` 被安全规则拦截；`5` 没有会话。

## 设备预设

| 预设 | 行尾 | 回复结束的判定 |
|---|---|---|
| `generic`（默认） | `\r`（和 PuTTY 按回车发送的一致） | 安静 0.5 秒 |
| `mcu`：Zephyr、RT-Thread、NuttX、ESP-IDF 控制台、FreeRTOS CLI、Arduino | `\r\n` | 出现 shell 提示符 |
| `at`：4G / NB-IoT / Wi-Fi / 蓝牙模组 | `\r` | 出现 `OK`、`ERROR`、`+CME ERROR` 或 `>` |
| `linux`：Linux、OpenWrt、BusyBox | `\r` | 出现 `#`、`$`、`login:` 或 `Password:` |
| `uboot` | `\r`，绝不能用 `\r\n`（U-Boot 遇到空行会重复执行上一条命令） | 出现 `=>` |
| `binary` | 无：十六进制收发，按 20ms 间隔分帧 | 输出安静 |
| `modbus` | 同 `binary`，另外发送时自动追加 CRC16，接收时自动校验 | 输出安静 |

以上默认值都可以用 `--eol`、`--prompt`、`--done`、`--encoding gbk`、`--timeout` 等参数覆盖。

## 安全模型

给真实硬件发指令和改代码不一样：有些错误没法回滚。

- **内置规则**会拦截明显不可逆的指令：
  - eFuse/OTP 烧写；
  - 擦除 flash（`sf/nand/mmc/mtd erase`、`erase_flash` 等）；
  - `mkfs`、格式化、重新分区；
  - 用 `dd` 写 `/dev`、`sysupgrade`、`mtd write`、`fw_setenv`；
  - 恢复出厂设置、`env default -a`；
  - `rm -rf /`；
  - SIM 卡 PIN 码和锁操作；
  - 开启读保护或 secure boot。

  被拦截的指令以退出码 4 失败，同时提示 AI 不要想办法绕过。
- **由你的 AI 工具审批**：如果你确实需要执行这条指令，AI 会改用 `opensiterm unsafe-send --reason "..."` 重新发送。这是一个独立的命令名（MCP 中对应 `serial_unsafe_send` 工具），所以你的 AI 工具一定会弹窗请你审批。
- **项目规则**写在 `.opensiterm/config.toml` 里，用 `opensiterm init` 生成。建议提交到仓库，团队共用同一套规则。动作设为 `action = "deny"` 的规则，只有人在 `opensiterm watch` 里才能发送。

  ```toml
  [defaults]
  port = "COM7"
  baud = 115200
  profile = "linux"

  [safety]
  disable = ["format"]          # 关闭某条内置规则

  [[rules]]
  name = "motor"
  pattern = '\bmotor\s+(start|run)\b'
  action = "confirm"            # 或 "deny"
  reason = "会启动电机，确认夹具附近没有人"
  ```

- **审计**：每一条发送的指令和每一次被拦截的尝试，都记录在 `.opensiterm/logs/audit.jsonl` 中，包括来源（`ai`、`human`、`unsafe`）和理由。

> **能力边界，如实说明**：这些规则只能防止 AI **手滑**，不是安全边界。AI 以你的系统账号运行，如果它存心绕过，完全可以做到，比如改配置、杀掉会话进程、自己写脚本打开串口。如果需要强隔离，就只给 AI 开放 MCP 服务，并关闭它的 shell。设备输出属于不可信输入，Skill 中已经要求 AI 绝不执行输出里出现的指令。

## 人和 AI 协作

- **实时查看**：在你自己的终端里运行 `opensiterm watch`，可以实时看到所有内容，也可以直接打字。你输入的内容在日志里标记为 `(human)`。watch 只接受真实终端的输入，AI 无法通过管道借道发送。如果用 Git Bash/mintty，请换成 Windows Terminal，或者用 `winpty`。
- **烧录固件**：执行 `opensiterm exec -- esptool.py --port COM7 write_flash 0x0 app.bin`，avrdude、stm32flash、`idf.py flash` 等同理。会话会先释放串口、执行烧录、再接管串口，并接住开机日志。
- **日志位置**：日志在项目的 `.opensiterm/logs/` 中。会话注册信息（pid、端口、token）保存在：
  - Windows：`%LOCALAPPDATA%\opensiterm`
  - macOS：`~/Library/Application Support/opensiterm`
  - Linux：`~/.local/state/opensiterm`

  可以用环境变量 `OPENSITERM_HOME` 覆盖。

## 常见问题排查

| 现象 | 解决办法 |
|---|---|
| 出现 `\xNN` 乱码 | 波特率不对：`opensiterm set --baud 9600`（或 74880、921600、1500000 等） |
| `no response within ...` | 行尾不对（试试 `--eol crlf` 或 `lf`）、波特率不对、TX/RX 接反，或者这条指令本来就不输出内容 |
| `in use by another program`（Windows） | 关掉占用串口的终端软件（MobaXterm、PuTTY、Arduino 串口监视器等），或者改用 `opensiterm watch` |
| `permission denied`（Linux） | 执行 `sudo usermod -aG dialout $USER`，然后注销并重新登录 |
| macOS 上能打开串口但没有反应 | 使用 `/dev/cu.*`，不要用 `/dev/tty.*` |
| 中文乱码 | `opensiterm set --encoding gbk` |
| Codex 中访问 127.0.0.1 或打开串口报错 | 改用 MCP 服务（见上文） |

## FAQ

**为什么不只做一个 Skill？** Skill 只是一份说明书。总得有个进程在 AI 的两条命令之间一直占着串口。

**为什么不只做 MCP？** 很多 AI 用普通 shell 命令时效果最好，Claude Code、opencode、oh-my-pi 都能直接执行 shell 命令。命令行工具你自己也能用。两种入口共享同一批会话。

**板子接在别的电脑上怎么办？** 在接板子的那台机器上运行 `ser2net`（或任意 RFC 2217 服务），然后执行 `opensiterm open rfc2217://lab-pi:4000` 或 `socket://lab-pi:4000`。OpenSiTerm 本身只监听 127.0.0.1。

**同时调试多块板子？** 每个串口开一个会话，用 `-s 名称` 区分。

## 开发

```bash
git clone https://github.com/aimachy/opensiterm && cd opensiterm
pip install -e ".[dev]"
pytest                      # 端到端测试使用 pyserial 的 loop:// 虚拟串口，不需要硬件
```

推送 `v*` 格式的标签会自动构建单文件程序并发布到 GitHub Release（见 `.github/workflows/release.yml`）。

## 许可证

MIT。OpenSiTerm 是独立项目，与 OpenAI、Anthropic、DeepSeek、Google 以及文中提到的其他厂商均无关联。
