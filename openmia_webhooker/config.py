from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

DEFAULT_ENDPOINT = "https://app.openmia.ai/api/observation/webhooks/custom_json?payload_type=json"
DEFAULT_CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "/root/.codex"))


@dataclass(frozen=True)
class OpenMIAConfig:
    endpoint: str = DEFAULT_ENDPOINT
    ingest_key: str = ""
    capture_text: bool = False
    base_dir: pathlib.Path = DEFAULT_CODEX_HOME

    @property
    def env_path(self) -> pathlib.Path:
        return self.base_dir / "openmia-custom-json.env"

    @property
    def state_dir(self) -> pathlib.Path:
        return self.base_dir / "openmia-custom-json" / "state"

    @property
    def log_path(self) -> pathlib.Path:
        return self.base_dir / "openmia-custom-json" / "collector.log"

    @property
    def codex_config_path(self) -> pathlib.Path:
        return self.base_dir / "config.toml"

    @classmethod
    def from_env(cls, base_dir: pathlib.Path | str | None = None) -> "OpenMIAConfig":
        resolved_base_dir = pathlib.Path(base_dir) if base_dir is not None else DEFAULT_CODEX_HOME
        values = _read_env_file(resolved_base_dir / "openmia-custom-json.env")
        values.update({k: v for k, v in os.environ.items() if k.startswith("OPENMIA_")})
        return cls(
            endpoint=values.get("OPENMIA_CUSTOM_JSON_ENDPOINT", DEFAULT_ENDPOINT),
            ingest_key=values.get("OPENMIA_CUSTOM_JSON_INGEST_KEY", ""),
            capture_text=_truthy(values.get("OPENMIA_CAPTURE_TEXT", "false")),
            base_dir=resolved_base_dir,
        )


def _read_env_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}
