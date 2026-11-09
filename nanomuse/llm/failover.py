"""A bounded, auditable route over independently configured chat models."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from nanomuse.llm.base import BaseLLM, DeltaCallback
from nanomuse.llm.resilience import can_failover, failure_code
from nanomuse.logger import logger
from nanomuse.schema import LLMResponse, Message


@dataclass(frozen=True)
class LLMRoute:
    """One configured model route, identified without exposing a credential."""

    name: str
    provider: str
    model: str
    llm: BaseLLM

    def view(self) -> dict[str, str]:
        return {"name": self.name, "provider": self.provider, "model": self.model}


FailoverCallback = Callable[[dict[str, str]], None]


class FailoverLLM(BaseLLM):
    """Try an ordered set of independent model routes for availability failures only.

    Each provider client keeps its own retry policy. This wrapper does not replay a
    request after authentication, validation or safety failures. A successful route
    becomes the active one for status reporting only; future calls still begin at the
    primary route, so a temporary outage heals without an explicit switch back.
    """

    name = "failover"

    def __init__(
        self,
        routes: Sequence[LLMRoute],
        *,
        on_failover: FailoverCallback | None = None,
    ) -> None:
        if not routes:
            raise ValueError("FailoverLLM needs at least one route")
        self.routes = tuple(routes)
        self.settings = getattr(self.routes[0].llm, "settings", None)
        self.supports_native_tools = all(
            bool(getattr(route.llm, "supports_native_tools", False)) for route in self.routes
        )
        self.vision_available: bool | None = getattr(self.routes[0].llm, "vision_available", None)
        self.active_route = self.routes[0].name
        self.failovers = 0
        self.last_failure = ""
        self._on_failover = on_failover

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active_route,
            "routes": [route.view() for route in self.routes],
            "failovers": self.failovers,
            "last_failure": self.last_failure,
        }

    async def ask(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        on_delta: DeltaCallback | None = None,
        on_reasoning_delta: DeltaCallback | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        first = self.routes[0]
        for index, route in enumerate(self.routes):
            try:
                response = await route.llm.ask(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    on_delta=on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    max_tokens=max_tokens,
                )
                self.active_route = route.name
                self.vision_available = getattr(route.llm, "vision_available", None)
                return response
            except Exception as exc:
                code = failure_code(exc)
                self.last_failure = code
                if index == len(self.routes) - 1 or not can_failover(exc):
                    raise
                next_route = self.routes[index + 1]
                self.failovers += 1
                event = {
                    "from": route.name,
                    "to": next_route.name,
                    "code": code,
                    "provider": route.provider,
                    "model": route.model,
                }
                logger.warning(
                    "model route {} ({}/{}) failed with {}; trying {} ({}/{})",
                    route.name,
                    route.provider,
                    route.model,
                    code,
                    next_route.name,
                    next_route.provider,
                    next_route.model,
                )
                if self._on_failover is not None:
                    self._on_failover(event)
        raise RuntimeError(f"all model routes failed after {first.name}")  # pragma: no cover

    def roomier_max_tokens(self) -> int:
        return max(route.llm.roomier_max_tokens() for route in self.routes)

    async def close(self) -> None:
        closed: set[int] = set()
        for route in self.routes:
            identity = id(route.llm)
            if identity in closed:
                continue
            closed.add(identity)
            await route.llm.close()


__all__ = ["FailoverLLM", "LLMRoute"]
