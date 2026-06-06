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
openmia-webhooker codex self_test --dry-run
openmia-webhooker codex user_prompt_submit
openmia-webhooker claude-code --dry-run
openmia-webhooker claude-code-run --dry-run -- -p "hello"
openmia-webhooker claude-code-watch --once --dry-run
```

Claude Code support has three modes:

- `claude-code`: reads Claude Code `stream-json` from stdin for pipes, CI and debugging.
- `claude-code-run`: wraps non-interactive `claude -p/--print`, forces `--verbose --output-format stream-json --include-hook-events`, uploads the trace, then prints Claude's final result.
- `claude-code-watch`: reads `~/.claude/projects/**/*.jsonl` and uploads completed local rounds without wrapping the `claude` command.

See [docs/OPENMIA_CUSTOM_JSON_DEPLOYMENT.md](docs/OPENMIA_CUSTOM_JSON_DEPLOYMENT.md)
for Codex deployment notes.

## Test

```bash
python3 -m unittest discover tests
```
