from __future__ import annotations

import json
import urllib.request
from typing import Any

from .config import OpenMIAConfig


class OpenMIAClient:
    """HTTP client for OpenMIA custom_json ingestion."""

    def __init__(
        self,
        endpoint: str,
        ingest_key: str,
        timeout: int = 10,
        user_agent: str = "openmia-webhooker/0.1",
    ) -> None:
        self.endpoint = endpoint
        self.ingest_key = ingest_key
        self.timeout = timeout
        self.user_agent = user_agent

    @classmethod
    def from_config(cls, config: OpenMIAConfig) -> "OpenMIAClient":
        return cls(config.endpoint, config.ingest_key)

    def post_trace(self, trace: dict[str, Any]) -> tuple[int, str]:
        body = json.dumps(trace, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.ingest_key}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
                "X-OpenMIA-Source": "openmia-webhooker",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.status, response.read(4096).decode("utf-8", errors="replace")
