"""Classify model failures for failover and safe scheduled-work retries.

A provider may retry an individual HTTP request itself. This module decides whether a
*second configured model* is a sensible next route, and whether a background delivery
that did not reach any tool may be tried later. It deliberately keeps authentication,
invalid requests and content refusals terminal: retrying them on another provider is
not a recovery strategy.
"""

from __future__ import annotations

import asyncio

import httpx
import openai

from nanomuse.cloud import CloudError
from nanomuse.llm.chatgpt import ChatGPTError

# A backup model may be useful when the first route cannot serve the work at all,
# including a depleted subscription or temporary provider outage.
_FAILOVER_CODES = {
    "allowance_exhausted",
    "daily_cap",
    "out_of_tokens",
    "provider_busy",
    "rate_limited",
    "upstream",
    "upstream_busy",
    "upstream_unconfigured",
    "offline",
    "timeout",
    "quota",
    "unreachable",
}

# Requeue only temporary transport or provider conditions. Plan quotas are left for a
# configured fallback route, not retried against the exhausted account every minute.
_RETRY_CODES = {
    "provider_busy",
    "rate_limited",
    "upstream",
    "upstream_busy",
    "offline",
    "timeout",
    "unreachable",
}


def failure_code(exc: BaseException) -> str:
    """A stable, deliberately small classification for retry policy and audit records."""
    if isinstance(exc, CloudError):
        return exc.code or "cloud"
    if isinstance(exc, ChatGPTError):
        return exc.code or "chatgpt"
    if isinstance(exc, openai.RateLimitError):
        return "rate_limited"
    if isinstance(exc, openai.APITimeoutError | asyncio.TimeoutError | TimeoutError):
        return "timeout"
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError | ConnectionError):
        return "offline"
    if isinstance(exc, openai.InternalServerError):
        return "upstream"
    if isinstance(exc, openai.APIError):
        return "provider"
    return "unknown"


def can_failover(exc: BaseException) -> bool:
    """Whether another independently configured model may reasonably serve this call."""
    code = failure_code(exc)
    if code in _FAILOVER_CODES:
        return True
    # APIError is broad: credentials, permissions and malformed requests inherit from it.
    # Those are route configuration problems, not availability failures, and trying a second
    # model would only hide the real problem or spend a different provider's allowance.
    terminal = (
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.BadRequestError,
        openai.UnprocessableEntityError,
    )
    return isinstance(exc, openai.APIError) and not isinstance(exc, terminal)


def can_retry_delivery(exc: BaseException) -> bool:
    """Whether a scheduled delivery may safely wait and try the same route later."""
    return failure_code(exc) in _RETRY_CODES


__all__ = ["can_failover", "can_retry_delivery", "failure_code"]
