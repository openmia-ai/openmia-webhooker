# OpenMIA Webhooker Codex Deployment Tutorial

这份教程用于部署 PyPI 版 OpenMIA Webhooker，让 Codex 的 hook 事件被转换成 OpenMIA Runtime JSON v1，并通过现有 custom_json webhook 上传到 OpenMIA。

本文假设 OpenMIA Webhooker 已经发布到 PyPI：

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
- 构造 Runtime JSON v1：session、trace、chat turn、tool span、reasoning placeholder span。
- 根据配置决定上传明文或 hash 摘要。
- 维护本地 state，把同一轮 prompt、工具调用和 stop 事件关联起来。
- 上传到 OpenMIA custom_json ingestion endpoint；服务端只负责 raw-first 存储、权限隔离和标准化写入。

Runtime JSON v1 使用 `schemaVersion = "openmia.runtime.v1"`。SDK/webhooker 侧负责理解 Codex hook 语义；OpenMIA app 服务端不再为每个本地工具复制一套来源专属语义 adapter。

本地仍需要维护这些文件：

- `~/.codex/openmia-custom-json.env`: OpenMIA endpoint、ingest key、正文采集开关。
- `~/.codex/config.toml`: Codex hook 配置。
- `~/.codex/openmia-custom-json/collector.log`: OpenMIA Webhooker 上传日志。
- `~/.codex/openmia-custom-json/state/*.json`: 会话关联 state。

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

## 3. 安装 OpenMIA Webhooker

推荐使用当前用户安装：

```bash
python3 -m pip install --user openmia-webhooker
```

如果你使用虚拟环境：

```bash
python3 -m venv ~/.venvs/openmia
source ~/.venvs/openmia/bin/activate
python3 -m pip install openmia-webhooker
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

## 4. 配置 OpenMIA

创建目录：

```bash
mkdir -p ~/.codex/openmia-custom-json/state
```

创建 `~/.codex/openmia-custom-json.env`：

```bash
OPENMIA_CUSTOM_JSON_ENDPOINT=https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json
OPENMIA_CUSTOM_JSON_INGEST_KEY=<your_openmia_ingest_key>
OPENMIA_CAPTURE_TEXT=false
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
command = "/root/.local/bin/openmia-webhooker codex user_prompt_submit"
```

虚拟环境部署时可以这样写：

```toml
command = "/root/.venvs/openmia/bin/openmia-webhooker codex user_prompt_submit"
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

## 8. 让 Codex 生效

更新 `config.toml` 后：

1. 关闭当前 Codex/IDE 会话。
2. 重新打开 Codex。
3. 发送一条简单消息。
4. 让 Codex 执行一个小工具调用，例如 `python3 --version`。
5. 检查 `~/.codex/openmia-custom-json/collector.log` 是否出现新的 `uploaded status 200`。

有些 Codex 环境首次运行 hook 时会要求信任 hook 命令。确认命令路径和 OpenMIA Webhooker 来源无误后，允许该 hook。

## 9. 日志和排障

查看最近日志：

```bash
tail -n 50 ~/.codex/openmia-custom-json/collector.log
```

常见问题：

- `openmia-webhooker: command not found`: CLI 不在 `PATH`。改用绝对路径，或把 `~/.local/bin` 加入 `PATH`。
- `OPENMIA_CUSTOM_JSON_INGEST_KEY missing`: `.env` 文件不存在、路径不对，或 key 没写。
- `http_error`: endpoint 或 ingest key 不正确，或者 OpenMIA 拒绝了 payload。
- `upload_failed`: 网络不可达、DNS 问题、TLS 问题，或 OpenMIA 服务暂时不可用。
- `state_write_failed`: state 主目录和 fallback 目录都不可写。OpenMIA Webhooker 会先尝试 `~/.codex/openmia-custom-json/state`，主目录不可写时自动使用 `/tmp/openmia-custom-json/state`。
- OpenMIA 没看到数据但日志 200: 检查 OpenMIA 的时间筛选、项目空间、ingestion job/raw payload 页面。

修复目录权限：

```bash
mkdir -p ~/.codex/openmia-custom-json/state
chmod 700 ~/.codex/openmia-custom-json
chmod 700 ~/.codex/openmia-custom-json/state
chmod 600 ~/.codex/openmia-custom-json.env
```

## 10. 隐私和安全

- 不要把 `~/.codex/openmia-custom-json.env` 提交到 Git。
- 不要把 ingest key 发给别人或贴到 issue、日志、截图里。
- 如果 key 曾经公开展示，去 OpenMIA 轮换一个新 key。
- 生产环境默认建议 `OPENMIA_CAPTURE_TEXT=false`。
- 如果启用 `OPENMIA_CAPTURE_TEXT=true`，先确认团队允许采集 prompt、工具输入输出和回复正文。
- OpenMIA Webhooker 会对常见敏感字段做脱敏，但业务数据是否允许上传仍由你的隐私策略决定。
- Codex hidden reasoning 不会被 hook 暴露，因此 OpenMIA Webhooker 只会上报 reasoning placeholder span，不会上报隐藏推理内容。

## 11. 升级 OpenMIA Webhooker

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
openmia-webhooker codex self_test --dry-run
openmia-webhooker codex self_test
```

然后重启 Codex，并观察日志。

## 12. 迁移到新机器

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

## 13. 快速检查清单

- `python3 --version` 正常。
- `python3 -m pip --version` 正常。
- `python3 -m pip show openmia-webhooker` 能看到 OpenMIA Webhooker。
- `openmia-webhooker --help` 正常。
- `~/.codex/openmia-custom-json.env` 存在，权限为 `600`。
- `~/.codex/config.toml` 已配置四个 hook。
- `openmia-webhooker codex self_test --dry-run` 能输出 JSON。
- `openmia-webhooker codex self_test` 能上传成功。
- 重启 Codex 后，真实对话事件能出现在 OpenMIA。
