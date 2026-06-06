# OpenMIA Webhooker Codex Deployment Tutorial

这份教程用于部署 PyPI 版 OpenMIA Webhooker，让 Codex 的 hook 事件被转换成 OpenMIA Runtime JSON v1，并通过现有 custom_json webhook 上传到 OpenMIA。

本文假设 OpenMIA Webhooker 已经发布到 PyPI。Linux/macOS 示例使用 `python3` 和 POSIX shell，Windows 示例使用 PowerShell。

```bash
pip install openmia-webhooker
```

并且 OpenMIA Webhooker 提供统一 CLI：

```bash
openmia-webhooker
```

## 1. 功能结构

部署后整体结构是：

```text
Codex hooks
  -> openmia-webhooker codex
  -> openmia_webhooker.adapters.codex
  -> OpenMIA Runtime JSON v1
  -> OpenMIA custom_json endpoint
```

OpenMIA Webhooker 负责：

- 读取 Codex hook stdin payload。
- 识别 `UserPromptSubmit`、`Stop`、`PreToolUse`、`PostToolUse` 事件。
- 构造 Runtime JSON v1：session、trace、顶层 chat span、tool span、reasoning placeholder span。
- 根据配置决定上传明文或 hash 摘要。
- 维护本地 state，把同一个本地 session 稳定映射到一个 trace，并把多轮对话追加为新的顶层 chat spans。
- 上传到现有 OpenMIA custom_json ingestion endpoint；服务端只负责 raw-first 存储、权限隔离和标准化写入。

Runtime JSON v1 使用 `schemaVersion = "openmia.runtime.v1"`。SDK/webhooker 侧负责理解 Codex hook 和 Claude Code stream-json 语义；OpenMIA app 服务端不再为每个本地工具复制一套来源专属语义 adapter。custom_json endpoint 保持不变，只是 payload schema 升级。

本地仍需要维护这些文件。`~` 表示当前用户 home 目录，Windows 下等价于 `$HOME` 或 `$env:USERPROFILE`：

- `~/.codex/openmia-custom-json.env`: OpenMIA endpoint、ingest key、正文采集开关。
- `~/.codex/config.toml`: Codex hook 配置。
- `~/.codex/openmia-custom-json/collector.log`: OpenMIA Webhooker 上传日志。
- `~/.codex/openmia-custom-json/state/*.json`: 会话关联 state。

Codex 和 Claude Code 当前遵循同一个展示语义：trace 表示本地 session，每轮用户对话是 `parentId = null` 的顶层 `chat` span，并带稳定 `roundId`。SDK 不发送空 `round` span，也不发送 session summary workflow span。

## 2. 前置条件

目标机器需要：

- Python 3.10 或更高版本。
- `pip` 可用。
- Codex 已安装并能正常运行。
- 一个 OpenMIA custom_json ingestion endpoint。
- 一个 OpenMIA custom_json ingest key。
- 目标机器可以访问 `https://app.openmia.ai`。

检查 Python 和 pip：

```bash
python3 --version
python3 -m pip --version
```

Windows PowerShell：

```powershell
py --version
py -m pip --version
```

## 3. 安装 OpenMIA Webhooker

Linux/macOS 推荐使用当前用户安装：

```bash
python3 -m pip install --user openmia-webhooker
```

如果你使用虚拟环境：

```bash
python3 -m venv ~/.venvs/openmia
source ~/.venvs/openmia/bin/activate
python3 -m pip install openmia-webhooker
```

Windows PowerShell 推荐使用当前用户安装：

```powershell
py -m pip install --user openmia-webhooker
```

虚拟环境部署：

```powershell
py -m venv $HOME\.venvs\openmia
& $HOME\.venvs\openmia\Scripts\Activate.ps1
py -m pip install openmia-webhooker
```

确认 CLI 可用：

```bash
openmia-webhooker --help
```

如果提示 `command not found`，检查用户级 pip bin 目录是否在 `PATH`：

```bash
python3 -m site --user-base
```

常见路径是：

```text
~/.local/bin
```

可以临时加入：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

生产环境建议把这行写入 shell profile，或在 Codex hook command 中使用 CLI 的绝对路径。

Windows PowerShell 可以查看用户级 scripts 目录：

```powershell
py -m site --user-site
```

如果 `openmia-webhooker` 不在 `PATH`，可以把 Python 用户 scripts 目录加入用户 PATH，或者在 Codex hook command 中使用完整的 `openmia-webhooker.exe` 路径。

安装后建议先运行本地诊断：

```bash
openmia-webhooker doctor
```

Windows PowerShell：

```powershell
openmia-webhooker doctor
```

## 4. 配置 OpenMIA

创建目录：

```bash
mkdir -p ~/.codex/openmia-custom-json/state
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force "$HOME\.codex\openmia-custom-json\state" | Out-Null
```

创建 `~/.codex/openmia-custom-json.env`：

```bash
OPENMIA_CUSTOM_JSON_ENDPOINT=https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json
OPENMIA_CUSTOM_JSON_INGEST_KEY=<your_openmia_ingest_key>
OPENMIA_CAPTURE_TEXT=false
```

Windows PowerShell 可用记事本或下面命令创建 `$HOME\.codex\openmia-custom-json.env`：

```powershell
@"
OPENMIA_CUSTOM_JSON_ENDPOINT=https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json
OPENMIA_CUSTOM_JSON_INGEST_KEY=<your_openmia_ingest_key>
OPENMIA_CAPTURE_TEXT=false
"@ | Set-Content -Encoding utf8 "$HOME\.codex\openmia-custom-json.env"
```

字段说明：

- `OPENMIA_CUSTOM_JSON_ENDPOINT`: OpenMIA custom_json webhook 地址。
- `OPENMIA_CUSTOM_JSON_INGEST_KEY`: OpenMIA ingest key。不要提交到 Git。
- `OPENMIA_CAPTURE_TEXT`: 是否上传用户输入、工具输入输出、assistant 输出等明文。

推荐生产环境先用：

```bash
OPENMIA_CAPTURE_TEXT=false
```

这样只上传长度和 sha256 hash，不上传正文。确认隐私策略允许后，再改成：

```bash
OPENMIA_CAPTURE_TEXT=true
```

限制 env 文件权限：

```bash
chmod 600 ~/.codex/openmia-custom-json.env
```

路径解析优先级：

- 代码中显式传入的 `base_dir`。
- `OPENMIA_HOME`。
- 兼容变量 `CODEX_HOME`。
- 默认 `~/.codex`。

高级覆盖变量：

- `OPENMIA_CUSTOM_JSON_ENV_FILE`: 指定 env 文件完整路径。
- `OPENMIA_STATE_DIR`: 指定 state 目录完整路径。
- `OPENMIA_LOG_PATH`: 指定 collector log 完整路径。

Linux/macOS 临时覆盖示例：

```bash
export OPENMIA_HOME="$HOME/.codex"
```

Windows PowerShell 临时覆盖示例：

```powershell
$env:OPENMIA_HOME = "$HOME\.codex"
```

## 5. 启用 Codex Hooks

把下面配置合并到 `~/.codex/config.toml`。如果已经有同名 hook block，保留一份即可。

```toml
[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "openmia-webhooker codex user_prompt_submit"
timeout = 15
statusMessage = "Sending OpenMIA custom telemetry"

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "openmia-webhooker codex stop"
timeout = 20
statusMessage = "Finalizing OpenMIA custom telemetry"

[[hooks.PreToolUse]]
[[hooks.PreToolUse.hooks]]
type = "command"
command = "openmia-webhooker codex pre_tool_use"
timeout = 15
statusMessage = "Sending OpenMIA tool telemetry"

[[hooks.PostToolUse]]
[[hooks.PostToolUse.hooks]]
type = "command"
command = "openmia-webhooker codex post_tool_use"
timeout = 20
statusMessage = "Sending OpenMIA tool result telemetry"
```

如果 Codex hook 环境找不到 `openmia-webhooker`，使用绝对路径，例如：

```toml
command = "/home/you/.local/bin/openmia-webhooker codex user_prompt_submit"
```

虚拟环境部署时可以这样写：

```toml
command = "/home/you/.venvs/openmia/bin/openmia-webhooker codex user_prompt_submit"
```

Windows PowerShell 用户级安装时，hook command 可以使用 `openmia-webhooker.exe` 的完整路径，例如：

```toml
command = "C:\\Users\\you\\AppData\\Roaming\\Python\\Python313\\Scripts\\openmia-webhooker.exe codex user_prompt_submit"
```

Windows 虚拟环境部署时：

```toml
command = "C:\\Users\\you\\.venvs\\openmia\\Scripts\\openmia-webhooker.exe codex user_prompt_submit"
```

## 6. 本地 Dry-Run 验证

dry-run 不会上传数据，只会打印将要发送的 JSON。

```bash
openmia-webhooker codex self_test --dry-run
```

模拟一次用户输入：

```bash
printf '{"conversation_id":"deploy_test","prompt":"hello from dry run"}' \
  | openmia-webhooker codex user_prompt_submit --dry-run
```

模拟一次工具调用开始：

```bash
printf '{"conversation_id":"deploy_test","tool_call_id":"call-1","tool_name":"exec_command","cmd":"python3 --version"}' \
  | openmia-webhooker codex pre_tool_use --dry-run
```

模拟一次工具调用结束：

```bash
printf '{"conversation_id":"deploy_test","tool_call_id":"call-1","tool_name":"exec_command","stdout":"Python 3.x"}' \
  | openmia-webhooker codex post_tool_use --dry-run
```

模拟一次停止事件：

```bash
printf '{"conversation_id":"deploy_test","output":"completed"}' \
  | openmia-webhooker codex stop --dry-run
```

如果这些命令都输出 JSON，说明 OpenMIA Webhooker、CLI、配置读取和 state 写入基本正常。

## 7. 真实上传验证

确认 ingest key 已配置后，运行：

```bash
openmia-webhooker codex self_test
```

查看日志：

```bash
tail -n 20 ~/.codex/openmia-custom-json/collector.log
```

成功时会看到类似：

```text
"message": "uploaded status 200"
"success": true
```

然后打开 OpenMIA 控制台，按最近时间查找 `Codex custom JSON collector self-test` 或 `Codex session`。

## 8. Claude Code 支持

Claude Code 有三种入口，全部输出 `schemaVersion = "openmia.runtime.v1"`，并复用现有 custom_json endpoint。

### stdin adapter

用于管道、CI 和调试。它从 stdin 读取 Claude Code `stream-json`：

```bash
claude -p "hello" --verbose --output-format stream-json --include-hook-events \
  | openmia-webhooker claude-code
```

dry-run：

```bash
printf '{"type":"system","subtype":"init","session_id":"claude-test"}\n' \
  | openmia-webhooker claude-code --dry-run
```

### non-interactive wrapper

用于不想手动拼 Claude Code `stream-json` 参数的场景。只支持 `--print/-p` 非交互模式：

```bash
openmia-webhooker claude-code-run -- -p "hello"
```

OpenMIA Webhooker 会自动追加：

```text
--verbose --output-format stream-json --include-hook-events
```

上传完成后，wrapper 会把 Claude Code 最终 result 文本输出到 stdout。`--dry-run` 时只输出 OpenMIA payload，不上传。

```bash
openmia-webhooker claude-code-run --dry-run -- -p "hello"
```

### local JSONL watcher

用于已有 Claude Code CLI 调用没有被 wrapper 包住的情况。默认读取：

```text
~/.claude/projects/**/*.jsonl
```

一次性扫描：

```bash
openmia-webhooker claude-code-watch --once
```

持续跟随，默认等待 round 空闲 10 秒后上传，避免过早上传不完整 assistant/tool 事件：

```bash
openmia-webhooker claude-code-watch --follow
```

自定义项目目录：

```bash
openmia-webhooker claude-code-watch --once --projects-dir /path/to/.claude/projects
```

Windows PowerShell：

```powershell
openmia-webhooker claude-code-watch --once --projects-dir "$HOME\.claude\projects"
```

watcher 只在真实用户 prompt 行开始新 round。Claude Code JSONL 里有些 `type=user` 行是 tool result，SDK 会继续把这些事件归入当前 round。后续 assistant/tool/result/error 事件归入当前 round，直到下一个真实用户 prompt 或 idle flush。已上传 round 会记录在 `~/.codex/openmia-custom-json/state`，重复 `--once` 不会重复上传。

如果 Claude Code 可执行文件不是 `claude`，可以使用：

```bash
openmia-webhooker claude-code-run --claude-command /path/to/claude -- -p "hello"
```

Windows PowerShell：

```powershell
openmia-webhooker claude-code-run --claude-command "C:\Path\To\claude.cmd" -- -p "hello"
```

也可以通过环境变量配置：

```bash
export OPENMIA_CLAUDE_COMMAND="/path/to/claude"
export OPENMIA_CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"
```

Windows PowerShell：

```powershell
$env:OPENMIA_CLAUDE_COMMAND = "C:\Path\To\claude.cmd"
$env:OPENMIA_CLAUDE_PROJECTS_DIR = "$HOME\.claude\projects"
```

## 9. 让 Codex 生效

更新 `config.toml` 后：

1. 关闭当前 Codex/IDE 会话。
2. 重新打开 Codex。
3. 发送一条简单消息。
4. 让 Codex 执行一个小工具调用，例如 `python3 --version`。
5. 检查 `~/.codex/openmia-custom-json/collector.log` 是否出现新的 `uploaded status 200`。

有些 Codex 环境首次运行 hook 时会要求信任 hook 命令。确认命令路径和 OpenMIA Webhooker 来源无误后，允许该 hook。

## 10. 日志和排障

先运行本地诊断。它不会上传、不写 state，也不会打印 ingest key：

```bash
openmia-webhooker doctor
```

机器可读输出：

```bash
openmia-webhooker doctor --json
```

查看最近日志：

```bash
tail -n 50 ~/.codex/openmia-custom-json/collector.log
```

常见问题：

- `openmia-webhooker: command not found`: CLI 不在 `PATH`。改用绝对路径，或把 `~/.local/bin` 加入 `PATH`。
- `OPENMIA_CUSTOM_JSON_INGEST_KEY missing`: `.env` 文件不存在、路径不对，或 key 没写。
- Claude command 找不到: 使用 `openmia-webhooker doctor` 查看解析结果，再通过 `--claude-command` 或 `OPENMIA_CLAUDE_COMMAND` 指定。
- `http_error`: endpoint 或 ingest key 不正确，或者 OpenMIA 拒绝了 payload。
- `upload_failed`: 网络不可达、DNS 问题、TLS 问题，或 OpenMIA 服务暂时不可用。
- `state_write_failed`: state 主目录和 fallback 目录都不可写。OpenMIA Webhooker 会先尝试 `~/.codex/openmia-custom-json/state`，主目录不可写时自动使用 `/tmp/openmia-custom-json/state`。
- OpenMIA 没看到数据但日志 200: 检查 OpenMIA 的时间筛选、项目空间、ingestion job/raw payload 页面。
- Claude Code CLI 调用没有自动 trace: 默认不会覆盖系统 `claude` 命令。使用 `openmia-webhooker claude-code-run -- -p ...` 包装非交互调用，或后台运行 `openmia-webhooker claude-code-watch --follow` 读取本地 JSONL。

修复目录权限：

```bash
mkdir -p ~/.codex/openmia-custom-json/state
chmod 700 ~/.codex/openmia-custom-json
chmod 700 ~/.codex/openmia-custom-json/state
chmod 600 ~/.codex/openmia-custom-json.env
```

Windows 权限通常不需要 `chmod`。如果遇到权限问题，确认当前用户能读写 `$HOME\.codex\openmia-custom-json`，或者用 `OPENMIA_HOME`、`OPENMIA_STATE_DIR`、`OPENMIA_LOG_PATH` 指向可写位置。

## 11. 隐私和安全

- 不要把 `~/.codex/openmia-custom-json.env` 提交到 Git。
- 不要把 ingest key 发给别人或贴到 issue、日志、截图里。
- 如果 key 曾经公开展示，去 OpenMIA 轮换一个新 key。
- 生产环境默认建议 `OPENMIA_CAPTURE_TEXT=false`。
- 如果启用 `OPENMIA_CAPTURE_TEXT=true`，先确认团队允许采集 prompt、工具输入输出和回复正文。
- OpenMIA Webhooker 会对常见敏感字段做脱敏，但业务数据是否允许上传仍由你的隐私策略决定。
- Codex hidden reasoning 不会被 hook 暴露，因此 OpenMIA Webhooker 只会上报 reasoning placeholder span，不会上报隐藏推理内容。

## 12. 升级 OpenMIA Webhooker

升级到最新版本：

```bash
python3 -m pip install --user --upgrade openmia-webhooker
```

虚拟环境部署时：

```bash
source ~/.venvs/openmia/bin/activate
python3 -m pip install --upgrade openmia-webhooker
```

升级后验证：

```bash
openmia-webhooker --help
openmia-webhooker doctor
openmia-webhooker codex self_test --dry-run
openmia-webhooker claude-code-watch --once --dry-run
openmia-webhooker codex self_test
```

然后重启 Codex，并观察日志。

## 13. 迁移到新机器

新机器只需要：

1. 安装 OpenMIA Webhooker：

```bash
python3 -m pip install --user openmia-webhooker
```

2. 创建 `~/.codex/openmia-custom-json.env`。
3. 在 `~/.codex/config.toml` 加入 hook 配置。
4. 运行 dry-run 和真实 self-test。
5. 重启 Codex。

通常不要复制旧机器的 `state/*.json`，除非你明确想保留旧会话关联。

## 14. 快速检查清单

- `python3 --version` 正常。
- `python3 -m pip --version` 正常。
- `python3 -m pip show openmia-webhooker` 能看到 OpenMIA Webhooker。
- `openmia-webhooker --help` 正常。
- `openmia-webhooker doctor` 能显示 env/state/log 路径，且不会打印 ingest key。
- `~/.codex/openmia-custom-json.env` 存在，权限为 `600`。
- `~/.codex/config.toml` 已配置四个 hook。
- `openmia-webhooker codex self_test --dry-run` 能输出 JSON。
- `openmia-webhooker claude-code --dry-run`、`claude-code-run --help`、`claude-code-watch --help` 正常。
- `openmia-webhooker codex self_test` 能上传成功。
- 重启 Codex 后，真实对话事件能出现在 OpenMIA。
