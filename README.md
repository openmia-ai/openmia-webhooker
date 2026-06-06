# OpenMIA Webhooker

OpenMIA Webhooker is a small Python package for converting local agent events
into OpenMIA Runtime JSON v1 payloads and uploading them through the existing
OpenMIA `custom_json` webhook endpoint.

Current adapters:

- Codex hooks
- Claude Code `stream-json`, non-interactive wrapper and local JSONL watcher

## Runtime JSON v1

Webhooker owns source-specific semantics. It turns Codex, Claude Code and future
local agent/webhook events into a stable runtime shape:

```json
{
  "schemaVersion": "openmia.runtime.v1",
  "source": { "type": "codex", "adapter": "codex_hooks" },
  "session": { "id": "local-session" },
  "trace": { "id": "codex_custom_abc", "name": "Codex session" },
  "spans": [
    { "id": "codex_custom_abc_chat_turn_1", "parentId": null, "roundId": "turn_1", "type": "chat" }
  ]
}
```

Each local session maps to one stable trace. New user prompts or Claude Code
runs in that session append a top-level `chat` span with a stable `roundId`
under the same trace. The SDK does not emit empty `round` or session-summary
spans; OpenMIA can derive round grouping from `roundId` while the trace itself
represents the session.

The OpenMIA app server then performs raw-first storage, validation, tenant
isolation and normalized trace/span writes. This keeps Codex/Claude semantics in
the SDK layer and prevents the server from growing one adapter per local tool.

For compatibility, the payload is still sent to the `custom_json` webhook.

## Install

```bash
pip install openmia-webhooker
```

For local development:

```bash
python3 -m pip install -e .
```

## CLI

```bash
openmia-webhooker doctor
openmia-webhooker codex self_test --dry-run
openmia-webhooker codex user_prompt_submit
openmia-webhooker claude-code --dry-run
openmia-webhooker claude-code-run --dry-run -- -p "hello"
openmia-webhooker claude-code-watch --once --dry-run
openmia-webhooker claude-code-watch --follow
```

Claude Code support has three modes:

- `claude-code`: reads Claude Code `stream-json` from stdin for pipes, CI and debugging.
- `claude-code-run`: wraps non-interactive `claude -p/--print`, forces `--verbose --output-format stream-json`, uploads the trace, then prints Claude's final result. Use `--claude-command` or `OPENMIA_CLAUDE_COMMAND` if the executable is not named `claude`.
- `claude-code-watch`: reads Claude Code project JSONL files and uploads completed local rounds without wrapping the `claude` command. The default is `~/.claude/projects/**/*.jsonl`; use `--projects-dir` or `OPENMIA_CLAUDE_PROJECTS_DIR` to override it. In `--follow` mode, status is written to stderr; TTYs use an inline one-line refresh by default, while logs/CI fall back to line output.

Status display controls:

```bash
openmia-webhooker claude-code-watch --follow --status-style auto
openmia-webhooker claude-code-watch --follow --status-style inline
openmia-webhooker claude-code-watch --follow --status-style line
openmia-webhooker claude-code-watch --follow --status-style off
openmia-webhooker claude-code-watch --follow --no-status
```

## Deployment Guide

See [docs/OPENMIA_CUSTOM_JSON_DEPLOYMENT.md](docs/OPENMIA_CUSTOM_JSON_DEPLOYMENT.md)
for Linux/macOS and Windows deployment steps, Codex hook configuration, Claude
Code wrapper/watcher setup, privacy settings and troubleshooting.
For more OpenMIA trace tutorials and product concepts, see
https://docs.openmia.ai/app/observation/traces.

## Configuration Paths

Webhooker keeps the existing local file contract:

- env file: `~/.codex/openmia-custom-json.env`
- state: `~/.codex/openmia-custom-json/state`
- log: `~/.codex/openmia-custom-json/collector.log`

`OPENMIA_CAPTURE_TEXT` defaults to `true`, so prompt text, tool input/output
and assistant output are uploaded to OpenMIA traces unless explicitly disabled.
Set `OPENMIA_CAPTURE_TEXT=false` in the env file to upload only length and
sha256 summaries. The local collector log records upload status and server
responses only; it does not store raw prompt or tool text.

Path resolution is cross-platform and configurable. Explicit code config wins,
then `OPENMIA_HOME`, then compatible `CODEX_HOME`, then `Path.home() / ".codex"`.
Advanced overrides are available through `OPENMIA_CUSTOM_JSON_ENV_FILE`,
`OPENMIA_STATE_DIR` and `OPENMIA_LOG_PATH`.

## Test

```bash
python3 -m unittest discover tests
```

## License

Apache-2.0. See [LICENSE](LICENSE).
