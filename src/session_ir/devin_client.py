"""Minimal zero-dependency client for Devin v3 Control Plane API."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

DEVIN_V3_BASE = "https://api.devin.ai/v3"


class DevinV3Client:
    """Thin client for creating sessions with structured_output_schema and harvesting traces."""

    def __init__(self, api_key: str | None = None, org_id: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("DEVIN_API_KEY", "")
        self.org_id = org_id or os.environ.get("DEVIN_ORG_ID", "")
        if not self.api_key or not self.org_id:
            raise ValueError("DEVIN_API_KEY (cog_*) and DEVIN_ORG_ID (org-*) are required")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{DEVIN_V3_BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def create_investigation_session(
        self,
        prompt: str,
        schema_path: str | Path,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        payload: dict[str, Any] = {
            "prompt": prompt,
            "structured_output_schema": schema,
            "structured_output_required": True,
        }
        if title:
            payload["title"] = title
        if tags:
            payload["tags"] = tags
        return self._request("POST", f"/organizations/{self.org_id}/sessions", payload)

    def get_session(self, devin_id: str) -> dict[str, Any]:
        return self._request("GET", f"/organizations/{self.org_id}/sessions/{devin_id}")

    def list_messages(self, devin_id: str, *, first: int = 100, after: str | None = None) -> dict[str, Any]:
        qs = f"?first={first}" + (f"&after={after}" if after else "")
        return self._request("GET", f"/organizations/{self.org_id}/sessions/{devin_id}/messages{qs}")

    def list_attachments(self, devin_id: str) -> dict[str, Any]:
        return self._request("GET", f"/organizations/{self.org_id}/sessions/{devin_id}/attachments")
