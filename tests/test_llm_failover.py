"""Bounded model failover is available-only and never hides terminal errors."""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest

from nanomuse.llm.failover import FailoverLLM, LLMRoute
from nanomuse.llm.mock import MockLLM
from nanomuse.schema import LLMResponse


def _status_error(cls, status: int, code: str):
    request = httpx.Request("POST", "https://model.example/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"code": code}})
    return cls(code, response=response, body={"code": code})


@pytest.mark.asyncio
async def test_availability_failure_uses_the_next_configured_route():
    def unavailable(_: object) -> LLMResponse:
        raise _status_error(openai.RateLimitError, 429, "rate_limited")

    primary = MockLLM([unavailable])
    backup = MockLLM([LLMResponse(content="served by backup")])
    events: list[dict[str, str]] = []
    llm = FailoverLLM(
        [
            LLMRoute("primary", "openai", "first", primary),
            LLMRoute("backup", "openai", "second", backup),
        ],
        on_failover=events.append,
    )

    response = await llm.ask([])

    assert response.content == "served by backup"
    assert len(primary.calls) == len(backup.calls) == 1
    assert llm.status()["active"] == "backup"
    assert events == [
        {
            "from": "primary",
            "to": "backup",
            "code": "rate_limited",
            "provider": "openai",
            "model": "first",
        }
    ]


@pytest.mark.asyncio
async def test_authentication_failure_does_not_use_a_fallback():
    def refused(_: object) -> LLMResponse:
        raise _status_error(openai.AuthenticationError, 401, "bad_key")

    primary = MockLLM([refused])
    backup = MockLLM([LLMResponse(content="must not run")])
    llm = FailoverLLM(
        [
            LLMRoute("primary", "openai", "first", primary),
            LLMRoute("backup", "openai", "second", backup),
        ]
    )

    with pytest.raises(openai.AuthenticationError):
        await llm.ask([])

    assert len(primary.calls) == 1
    assert backup.calls == []


@pytest.mark.asyncio
async def test_cancellation_propagates_without_using_a_fallback():
    def cancelled(_: object) -> LLMResponse:
        raise asyncio.CancelledError()

    primary = MockLLM([cancelled])
    backup = MockLLM([LLMResponse(content="must not run")])
    llm = FailoverLLM(
        [
            LLMRoute("primary", "openai", "first", primary),
            LLMRoute("backup", "openai", "second", backup),
        ]
    )

    with pytest.raises(asyncio.CancelledError):
        await llm.ask([])

    assert len(primary.calls) == 1
    assert backup.calls == []
