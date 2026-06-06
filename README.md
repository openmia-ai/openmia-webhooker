# OpenMIA Webhooker

OpenMIA Webhooker is a small Python package for converting local agent events
into OpenMIA `custom_json` traces and uploading them through a webhook endpoint.

Current adapters:

- Codex hooks
- Claude Code `stream-json`

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
