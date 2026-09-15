"""LLM integration — OpenAI-compatible chat-completions client (rule #30 boundary).

This is the ONLY component that talks to a model provider. The agent brain
(`app/agent/brain.py`) consumes it; the LLM never touches services, the DB,
or any integration adapter directly.

Every failure is mapped onto the contract's FIXED error-code set:
  401/403  -> DEPENDENCY_FAILURE  (bad credentials — a configuration problem)
  429/5xx  -> INTEGRATION_OFFLINE (provider down / rate-limited; retried once)
  timeout  -> TIMEOUT
  no key   -> INTEGRATION_OFFLINE (honest: the capability is not configured)
  malformed-> EXECUTION_FAILED

Secrets (the API key) are never logged. Only purpose/model/latency/token counts.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.core import context
from app.core.errors import AppError, execution_failed
from app.generated import models


def _timeout_error(message: str, details: dict) -> AppError:
    return AppError(models.ErrorCode.TIMEOUT, message, details)


def _dependency_error(message: str, details: dict) -> AppError:
    return AppError(models.ErrorCode.DEPENDENCY_FAILURE, message, details)


def _offline_error(message: str, details: dict) -> AppError:
    return AppError(models.ErrorCode.INTEGRATION_OFFLINE, message, details)


class LLMResult:
    """One parsed completion. `content` is the assistant text (JSON when json_mode)."""

    __slots__ = ("content", "tool_calls", "usage", "model", "raw")

    def __init__(self, content: str | None, tool_calls: list[dict], usage: dict, model: str, raw: dict) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage
        self.model = model
        self.raw = raw


class OpenAICompatibleLLM:
    """A real client for any OpenAI-compatible `/chat/completions` endpoint
    (OpenAI, OpenRouter, Groq, Together, vLLM, Ollama, LiteLLM, ...).

    `client` exists for the test boundary: production passes nothing (a real
    httpx client), tests inject a MockTransport-backed client.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str = "gpt-4o-mini", timeout_seconds: float = 60.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._api_key = api_key or None
        self._model = model or "gpt-4o-mini"
        self._timeout = timeout_seconds
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        """No credentials configured -> the capability is honestly unavailable."""
        return bool(self._api_key)

    @property
    def model_name(self) -> str:
        return self._model

    async def chat(self, messages: list[dict], *, temperature: float = 0.2,
                   max_tokens: int = 2048, json_mode: bool = True,
                   purpose: str = "chat") -> LLMResult:
        if not self.configured:
            raise _offline_error(
                "LLM provider is not configured", {"reason": "GHAYATH_LLM_API_KEY is not set"})

        payload: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        last: AppError | None = None
        for attempt in (1, 2):
            start = time.monotonic()
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as e:
                self._log(purpose, ok=False, latency_ms=self._ms(start), error="timeout")
                raise _timeout_error("LLM request timed out", {"model": self._model}) from e
            except httpx.HTTPError as e:
                self._log(purpose, ok=False, latency_ms=self._ms(start), error=type(e).__name__)
                raise _offline_error("LLM provider unreachable", {"reason": type(e).__name__}) from e

            latency = self._ms(start)
            if resp.status_code in (401, 403):
                self._log(purpose, ok=False, latency_ms=latency, error="credentials_rejected")
                raise _dependency_error("LLM credentials were rejected by the provider",
                                        {"status": resp.status_code})
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last = _offline_error("LLM provider error", {"status": resp.status_code})
                if attempt == 1:
                    await asyncio.sleep(0.1)  # one short retry before declaring the provider offline
                    continue
                self._log(purpose, ok=False, latency_ms=latency, error=f"http_{resp.status_code}")
                raise last
            if resp.status_code >= 400:
                self._log(purpose, ok=False, latency_ms=latency, error=f"http_{resp.status_code}")
                raise _offline_error("LLM provider error", {"status": resp.status_code})

            try:
                body = resp.json()
                choice = body["choices"][0]
                message = choice.get("message") or {}
                usage = body.get("usage") or {}
            except (ValueError, KeyError, IndexError, TypeError) as e:
                self._log(purpose, ok=False, latency_ms=latency, error="malformed_response")
                raise execution_failed("Malformed LLM response") from e

            self._log(purpose, ok=True, latency_ms=latency,
                      prompt_tokens=usage.get("prompt_tokens"),
                      completion_tokens=usage.get("completion_tokens"),
                      messages=len(messages))
            return LLMResult(message.get("content"), message.get("tool_calls") or [],
                             usage, body.get("model", self._model), body)
        raise last  # unreachable: loop always returns or raises

    @staticmethod
    def _ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)

    def _log(self, purpose: str, ok: bool, latency_ms: int, **extra) -> None:
        # Never logs content or credentials — only shape/usage metadata.
        context.log_event("llm.call", {"purpose": purpose, "model": self._model,
                                       "ok": ok, "latency_ms": latency_ms, **extra})

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
