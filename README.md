# OpenMIA Webhooker

OpenMIA Webhooker is a small Python package for converting local agent events
into OpenMIA Runtime JSON v1 payloads and uploading them through the existing
OpenMIA `custom_json` webhook endpoint.

Current adapters:

- Codex hooks
- Claude Code `stream-json`

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
    { "id": "codex_custom_abc_root", "name": "Codex session", "type": "workflow" },
    { "id": "codex_custom_abc_round_turn_1", "parentId": null, "roundId": "turn_1", "type": "round" },
    { "id": "codex_custom_abc_chat_turn_1", "parentId": "codex_custom_abc_round_turn_1", "roundId": "turn_1", "type": "chat" }
  ]
}
```

Each local session maps to one stable trace. New user prompts or Claude Code
runs in that session append a top-level `round` span and a child `chat` span
under the same trace. The session/root span and round spans are siblings so UI
trees can show them at the same indentation level.

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
```

See [docs/OPENMIA_CUSTOM_JSON_DEPLOYMENT.md](docs/OPENMIA_CUSTOM_JSON_DEPLOYMENT.md)
for Codex deployment notes.

## Test

```bash
python3 -m unittest discover tests
```
