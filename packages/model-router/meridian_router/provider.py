"""Model providers.

Every method returns a result rather than raising. A model being slow, absent,
overloaded or wrong is a **normal operating condition**, not an exception — the
deterministic pipeline must continue regardless, and a provider that throws would
make that the caller's problem on every call site.

Redaction runs immediately before the request leaves the process, and a
post-redaction assertion fails the call if a secret pattern survives. That check
should be unreachable; if it ever fires, not sending is the correct outcome.
"""

from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from meridian_config.redaction import contains_secret, redact_text

from meridian_router.registry import ModelSpec, Provider


@dataclass(frozen=True, slots=True)
class Invocation:
    """Audit record for one model call.

    Stores the prompt **hash**, never the prompt body (security.md §5). The
    prompt may contain redacted trade context, and retaining it would recreate
    the exposure redaction exists to prevent.
    """

    model_key: str
    model_id: str
    provider: Provider
    prompt_hash: str
    prompt_chars: int
    latency_ms: int
    ok: bool
    error: str = ""
    response_chars: int = 0
    estimated_cost_usd: Decimal = Decimal(0)
    redaction_applied: bool = True
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Outcome of a model call. Never an exception."""

    ok: bool
    text: str = ""
    error: str = ""
    invocation: Invocation | None = None
    embedding: tuple[float, ...] | None = None

    @property
    def degraded(self) -> bool:
        return not self.ok


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    reachable: bool
    detail: str = ""
    available_models: tuple[str, ...] = field(default_factory=tuple)


def prompt_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()[:32]}"


class ModelProvider(ABC):
    """Interface every provider satisfies."""

    provider: Provider

    @abstractmethod
    async def generate(
        self, spec: ModelSpec, prompt: str, *, json_mode: bool = True, seed: int | None = None
    ) -> ProviderResult: ...

    @abstractmethod
    async def embed(self, spec: ModelSpec, text: str) -> ProviderResult: ...

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    @staticmethod
    def _prepare(prompt: str) -> tuple[str, str]:
        """Redact, then verify. Returns (safe_prompt, hash).

        Raises only if a secret survives redaction — the one case where refusing
        to proceed is safer than degrading, because degrading would mean sending.
        """
        safe = redact_text(prompt)
        if contains_secret(safe):
            raise RuntimeError(
                "A secret pattern survived redaction. Refusing to send. This is a "
                "defect in meridian_config.redaction, not a transient failure."
            )
        return safe, prompt_hash(safe)


class OllamaProvider(ModelProvider):
    """Local models over Ollama's HTTP API.

    Local, so ``privacy: unrestricted`` — nothing leaves the machine. Redaction
    still runs: it is cheap, it keeps one code path, and it means a future
    misconfiguration that points this at a remote host is not a data leak.
    """

    provider = Provider.OLLAMA

    def __init__(
        self, base_url: str = "http://localhost:11434", *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def _post(self, path: str, payload: dict[str, Any], timeout: float) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(f"{self.base_url}{path}", json=payload, timeout=timeout)
        async with httpx.AsyncClient() as client:
            return await client.post(f"{self.base_url}{path}", json=payload, timeout=timeout)

    async def generate(
        self, spec: ModelSpec, prompt: str, *, json_mode: bool = True, seed: int | None = None
    ) -> ProviderResult:
        """Call the model, retrying on transport failure.

        Retries cover connection and timeout errors only. A 4xx means the request
        itself is wrong, and repeating it would waste the timeout budget without
        changing the outcome.
        """
        try:
            safe_prompt, digest = self._prepare(prompt)
        except RuntimeError as exc:
            return ProviderResult(ok=False, error=str(exc))

        payload: dict[str, Any] = {
            "model": spec.model_id,
            "prompt": safe_prompt,
            "stream": False,
            # Deterministic by default: temperature 0 plus a fixed seed, so a
            # replayed run produces the same critique.
            "options": {"temperature": 0, "seed": seed if seed is not None else 0},
        }
        if json_mode:
            payload["format"] = "json"

        started = time.perf_counter()
        last_error = ""

        for attempt in range(1, spec.retries + 2):
            try:
                response = await self._post("/api/generate", payload, float(spec.timeout_s))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            elapsed = int((time.perf_counter() - started) * 1000)

            if response.status_code >= 400:
                # Not retried: the request is wrong, not the connection.
                return ProviderResult(
                    ok=False,
                    error=f"HTTP {response.status_code}: {response.text[:200]}",
                    invocation=Invocation(
                        model_key=spec.key,
                        model_id=spec.model_id,
                        provider=self.provider,
                        prompt_hash=digest,
                        prompt_chars=len(safe_prompt),
                        latency_ms=elapsed,
                        ok=False,
                        error=f"HTTP {response.status_code}",
                        attempts=attempt,
                    ),
                )

            try:
                body = response.json()
            except ValueError as exc:
                return ProviderResult(ok=False, error=f"Non-JSON envelope: {exc}")

            text = str(body.get("response", ""))
            return ProviderResult(
                ok=True,
                text=text,
                invocation=Invocation(
                    model_key=spec.key,
                    model_id=spec.model_id,
                    provider=self.provider,
                    prompt_hash=digest,
                    prompt_chars=len(safe_prompt),
                    latency_ms=elapsed,
                    ok=True,
                    response_chars=len(text),
                    attempts=attempt,
                ),
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        return ProviderResult(
            ok=False,
            error=f"Unreachable after {spec.retries + 1} attempts: {last_error}",
            invocation=Invocation(
                model_key=spec.key,
                model_id=spec.model_id,
                provider=self.provider,
                prompt_hash=digest,
                prompt_chars=len(safe_prompt),
                latency_ms=elapsed,
                ok=False,
                error=last_error,
                attempts=spec.retries + 1,
            ),
        )

    async def embed(self, spec: ModelSpec, text: str) -> ProviderResult:
        try:
            safe_text, digest = self._prepare(text)
        except RuntimeError as exc:
            return ProviderResult(ok=False, error=str(exc))

        started = time.perf_counter()
        try:
            response = await self._post(
                "/api/embeddings",
                {"model": spec.model_id, "prompt": safe_text},
                float(spec.timeout_s),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return ProviderResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return ProviderResult(ok=False, error=f"HTTP {response.status_code}")

        try:
            vector = response.json().get("embedding") or []
        except ValueError as exc:
            return ProviderResult(ok=False, error=f"Non-JSON envelope: {exc}")

        if not vector:
            return ProviderResult(ok=False, error="Empty embedding returned")

        return ProviderResult(
            ok=True,
            embedding=tuple(float(v) for v in vector),
            invocation=Invocation(
                model_key=spec.key,
                model_id=spec.model_id,
                provider=self.provider,
                prompt_hash=digest,
                prompt_chars=len(safe_text),
                latency_ms=elapsed,
                ok=True,
            ),
        )

    async def health(self) -> ProviderHealth:
        try:
            if self._client is not None:
                response = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"{self.base_url}/api/tags", timeout=5.0)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return ProviderHealth(reachable=False, detail=f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            return ProviderHealth(reachable=False, detail=f"HTTP {response.status_code}")

        try:
            models = response.json().get("models", [])
        except ValueError:
            return ProviderHealth(reachable=False, detail="Non-JSON response")

        return ProviderHealth(
            reachable=True,
            detail=f"{len(models)} models available",
            available_models=tuple(str(m.get("name", "")) for m in models),
        )


class NullProvider(ModelProvider):
    """Always degrades. The no-LLM configuration made explicit.

    Not a stub for tests — this is what runs when Ollama is disabled and no cloud
    key is present, and the suite exercises it as a supported configuration.
    """

    provider = Provider.OLLAMA

    async def generate(
        self, spec: ModelSpec, prompt: str, *, json_mode: bool = True, seed: int | None = None
    ) -> ProviderResult:
        return ProviderResult(ok=False, error="No model provider is configured")

    async def embed(self, spec: ModelSpec, text: str) -> ProviderResult:
        return ProviderResult(ok=False, error="No embedding provider is configured")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(reachable=False, detail="no-LLM mode")
