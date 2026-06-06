from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

DEFAULT_ENDPOINT = "https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json"
DEFAULT_CODEX_HOME = pathlib.Path.home() / ".codex"
ENV_FILE_NAME = "openmia-custom-json.env"


@dataclass(frozen=True)
class OpenMIAConfig:
    endpoint: str = DEFAULT_ENDPOINT
    ingest_key: str = ""
    capture_text: bool = True
    base_dir: pathlib.Path = DEFAULT_CODEX_HOME
    env_file_path: pathlib.Path | None = None
    state_dir_path: pathlib.Path | None = None
    log_file_path: pathlib.Path | None = None

    @property
    def env_path(self) -> pathlib.Path:
        return self.env_file_path or self.base_dir / ENV_FILE_NAME

    @property
    def state_dir(self) -> pathlib.Path:
        return self.state_dir_path or self.base_dir / "openmia-custom-json" / "state"

    @property
    def log_path(self) -> pathlib.Path:
        return self.log_file_path or self.base_dir / "openmia-custom-json" / "collector.log"

    @property
    def codex_config_path(self) -> pathlib.Path:
        return self.base_dir / "config.toml"

    @classmethod
    def from_env(cls, base_dir: pathlib.Path | str | None = None) -> "OpenMIAConfig":
        resolved_base_dir = resolve_base_dir(base_dir)
        env_file_path = _optional_path(os.environ.get("OPENMIA_CUSTOM_JSON_ENV_FILE")) or resolved_base_dir / ENV_FILE_NAME
        values = _read_env_file(env_file_path)
        values.update({k: v for k, v in os.environ.items() if k.startswith("OPENMIA_") and v != ""})
        return cls(
            endpoint=values.get("OPENMIA_CUSTOM_JSON_ENDPOINT", DEFAULT_ENDPOINT),
            ingest_key=values.get("OPENMIA_CUSTOM_JSON_INGEST_KEY", ""),
            capture_text=_truthy(values.get("OPENMIA_CAPTURE_TEXT", "true")),
            base_dir=resolved_base_dir,
            env_file_path=env_file_path,
            state_dir_path=_optional_path(values.get("OPENMIA_STATE_DIR")),
            log_file_path=_optional_path(values.get("OPENMIA_LOG_PATH")),
        )


def resolve_base_dir(base_dir: pathlib.Path | str | None = None) -> pathlib.Path:
    if base_dir is not None:
        return pathlib.Path(base_dir).expanduser()
    for key in ("OPENMIA_HOME", "CODEX_HOME"):
        value = os.environ.get(key)
        if value:
            return pathlib.Path(value).expanduser()
    return DEFAULT_CODEX_HOME


def _read_env_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        stripped = val.strip().strip('"').strip("'")
        if stripped != "":
            values[key.strip()] = stripped
    return values


def _optional_path(value: str | None) -> pathlib.Path | None:
    return pathlib.Path(value).expanduser() if value else None


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}
