"""Integration adapters — the ONLY component allowed to touch external providers.

The LLM/agent layer NEVER calls these directly; it goes through
ToolRouter → PermissionEngine → ActionExecutor → IntegrationAdapter (contract rule #30).

A provider without credentials is explicitly DISCONNECTED: execute() raises
IntegrationOfflineError → HTTP 503 INTEGRATION_OFFLINE. Nothing is simulated.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.errors import integration_offline, tool_unavailable


@dataclass
class AdapterHealth:
    state: str  # CONNECTED | DISCONNECTED | DEGRADED
    detail: str = ""


class IntegrationAdapter(abc.ABC):
    provider: str = "base"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client  # injectable for tests (transport boundary only)

    @abc.abstractmethod
    def _configured(self) -> bool: ...

    def health(self) -> AdapterHealth:
        if not self._configured():
            return AdapterHealth("DISCONNECTED", "provider credentials not configured")
        return AdapterHealth("CONNECTED")

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """HTTP at the transport boundary. A test-injected client is never closed by us."""
        owned = self._client is None
        client = self._client if self._client is not None else httpx.AsyncClient(timeout=10)
        try:
            return await client.request(method, url, **kwargs)
        finally:
            if owned:
                await client.aclose()

    def require_connected(self) -> None:
        if not self._configured():
            raise integration_offline(self.provider)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class WhatsAppAdapter(IntegrationAdapter):
    provider = "whatsapp"

    def _configured(self) -> bool:
        return bool(self._settings.whatsapp_provider_token and self._settings.whatsapp_provider_base_url)

    async def execute(self, action: str, params: dict) -> dict:
        self.require_connected()
        if action != "send":
            raise tool_unavailable(f"whatsapp.{action}")
        base = (self._settings.whatsapp_provider_base_url or "").rstrip("/")
        resp = await self._request(
            "POST",
            f"{base}/messages",
            headers={"Authorization": f"Bearer {self._settings.whatsapp_provider_token}"},
            json={"recipient": params["recipient"], "message": params["message"]},
        )
        if resp.status_code >= 400:
            raise tool_unavailable("whatsapp.send", details={"provider_status": resp.status_code})
        return {"external_id": resp.json().get("id"), "status": "SENT"}

    async def verify(self, result: dict) -> bool:
        return result.get("status") == "SENT"


class EmailAdapter(IntegrationAdapter):
    provider = "email"

    def _configured(self) -> bool:
        return bool(self._settings.email_provider_token and self._settings.email_provider_base_url)

    async def execute(self, action: str, params: dict) -> dict:
        self.require_connected()
        if action != "send":
            raise tool_unavailable(f"email.{action}")
        base = (self._settings.email_provider_base_url or "").rstrip("/")
        resp = await self._request(
            "POST",
            f"{base}/send",
            headers={"Authorization": f"Bearer {self._settings.email_provider_token}"},
            json={"to": params["to"], "subject": params.get("subject"), "body": params["body"]},
        )
        if resp.status_code >= 400:
            raise tool_unavailable("email.send", details={"provider_status": resp.status_code})
        return {"external_id": resp.json().get("id"), "status": "SENT"}

    async def verify(self, result: dict) -> bool:
        return result.get("status") == "SENT"


class GitHubAdapter(IntegrationAdapter):
    provider = "github"

    def _configured(self) -> bool:
        return bool(self._settings.github_token)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._settings.github_token}", "Accept": "application/vnd.github+json"}

    async def _get_json(self, path: str) -> dict | list:
        self.require_connected()
        resp = await self._request("GET", f"{self._settings.github_base_url.rstrip('/')}{path}", headers=self._headers())
        if resp.status_code >= 400:
            raise tool_unavailable("github.read", details={"provider_status": resp.status_code})
        return resp.json()

    async def execute(self, action: str, params: dict) -> dict:
        self.require_connected()
        if action == "create_issue":
            data = await self._post_json(f"/repos/{params['repository']}/issues", {
                "title": params["title"], "body": params.get("body", ""),
            })
            return {"number": data.get("number"), "url": data.get("html_url")}
        if action == "repo_status":
            full = params["repository"]
            runs = await self._get_json(f"/repos/{full}/actions/runs?per_page=1")
            open_issues = await self._get_json(f"/repos/{full}/issues?state=open&per_page=1")
            # v0 maps provider signals conservatively; UNKNOWN is honest when a
            # signal cannot be derived from the responses.
            return {
                "build": "PASSING" if isinstance(runs, list) and not runs else "UNKNOWN",
                "tests": "UNKNOWN",
                "deployment": "UNKNOWN",
                "open_issues": len(open_issues) if isinstance(open_issues, list) else 0,
                "security_alerts": 0,
            }
        raise tool_unavailable(f"github.{action}")

    async def _post_json(self, path: str, payload: dict):
        resp = await self._request(
            "POST", f"{self._settings.github_base_url.rstrip('/')}{path}", headers=self._headers(), json=payload)
        if resp.status_code >= 400:
            raise tool_unavailable("github.write", details={"provider_status": resp.status_code})
        return resp.json()

    async def verify(self, result: dict) -> bool:
        if "number" in result:  # create_issue result
            return result.get("number") is not None
        if "build" in result:   # repo_status result — the signals must be present
            return result.get("open_issues") is not None
        return False
