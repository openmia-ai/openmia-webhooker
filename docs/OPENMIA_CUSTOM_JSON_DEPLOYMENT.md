# OpenMIA Webhooker Deployment Guide

This guide explains how to deploy OpenMIA Webhooker so local Codex hook events
and Claude Code events are converted into OpenMIA Runtime JSON v1 payloads and
uploaded through the existing OpenMIA `custom_json` webhook endpoint.

The examples assume OpenMIA Webhooker is available from PyPI. Linux/macOS
examples use `python3` and a POSIX shell. Windows examples use PowerShell.

```bash
pip install openmia-webhooker
```

OpenMIA Webhooker exposes one CLI:

```bash
openmia-webhooker
```

For more OpenMIA trace concepts and product tutorials, see:

https://docs.openmia.ai/app/observation/traces

## 1. Architecture

After deployment, Codex events flow through this path:

```text
Codex hooks
  -> openmia-webhooker codex
  -> openmia_webhooker.adapters.codex
  -> OpenMIA Runtime JSON v1
  -> OpenMIA custom_json endpoint
```

OpenMIA Webhooker is responsible for:

- Reading Codex hook payloads from stdin.
- Recognizing `UserPromptSubmit`, `Stop`, `PreToolUse`, and `PostToolUse`.
- Building Runtime JSON v1 with sessions, traces, top-level chat spans, tool
  spans, and reasoning placeholder spans.
- Uploading either raw text or length/hash summaries based on configuration.
- Maintaining local state so one local session maps to one stable trace, with
  later conversation rounds appended as new top-level chat spans.
- Uploading to the existing OpenMIA custom_json ingestion endpoint. The server
  remains responsible for raw-first storage, tenant isolation, validation, and
  normalized trace/span writes.

Runtime JSON v1 uses `schemaVersion = "openmia.runtime.v1"`. Webhooker owns the
Codex hook and Claude Code stream-json semantics; the OpenMIA app server does
not need a separate adapter for every local tool. The endpoint remains the
existing `custom_json` webhook endpoint.

OpenMIA Webhooker uses these local files. `~` means the current user's home
directory. On Windows, it is equivalent to `$HOME` or `$env:USERPROFILE`.

- `~/.codex/openmia-custom-json.env`: OpenMIA endpoint, ingest key, and text
  capture settings.
- `~/.codex/config.toml`: Codex hook configuration.
- `~/.codex/openmia-custom-json/collector.log`: upload status log.
- `~/.codex/openmia-custom-json/state/*.json`: local session state.

Codex and Claude Code share the same display semantics: a trace represents one
local session, and each user round is a top-level `chat` span with
`parentId = null` and a stable `roundId`. Webhooker does not emit empty `round`
spans or session-summary workflow spans.

## 2. Prerequisites

The target machine needs:

- Python 3.10 or newer.
- `pip`.
- Codex installed and working, if deploying Codex hooks.
- Claude Code installed and working, if deploying Claude Code collection.
- An OpenMIA custom_json ingestion endpoint.
- An OpenMIA custom_json ingest key.
- Network access to `https://app.openmia.ai`.

Check Python and pip:

```bash
python3 --version
python3 -m pip --version
```

Windows PowerShell:

```powershell
py --version
py -m pip --version
```

## 3. Install OpenMIA Webhooker

Linux/macOS user install:

```bash
python3 -m pip install --user openmia-webhooker
```

Virtual environment:

```bash
python3 -m venv ~/.venvs/openmia
source ~/.venvs/openmia/bin/activate
python3 -m pip install openmia-webhooker
```

Windows PowerShell user install:

```powershell
py -m pip install --user openmia-webhooker
```

Windows virtual environment:

```powershell
py -m venv $HOME\.venvs\openmia
& $HOME\.venvs\openmia\Scripts\Activate.ps1
py -m pip install openmia-webhooker
```

Verify the CLI:

```bash
openmia-webhooker --help
```

If the command is not on `PATH`, either add the Python user scripts directory to
`PATH` or use the absolute executable path in hook commands.

Linux/macOS:

```bash
python3 -m site --user-base
```

The scripts directory is usually:

```text
~/.local/bin
```

Windows PowerShell:

```powershell
py -m site --user-site
```

The corresponding scripts directory is usually under:

```text
%APPDATA%\Python\Python3xx\Scripts
```

Run diagnostics:

```bash
openmia-webhooker doctor
```

## 4. Configure OpenMIA

Create the state directory:

```bash
mkdir -p ~/.codex/openmia-custom-json/state
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.codex\openmia-custom-json\state" | Out-Null
```

Create `~/.codex/openmia-custom-json.env`:

```bash
OPENMIA_CUSTOM_JSON_ENDPOINT=https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json
OPENMIA_CUSTOM_JSON_INGEST_KEY=<your_openmia_ingest_key>
OPENMIA_CAPTURE_TEXT=true
```

Windows PowerShell:

```powershell
@"
OPENMIA_CUSTOM_JSON_ENDPOINT=https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json
OPENMIA_CUSTOM_JSON_INGEST_KEY=<your_openmia_ingest_key>
OPENMIA_CAPTURE_TEXT=true
"@ | Set-Content -Encoding utf8 "$HOME\.codex\openmia-custom-json.env"
```

Fields:

- `OPENMIA_CUSTOM_JSON_ENDPOINT`: OpenMIA custom_json webhook URL.
- `OPENMIA_CUSTOM_JSON_INGEST_KEY`: OpenMIA ingest key. Do not commit it.
- `OPENMIA_CAPTURE_TEXT`: whether to upload prompt text, tool input/output,
  and assistant output. The default is `true` when the variable is omitted.

The default uploads raw text so traces are complete in OpenMIA. If your privacy
policy does not allow uploading raw text, explicitly disable it:

```bash
OPENMIA_CAPTURE_TEXT=false
```

When disabled, Webhooker uploads length and sha256 summaries instead of raw
text. Re-enable full traces with:

```bash
OPENMIA_CAPTURE_TEXT=true
```

On Linux/macOS, restrict env file permissions:

```bash
chmod 600 ~/.codex/openmia-custom-json.env
```

Path resolution priority:

1. Explicit `base_dir` passed in code.
2. `OPENMIA_HOME`.
3. Compatible `CODEX_HOME`.
4. `Path.home() / ".codex"`.

Advanced path overrides:

- `OPENMIA_CUSTOM_JSON_ENV_FILE`: full path to the env file.
- `OPENMIA_STATE_DIR`: full path to the state directory.
- `OPENMIA_LOG_PATH`: full path to `collector.log`.

Temporary Linux/macOS override:

```bash
export OPENMIA_HOME="$HOME/.codex"
```

Temporary Windows PowerShell override:

```powershell
$env:OPENMIA_HOME = "$HOME\.codex"
```

## 5. Enable Codex Hooks

Merge the following configuration into `~/.codex/config.toml`. If a hook block
for the same event already exists, do not add a second top-level
`[[hooks.<Event>]]` block. Instead, add the `[[hooks.<Event>.hooks]]` command
entry under the existing event block so each event has only one top-level block.

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
matcher = "*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "openmia-webhooker codex pre_tool_use"
timeout = 15
statusMessage = "Sending OpenMIA tool telemetry"

[[hooks.PostToolUse]]
matcher = "*"
[[hooks.PostToolUse.hooks]]
type = "command"
command = "openmia-webhooker codex post_tool_use"
timeout = 20
statusMessage = "Sending OpenMIA tool result telemetry"
```

If the Codex hook environment cannot find `openmia-webhooker`, use an absolute
path:

```toml
command = "/home/you/.local/bin/openmia-webhooker codex user_prompt_submit"
```

Virtual environment:

```toml
command = "/home/you/.venvs/openmia/bin/openmia-webhooker codex user_prompt_submit"
```

Windows user install with a path that does not contain spaces:

```toml
command = "C:\\Users\\you\\AppData\\Roaming\\Python\\Python313\\Scripts\\openmia-webhooker.exe codex user_prompt_submit"
```

If the Windows user path contains spaces, create a `.cmd` wrapper in a path
without spaces. This avoids quoting differences between hook runners and
Windows shells.

```bat
@echo off
"C:\Users\you name\AppData\Roaming\Python\Python313\Scripts\openmia-webhooker.exe" %*
```

Then use the wrapper in `config.toml`:

```toml
command = "W:\\tools\\openmia-webhooker-codex-hook.cmd codex user_prompt_submit"
```

Windows virtual environment with a path that does not contain spaces:

```toml
command = "C:\\Users\\you\\.venvs\\openmia\\Scripts\\openmia-webhooker.exe codex user_prompt_submit"
```

## 6. Local Dry-Run Validation

Dry-run commands do not upload data. They print the JSON payload that would be
sent. To avoid leaking sensitive text in local terminals and CI logs, dry-run
output is redacted. Even when `OPENMIA_CAPTURE_TEXT=true`, do not use dry-run
output to decide whether the OpenMIA platform will show raw text. Use
`openmia-webhooker doctor --json` to confirm `capture_text`, then verify raw
text in a real uploaded trace.

Self-test:

```bash
openmia-webhooker codex self_test --dry-run
```

Simulate a user prompt:

```bash
printf '{"conversation_id":"deploy_test","prompt":"hello from dry run"}' \
  | openmia-webhooker codex user_prompt_submit --dry-run
```

Simulate a tool call start:

```bash
printf '{"conversation_id":"deploy_test","tool_call_id":"call-1","tool_name":"exec_command","cmd":"python3 --version"}' \
  | openmia-webhooker codex pre_tool_use --dry-run
```

Simulate a tool call result:

```bash
printf '{"conversation_id":"deploy_test","tool_call_id":"call-1","tool_name":"exec_command","stdout":"Python 3.x"}' \
  | openmia-webhooker codex post_tool_use --dry-run
```

Simulate a stop event:

```bash
printf '{"conversation_id":"deploy_test","output":"completed"}' \
  | openmia-webhooker codex stop --dry-run
```

If these commands print JSON, the CLI, config loading, and local state writes are
working.

## 7. Real Upload Validation

After configuring the ingest key, run:

```bash
openmia-webhooker codex self_test
```

Check the log:

```bash
tail -n 20 ~/.codex/openmia-custom-json/collector.log
```

Success looks like:

```text
"message": "uploaded status 200"
"success": true
```

Then open the OpenMIA console and search recent traces for
`Codex custom JSON collector self-test` or `Codex session`.

## 8. Claude Code Support

Claude Code has three entry points. All emit `schemaVersion =
"openmia.runtime.v1"` and use the same custom_json endpoint.

### stdin adapter

Use this for pipes, CI, and debugging. It reads Claude Code `stream-json` from
stdin:

```bash
claude -p "hello" --verbose --output-format stream-json \
  | openmia-webhooker claude-code
```

Dry-run:

```bash
printf '{"type":"system","subtype":"init","session_id":"claude-test"}\n' \
  | openmia-webhooker claude-code --dry-run
```

### non-interactive wrapper

Use this when you do not want to manually pass Claude Code `stream-json`
arguments. It supports only non-interactive `--print/-p` runs:

```bash
openmia-webhooker claude-code-run -- -p "hello"
```

OpenMIA Webhooker automatically appends:

```text
--verbose --output-format stream-json
```

After upload, the wrapper prints Claude Code's final result text to stdout.
With `--dry-run`, it prints the OpenMIA payload and does not upload:

```bash
openmia-webhooker claude-code-run --dry-run -- -p "hello"
```

If the Claude executable is not named `claude`, pass it explicitly:

```bash
openmia-webhooker claude-code-run --claude-command /path/to/claude -- -p "hello"
```

Windows PowerShell:

```powershell
openmia-webhooker claude-code-run --claude-command "C:\Path\To\claude.exe" -- -p "hello"
```

If your installed Claude Code version rejects an argument, run:

```bash
claude --help
```

and prefer the stdin adapter form shown above. Current Claude Code versions
support `--verbose --output-format stream-json`; older instructions that include
`--include-hook-events` may fail on newer Claude Code releases.

### local JSONL watcher

Use this when existing Claude Code CLI calls are not wrapped. By default it
reads:

```text
~/.claude/projects/**/*.jsonl
```

One-time scan:

```bash
openmia-webhooker claude-code-watch --once
```

Follow mode waits until a round has been idle for 10 seconds before upload, so
it does not upload incomplete assistant/tool events too early:

```bash
openmia-webhooker claude-code-watch --follow
```

`--follow` writes status to stderr. TTYs use an inline one-line refresh by
default; logs and CI fall back to line output. Status includes the projects
directory, dry-run state, idle flush interval, poll interval, ready/pending
round counts, uploaded/printed/skipped/failed counts, next scan countdown, and
pending flush countdown.

Status controls:

```bash
openmia-webhooker claude-code-watch --follow --status-style auto
openmia-webhooker claude-code-watch --follow --status-style inline
openmia-webhooker claude-code-watch --follow --status-style line
openmia-webhooker claude-code-watch --follow --status-style off
openmia-webhooker claude-code-watch --follow --no-status
```

Custom projects directory:

```bash
openmia-webhooker claude-code-watch --once --projects-dir /path/to/.claude/projects
```

Windows PowerShell:

```powershell
openmia-webhooker claude-code-watch --once --projects-dir "$HOME\.claude\projects"
```

The watcher starts a new round only on real user prompt lines. Some Claude Code
JSONL `type=user` rows are tool results; Webhooker keeps those inside the
current round. Later assistant/tool/result/error events stay in the current
round until the next real user prompt or idle flush. Uploaded rounds are stored
in `~/.codex/openmia-custom-json/state`, so repeated `--once` scans do not
upload the same round again.

Environment overrides:

```bash
export OPENMIA_CLAUDE_COMMAND="/path/to/claude"
export OPENMIA_CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"
```

Windows PowerShell:

```powershell
$env:OPENMIA_CLAUDE_COMMAND = "C:\Path\To\claude.exe"
$env:OPENMIA_CLAUDE_PROJECTS_DIR = "$HOME\.claude\projects"
```

### Windows wrappers

For Windows machines with spaces in user paths, prefer `.cmd` wrappers in a path
without spaces.

Non-interactive Claude wrapper:

```bat
@echo off
"C:\Users\you name\.local\bin\claude.exe" %* --verbose --output-format stream-json | "C:\Users\you name\AppData\Roaming\Python\Python312\Scripts\openmia-webhooker.exe" claude-code
```

Watcher wrapper:

```bat
@echo off
"C:\Users\you name\AppData\Roaming\Python\Python312\Scripts\openmia-webhooker.exe" claude-code-watch --follow --projects-dir "C:\Users\you name\.claude\projects" --status-style line %*
```

Start the watcher in the background from PowerShell:

```powershell
Start-Process -FilePath "cmd.exe" `
  -ArgumentList @("/c", "W:\tools\openmia-claude-watch.cmd") `
  -WindowStyle Hidden
```

## 9. Make Codex Hooks Take Effect

After updating `config.toml`:

1. Close the current Codex/IDE session.
2. Reopen Codex.
3. Send a simple message.
4. Let Codex run a small tool call, such as `python3 --version`.
5. Check `~/.codex/openmia-custom-json/collector.log` for a new
   `uploaded status 200` entry.

Some Codex environments ask you to trust hook commands the first time they run.
After confirming the command path and Webhooker source, allow the hook.

## 10. Logs and Troubleshooting

Run diagnostics first. It does not upload, does not write state, and does not
print the ingest key:

```bash
openmia-webhooker doctor
```

Machine-readable output:

```bash
openmia-webhooker doctor --json
```

View recent logs:

```bash
tail -n 50 ~/.codex/openmia-custom-json/collector.log
```

`collector.log` records upload status, HTTP status, trace id, and server
responses only. It does not store prompts, tool input/output, or assistant
responses. Raw text upload is controlled by `OPENMIA_CAPTURE_TEXT` and appears
in the OpenMIA trace payload.

Common issues:

- `openmia-webhooker: command not found`: CLI is not on `PATH`. Use an absolute
  path or add the Python scripts directory to `PATH`.
- `OPENMIA_CUSTOM_JSON_INGEST_KEY missing`: env file is missing, path is wrong,
  or the key is not set.
- `capture_text` is `false`: `openmia-webhooker doctor --json` shows the current
  value. Set `OPENMIA_CAPTURE_TEXT=true` if the platform trace should include
  raw text.
- Claude command not found: run `openmia-webhooker doctor`, then set
  `--claude-command` or `OPENMIA_CLAUDE_COMMAND`.
- Claude returns `Not logged in`: run `claude auth login` in a real terminal.
- `http_error`: endpoint or ingest key is wrong, or OpenMIA rejected the
  payload.
- `upload_failed`: network, DNS, TLS, or OpenMIA availability issue.
- `state_write_failed`: primary and fallback state directories are not
  writable.
- OpenMIA does not show data even though logs show HTTP 200: check OpenMIA time
  filters, workspace/project selection, ingestion job pages, and raw payload
  pages.
- Claude Code CLI calls do not automatically trace: Webhooker does not replace
  the system `claude` command. Use `openmia-webhooker claude-code-run -- -p ...`
  or run `openmia-webhooker claude-code-watch --follow`.

Repair directory permissions on Linux/macOS:

```bash
mkdir -p ~/.codex/openmia-custom-json/state
chmod 700 ~/.codex/openmia-custom-json
chmod 700 ~/.codex/openmia-custom-json/state
chmod 600 ~/.codex/openmia-custom-json.env
```

Windows usually does not need `chmod`. If permission problems occur, confirm the
current user can read/write `$HOME\.codex\openmia-custom-json`, or use
`OPENMIA_HOME`, `OPENMIA_STATE_DIR`, and `OPENMIA_LOG_PATH` to point to writable
locations.

## 11. Privacy and Security

- Do not commit `~/.codex/openmia-custom-json.env`.
- Do not share ingest keys in issues, logs, screenshots, or chat.
- If a key was exposed, rotate it in OpenMIA.
- `OPENMIA_CAPTURE_TEXT=true` is the default and uploads prompts, tool
  input/output, and assistant responses so traces are complete.
- If your privacy policy does not allow raw text collection, set
  `OPENMIA_CAPTURE_TEXT=false`.
- Webhooker redacts common sensitive fields, but your privacy policy still
  determines whether business data may be uploaded.
- Codex hidden reasoning is not exposed to hooks. Webhooker reports reasoning
  placeholder spans only; it never uploads hidden reasoning content.

## 12. Upgrade OpenMIA Webhooker

Upgrade to the latest version:

```bash
python3 -m pip install --user --upgrade openmia-webhooker
```

Virtual environment:

```bash
source ~/.venvs/openmia/bin/activate
python3 -m pip install --upgrade openmia-webhooker
```

Verify after upgrade:

```bash
openmia-webhooker --help
openmia-webhooker doctor
openmia-webhooker codex self_test --dry-run
openmia-webhooker claude-code-watch --once --dry-run
openmia-webhooker codex self_test
```

Then restart Codex and inspect logs.

## 13. Migrate to a New Machine

On a new machine:

1. Install OpenMIA Webhooker.
2. Create `~/.codex/openmia-custom-json.env`.
3. Add Codex hook configuration to `~/.codex/config.toml`.
4. Run dry-run and real self-test validation.
5. Restart Codex.

Usually do not copy old `state/*.json` files unless you intentionally want to
preserve old local session associations.

## 14. Quick Checklist

- `python3 --version` works.
- `python3 -m pip --version` works.
- `python3 -m pip show openmia-webhooker` finds the package.
- `openmia-webhooker --help` works.
- `openmia-webhooker doctor` shows env/state/log paths and does not print the
  ingest key.
- `openmia-webhooker doctor --json` shows the expected `capture_text` value.
- `~/.codex/openmia-custom-json.env` exists.
- `~/.codex/config.toml` has the four Codex hooks.
- `openmia-webhooker codex self_test --dry-run` prints JSON.
- `openmia-webhooker claude-code --dry-run`, `claude-code-run --help`, and
  `claude-code-watch --help` work.
- `openmia-webhooker codex self_test` uploads successfully.
- After restarting Codex, real conversation events appear in OpenMIA.
