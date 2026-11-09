"""Read-only infrastructure health checks for the always-on runtime.

The health endpoint must be safe to call from an external supervisor.  This module therefore
only reads local state or opens the specifically declared TCP and HTTP connections.  It never
starts processes, runs commands, follows an arbitrary recovery script, or returns paths,
URLs, headers, cookies or vault values.

A recovery is the other half: one owner-declared program that may repair a PID, TCP or HTTP
check.  It runs only through the Sentinel, only while its probe is unhealthy, and only when
the program is still exactly the one that was approved: the fingerprint of a recovery covers
its whole declaration and the content of its program, and it is checked again right before
the program starts.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from nanomuse.config import (
    HealthFileProbeSettings,
    HealthHTTPProbeSettings,
    HealthLoginStateSettings,
    HealthPIDProbeSettings,
    HealthRecoverySettings,
    HealthSettings,
    HealthTCPProbeSettings,
)
from nanomuse.schema import RiskLevel, ToolResult
from nanomuse.stdio import decode_output
from nanomuse.tools.base import BaseTool, CallAssessment, current_call_id
from nanomuse.tools.shell import kill_tree, scrubbed_env
from nanomuse.vault import CredentialVault
from nanomuse.vault.vault import VaultError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Treat a redirect as its own response so configured credentials stay on one host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_remote_opener = urllib.request.build_opener(_NoRedirect())
_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _windows_pid_state(pid: int) -> bool | None:
    """Whether a Windows process is active; None means it exists but cannot be queried."""
    import ctypes
    from ctypes import wintypes

    query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        # 5 is ERROR_ACCESS_DENIED: the process is there, but this account cannot ask.
        last_error = getattr(ctypes, "get_last_error", lambda: 0)()
        return None if last_error == 5 else False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _program_digest(program: str) -> str:
    """The SHA-256 of a program file's content.

    The metadata alone is not enough: a program rewritten with the same length and its
    modification time put back looks untouched.  The content hash is what makes a long-lived
    approval about this exact program, and it is read again right before the program starts.

    The file is read every time rather than cached against its metadata.  A cache would have
    to trust the modification time, and that is exactly the field a rewrite can put back; the
    files involved are small scripts, so reading them again costs less than being wrong.
    """
    digest = hashlib.sha256()
    try:
        with Path(program).open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _recovery_fingerprint(recovery: HealthRecoverySettings) -> str:
    """What a standing approval about a recovery is bound to: the whole declaration and the
    content of the program it would run."""
    payload = {
        "recovery": recovery.model_dump(mode="json"),
        "program": _program_digest(recovery.program),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class HealthMonitor:
    """Collect a redacted snapshot from declarative, side-effect-free probes."""

    #: How long a collected view is reused before the probes run again.  The probes are
    #: read-only, so a few seconds of an older answer is worth much less than a second
    #: of a supervisor's timeout; the Docker health check asks every 30 seconds.
    VIEW_CACHE_S = 5.0
    #: The longest the whole view may take.  One slow endpoint must not hold the
    #: endpoint hostage: a probe that has not answered by then is reported as unknown
    #: rather than waited for.  It is under the three seconds the container's own
    #: health check allows, so a slow probe never makes the container look unhealthy.
    VIEW_DEADLINE_S = 2.0

    def __init__(self, settings: HealthSettings, vault: CredentialVault, data_dir: Path):
        self.settings = settings
        self.vault = vault
        self.data_dir = Path(data_dir)
        self._cached: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def view(self) -> dict[str, Any]:
        """Return the configured checks and their current state.

        A check's detail deliberately contains only a short error class or a configured label.
        The response is public on token-protected deployments so it must not disclose a local
        path, an endpoint, a header or a secret value.

        The probes run together, so one slow endpoint costs the view its own timeout and
        not the sum of them all, and the whole view is bounded: a probe that has not
        answered when the deadline passes is reported as unknown and the answer so far
        is returned.  A supervisor's liveness check must not wait on a remote endpoint.
        """
        if not self.settings.enabled:
            return {"state": "disabled", "checks": []}
        with self._lock:
            cached = self._cached
            if cached and time.monotonic() - cached[0] < self.VIEW_CACHE_S:
                return cached[1]
        view = self._collect()
        with self._lock:
            self._cached = (time.monotonic(), view)
        return view

    def _collect(self) -> dict[str, Any]:
        """Run every probe together, under the view's deadline."""
        probes: list[tuple[str, str, Callable[[], dict[str, Any]]]] = [
            ("disk", "disk", self._disk),
            ("vault", "vault", self._vault),
        ]
        probes.extend(
            (item.name, "backup", partial(self._file, item, "backup"))
            for item in self.settings.backups
        )
        probes.extend(
            (item.name, "login", partial(self._login, item)) for item in self.settings.login_states
        )
        probes.extend(
            (item.name, "pid", partial(self._pid, item)) for item in self.settings.pid_files
        )
        probes.extend((item.name, "tcp", partial(self._tcp, item)) for item in self.settings.tcp)
        probes.extend((item.name, "http", partial(self._http, item)) for item in self.settings.http)

        checks: list[dict[str, Any]] = []
        pool = ThreadPoolExecutor(max_workers=len(probes) or 1)
        try:
            futures = {
                pool.submit(self._probe, name, kind, run): (name, kind)
                for name, kind, run in probes
            }
            done, pending = wait(futures, timeout=self.VIEW_DEADLINE_S)
            for future in done:
                checks.append(future.result())
            for future in pending:
                name, kind = futures[future]
                checks.append(self._result(name, kind, "unknown"))
                # the probe is still running in its thread; it is a read-only check, so
                # letting it finish on its own is harmless, but it must not keep this
                # thread (and the caller waiting for the answer) any longer
                future.cancel()
        finally:
            # not wait=True: a probe that is still waiting on a slow endpoint would
            # otherwise hold the answer back until it times out on its own
            pool.shutdown(wait=False)
        # the order the owner wrote them in, so the view reads the same every time
        order = {name: index for index, (name, _, _) in enumerate(probes)}
        checks.sort(key=lambda check: order.get(str(check.get("name")), 0))
        return {"state": self._overall(checks), "checks": checks}

    def check(self, name: str) -> dict[str, Any]:
        """Run one named check, for a recovery's before and after comparison."""
        if not self.settings.enabled:
            return self._result(name, "health", "disabled")
        wanted = name.casefold()
        if wanted == "disk":
            return self._probe("disk", "disk", self._disk)
        if wanted == "vault":
            return self._probe("vault", "vault", self._vault)
        for backup in self.settings.backups:
            if backup.name.casefold() == wanted:
                return self._probe(backup.name, "backup", partial(self._file, backup, "backup"))
        for login in self.settings.login_states:
            if login.name.casefold() == wanted:
                return self._probe(login.name, "login", partial(self._login, login))
        for pid_file in self.settings.pid_files:
            if pid_file.name.casefold() == wanted:
                return self._probe(pid_file.name, "pid", partial(self._pid, pid_file))
        for tcp in self.settings.tcp:
            if tcp.name.casefold() == wanted:
                return self._probe(tcp.name, "tcp", partial(self._tcp, tcp))
        for http_probe in self.settings.http:
            if http_probe.name.casefold() == wanted:
                return self._probe(http_probe.name, "http", partial(self._http, http_probe))
        raise KeyError(name)

    @staticmethod
    def _overall(checks: list[dict[str, Any]]) -> str:
        states = {str(check["state"]) for check in checks}
        if "error" in states:
            return "error"
        if "warn" in states:
            return "warn"
        # a probe that did not answer in time is not healthy, but it is not broken
        # either: the view says so rather than pretending the check passed
        if "unknown" in states:
            return "unknown"
        return "ok"

    @staticmethod
    def _result(name: str, kind: str, state: str, **detail: Any) -> dict[str, Any]:
        return {"name": name, "kind": kind, "state": state, **detail}

    @staticmethod
    def _exception(exc: Exception) -> str:
        """An error label that cannot leak endpoint, path or secret text."""
        return type(exc).__name__

    def _probe(self, name: str, kind: str, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """One probe, and only that probe, may fail: a broken endpoint never blanks the view."""
        try:
            return run()
        except Exception as exc:  # noqa: BLE001 - the rest of the checks still matter
            return self._result(name, kind, "error", error=self._exception(exc))

    @staticmethod
    def _path(path: Path) -> Path:
        return Path(path).expanduser()

    def _disk(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.data_dir)
            total = usage.total
            free = usage.free
            free_mb = free // (1024 * 1024)
            free_percent = round((free / total * 100) if total else 0.0, 1)
        except OSError as exc:
            return self._result("disk", "disk", "error", error=self._exception(exc))
        below_mb = self.settings.min_free_mb and free_mb < self.settings.min_free_mb
        below_percent = (
            self.settings.min_free_percent and free_percent < self.settings.min_free_percent
        )
        return self._result(
            "disk",
            "disk",
            "warn" if below_mb or below_percent else "ok",
            free_mb=free_mb,
            free_percent=free_percent,
        )

    def _vault(self) -> dict[str, Any]:
        if not self.vault.path.is_file():
            return self._result("vault", "vault", "ok", configured=False, entries=0)
        # CredentialVault lazily creates a key file.  A health read must never have that
        # side effect, especially when a copied vault is missing its original key.
        if not os.environ.get("NANOMUSE_VAULT_KEY") and not self.vault.key_path.is_file():
            return self._result("vault", "vault", "error", error="key_unavailable")
        try:
            entries = len(self.vault.names())
        except (OSError, ValueError, VaultError) as exc:
            return self._result("vault", "vault", "error", error=self._exception(exc))
        return self._result("vault", "vault", "ok", configured=True, entries=entries)

    def _file(self, probe: HealthFileProbeSettings, kind: str) -> dict[str, Any]:
        try:
            modified = self._path(probe.path).stat().st_mtime
        except OSError as exc:
            return self._result(probe.name, kind, "error", error=self._exception(exc))
        age_seconds = max(0, round(time.time() - modified))
        max_age_seconds = round(probe.max_age_hours * 3600)
        return self._result(
            probe.name,
            kind,
            "ok" if age_seconds <= max_age_seconds else "error",
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
        )

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _login(self, probe: HealthLoginStateSettings) -> dict[str, Any]:
        path = self._path(probe.path)
        try:
            modified = path.stat().st_mtime
            with path.open("rb") as source:
                raw = source.read(65_537)
            if len(raw) > 65_536:
                raise ValueError("state file too large")
            state = json.loads(raw.decode("utf-8"))
            if not isinstance(state, dict):
                raise ValueError("state is not an object")
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            return self._result(probe.name, "login", "error", error=self._exception(exc))

        checked_at = self._parse_time(state.get("checked_at"))
        age_seconds = max(
            0,
            round(time.time() - (checked_at.timestamp() if checked_at else modified)),
        )
        max_age_seconds = round(probe.max_age_hours * 3600)
        if state.get("ok") is False or state.get("logged_in") is False:
            return self._result(
                probe.name,
                "login",
                "error",
                age_seconds=age_seconds,
                max_age_seconds=max_age_seconds,
                error="not_logged_in",
            )
        if age_seconds > max_age_seconds:
            return self._result(
                probe.name,
                "login",
                "error",
                age_seconds=age_seconds,
                max_age_seconds=max_age_seconds,
                error="stale",
            )

        expires_at = self._parse_time(state.get("expires_at"))
        if expires_at is None:
            return self._result(
                probe.name,
                "login",
                "ok",
                age_seconds=age_seconds,
                max_age_seconds=max_age_seconds,
            )
        expires_in_seconds = round(expires_at.timestamp() - time.time())
        warn_before_seconds = round(probe.warn_before_expiry_hours * 3600)
        return self._result(
            probe.name,
            "login",
            "error"
            if expires_in_seconds <= 0
            else "warn"
            if expires_in_seconds <= warn_before_seconds
            else "ok",
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            expires_in_seconds=expires_in_seconds,
        )

    def _pid(self, probe: HealthPIDProbeSettings) -> dict[str, Any]:
        try:
            with self._path(probe.path).open("rt", encoding="utf-8") as source:
                raw = source.read(32).strip()
            pid = int(raw)
            if pid <= 0:
                raise ValueError("PID must be positive")
        except (OSError, ValueError) as exc:
            return self._result(probe.name, "pid", "error", error=self._exception(exc))
        if os.name == "nt":
            state = _windows_pid_state(pid)
            if state is None:
                return self._result(probe.name, "pid", "ok", access="limited")
            return self._result(
                probe.name,
                "pid",
                "ok" if state else "error",
                **({} if state else {"error": "not_running"}),
            )
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return self._result(probe.name, "pid", "error", error="not_running")
        except PermissionError:
            # The process exists but belongs to another account.  That is healthy enough for
            # a service supervisor and does not need a privilege escalation to inspect it.
            return self._result(probe.name, "pid", "ok", access="limited")
        except OSError as exc:
            return self._result(probe.name, "pid", "error", error=self._exception(exc))
        return self._result(probe.name, "pid", "ok")

    def _tcp(self, probe: HealthTCPProbeSettings) -> dict[str, Any]:
        started = time.monotonic()
        try:
            with socket.create_connection((probe.host, probe.port), timeout=probe.timeout_s):
                pass
        except OSError as exc:
            return self._result(
                probe.name,
                "tcp",
                "error",
                latency_ms=round((time.monotonic() - started) * 1000),
                error=self._exception(exc),
            )
        return self._result(
            probe.name,
            "tcp",
            "ok",
            latency_ms=round((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _loopback(url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host in {"localhost", "::1"} or host.startswith("127.")

    def _http(self, probe: HealthHTTPProbeSettings) -> dict[str, Any]:
        started = time.monotonic()
        try:
            url = self.vault.resolve(probe.url, strict=True)
            headers = self.vault.resolve(probe.headers, strict=True)
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("invalid URL")
            request = urllib.request.Request(url, headers=headers, method=probe.method)
            opener = _local_opener if self._loopback(url) else _remote_opener
            with opener.open(request, timeout=probe.timeout_s) as response:
                status = int(response.status)
                response.read(1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
        except (OSError, ValueError, VaultError, http.client.HTTPException) as exc:
            # A broken answer (a half-written response, a dropped connection) is this
            # probe's problem, not the end of the health view.
            return self._result(
                probe.name,
                "http",
                "error",
                latency_ms=round((time.monotonic() - started) * 1000),
                error=self._exception(exc),
            )
        return self._result(
            probe.name,
            "http",
            "ok" if status in probe.expected_status else "error",
            latency_ms=round((time.monotonic() - started) * 1000),
            status=status,
        )


_RECOVERY_OUTPUT_BYTES = 16_384

# The one-time permit a recovery needs: minted by the Sentinel after it allowed the call,
# consumed by the recovery itself, and gone afterwards.  A call that was never allowed has
# no permit, so a denied call cannot be executed directly afterwards.  The call id is part
# of it, so a permit cannot be carried over to another call.
_recovery_permit: ContextVar[tuple[str, str, str, str, str] | None] = ContextVar(
    "health_recovery_permit", default=None
)


async def _read_capped(
    stream: asyncio.StreamReader, cap: int = _RECOVERY_OUTPUT_BYTES
) -> tuple[bytes, int]:
    chunks: list[bytes] = []
    kept = total = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        if kept < cap:
            piece = chunk[: cap - kept]
            chunks.append(piece)
            kept += len(piece)
    return b"".join(chunks), total


class InfrastructureHealth(BaseTool):
    """The agent's read-only health view and its predeclared, Sentinel-gated repairs."""

    name: str = "health"
    description: str = (
        "Inspect runtime infrastructure or run one owner-configured recovery. Actions: `status` "
        "returns the redacted health checks and the names of available recoveries; optional "
        "`name` limits it to one check. `recover` runs a named recovery only while its bound "
        "probe is unhealthy, then checks that probe again. A recovery changes process state and "
        "goes through Sentinel approval. Login, backup, disk and vault checks cannot have a "
        "recovery."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "recover"]},
            "name": {
                "type": "string",
                "description": "A check name for status, or a configured recovery name.",
            },
        },
        "required": ["action"],
    }
    risk: RiskLevel = RiskLevel.SAFE
    monitor: HealthMonitor
    vault: CredentialVault
    data_dir: Path
    locks: dict[str, asyncio.Lock] = Field(default_factory=dict, exclude=True)
    #: when each recovery last started, so a probe that stays unhealthy cannot turn the
    #: recovery into a loop of processes
    last_run: dict[str, float] = Field(default_factory=dict, exclude=True)

    @property
    def recoveries(self) -> list[HealthRecoverySettings]:
        """The declared recoveries, read from the monitor's own settings.

        The tool and the monitor must agree on what a recovery is: the monitor checks the
        probe it repairs, the tool runs the program. Two copies of the list could drift, and
        a recovery the tool offers but the monitor does not check would run against nothing.
        """
        return list(self.monitor.settings.recoveries)

    def _recovery(self, name: str) -> HealthRecoverySettings | None:
        wanted = name.casefold()
        return next((item for item in self.recoveries if item.name.casefold() == wanted), None)

    def _cooldown_left(self, recovery: HealthRecoverySettings) -> float:
        """How many seconds are left of the recovery's cooldown; 0 when it may run."""
        window = max(recovery.cooldown_s, 0.0)
        if window <= 0:
            return 0.0
        last = self.last_run.get(recovery.name.casefold())
        if last is None:
            return 0.0
        return max(0.0, window - (time.monotonic() - last))

    @staticmethod
    def _grant_target(recovery: HealthRecoverySettings) -> str:
        """Bind a standing grant to the declaration and the content of the program."""
        digest = _recovery_fingerprint(recovery)
        return f"{recovery.name}@{digest[:16]}"

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        action = str(args.get("action") or "status").strip().lower()
        name = str(args.get("name") or "").strip()
        if action != "recover":
            return CallAssessment(
                risk=RiskLevel.SAFE, summary=f"health status{': ' + name if name else ''}"
            )
        recovery = self._recovery(name)
        if recovery is None:
            return CallAssessment(risk=RiskLevel.SAFE, summary="unknown health recovery")
        return CallAssessment(
            risk=RiskLevel.SENSITIVE,
            egress=recovery.egress,
            egress_target=recovery.egress_target,
            egress_configured=recovery.egress,
            target=self._grant_target(recovery),
            # A recovery is one exact program, not a tool: a "for this conversation"
            # permission stays bound to it, and no permission for the whole tool covers it.
            keep_target_in_conversation=True,
            exact_target_only=True,
            summary=f"recover infrastructure: {recovery.name}",
            detail=self._recovery_detail(recovery),
        )

    @staticmethod
    def _recovery_detail(recovery: HealthRecoverySettings) -> str:
        """What the program will actually run, for the approval card.

        A card that only says "recover infrastructure: restart relay" asks the person to
        approve a program they cannot see.  The program is the owner's own configuration and
        the arguments are the owner's own words, so showing them is showing the owner their
        own setup, not a secret.  The environment is not shown: it carries the secrets.
        """
        parts = [recovery.program, *recovery.args]
        return " ".join(parts)[:500]

    def authorize_execution(self, assessment: CallAssessment, args: dict[str, Any]) -> None:
        """Mint the one-time permit a recovery needs, once the Sentinel has allowed the call.

        The permit carries the fingerprint of the recovery as it is right now.  If the
        declaration or the program changed between the assessment and the approval, the
        approval was given for something else and the call is refused.
        """
        action = str(args.get("action") or "status").strip().lower()
        name = str(args.get("name") or "").strip()
        if action != "recover":
            _recovery_permit.set(None)
            return
        recovery = self._recovery(name)
        if recovery is None:
            _recovery_permit.set(None)
            raise PermissionError("there is no recovery with that name")
        fingerprint = _recovery_fingerprint(recovery)
        if assessment.target != self._grant_target(recovery):
            _recovery_permit.set(None)
            raise PermissionError("the recovery changed while it was being approved")
        _recovery_permit.set(
            (
                current_call_id(),
                uuid.uuid4().hex,
                recovery.name.casefold(),
                fingerprint,
                _program_digest(recovery.program),
            )
        )

    def _recoveries_view(self) -> list[dict[str, Any]]:
        return [
            {"name": recovery.name, "probe": recovery.probe, "network": recovery.egress}
            for recovery in self.recoveries
        ]

    async def execute(self, action: str = "status", name: str = "", **_: Any) -> ToolResult:
        action = action.strip().lower()
        name = name.strip()
        if action == "status":
            try:
                health = (
                    await asyncio.to_thread(self.monitor.check, name)
                    if name
                    else await asyncio.to_thread(self.monitor.view)
                )
            except KeyError:
                return ToolResult.fail("unknown health check")
            return ToolResult(
                output=json.dumps(
                    {"health": health, "recoveries": self._recoveries_view()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        if action != "recover":
            return ToolResult.fail("action must be status or recover")
        recovery = self._recovery(name)
        if recovery is None:
            return ToolResult.fail("unknown health recovery")
        permit = _recovery_permit.get()
        _recovery_permit.set(None)  # one permit, one call
        if permit is None:
            return ToolResult.fail(
                "this recovery was not approved by the Sentinel; ask for it again"
            )
        call_id, _nonce, approved_name, approved_fingerprint, approved_digest = permit
        if call_id != current_call_id():
            return ToolResult.fail("this recovery was not approved by the Sentinel")
        if approved_name != recovery.name.casefold():
            return ToolResult.fail("the approval was given for another recovery")
        if _recovery_fingerprint(recovery) != approved_fingerprint:
            return ToolResult.fail(
                "the recovery declaration changed after it was approved; ask for it again"
            )
        lock = self.locks.setdefault(recovery.name.casefold(), asyncio.Lock())
        async with lock:
            before = await asyncio.to_thread(self.monitor.check, recovery.probe)
            if before["state"] == "disabled":
                return ToolResult.fail("health checks are disabled")
            if before["state"] == "ok":
                return ToolResult.fail("recovery not run because its probe is healthy")
            # A probe that stays unhealthy must not turn into a loop: the model may ask for
            # the same recovery again and again, and each run starts a process that lives
            # for up to timeout_s.  The wait gives the service time to come back and gives
            # the person a chance to look at it.
            wait = self._cooldown_left(recovery)
            if wait > 0:
                return ToolResult.fail(
                    f"the {recovery.name} recovery ran {int(wait)}s ago; "
                    f"wait {int(wait)}s before running it again"
                )
            self.last_run[recovery.name.casefold()] = time.monotonic()
            process = await self._run_recovery(recovery, approved_digest)
            if not process.ok:
                return process
            if recovery.verify_after_s:
                await asyncio.sleep(recovery.verify_after_s)
            after = await asyncio.to_thread(self.monitor.check, recovery.probe)
            result = {
                "recovery": recovery.name,
                "before": before,
                "after": after,
            }
            if process.output:
                result["process_output"] = process.output
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if after["state"] != "ok":
                return ToolResult(
                    output=text, error="recovery finished but the probe is still unhealthy"
                )
            return ToolResult(output=text)

    async def _run_recovery(self, recovery: HealthRecoverySettings, approved: str) -> ToolResult:
        try:
            extra_env = self.vault.resolve(recovery.env, strict=True)
        except (OSError, ValueError, VaultError) as exc:
            return ToolResult.fail(f"recovery environment is unavailable: {type(exc).__name__}")
        env = scrubbed_env()
        env.update({str(key): str(value) for key, value in extra_env.items()})
        cwd = (recovery.cwd or self.data_dir).expanduser()
        if not cwd.is_dir():
            return ToolResult.fail("configured recovery working directory is unavailable")
        own_session = os.name != "nt"
        # The probe above may take a minute; the program is read again here, right before it
        # starts, so a file swapped in the meantime is not the file that was approved.
        if _program_digest(recovery.program) != approved:
            return ToolResult.fail("the recovery program changed before it could start")
        try:
            proc = await asyncio.create_subprocess_exec(
                recovery.program,
                *recovery.args,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=own_session,
            )
            try:
                assert proc.stdout is not None and proc.stderr is not None
                (stdout, out_total), (stderr, err_total), _ = await asyncio.wait_for(
                    asyncio.gather(
                        _read_capped(proc.stdout),
                        _read_capped(proc.stderr),
                        proc.wait(),
                    ),
                    timeout=recovery.timeout_s,
                )
            except TimeoutError:
                kill_tree(proc)
                await proc.wait()
                return ToolResult.fail(f"recovery timed out after {recovery.timeout_s:.0f}s")
            except asyncio.CancelledError:
                kill_tree(proc)
                raise
        except FileNotFoundError:
            return ToolResult.fail("configured recovery program was not found")
        except (OSError, ValueError) as exc:
            return ToolResult.fail(f"configured recovery could not start: {type(exc).__name__}")

        parts = []
        if stdout.strip():
            parts.append(decode_output(stdout).strip())
        if stderr.strip():
            parts.append("[stderr]\n" + decode_output(stderr).strip())
        if out_total > _RECOVERY_OUTPUT_BYTES or err_total > _RECOVERY_OUTPUT_BYTES:
            parts.append("[output truncated]")
        output = self.vault.redact("\n".join(parts))
        if proc.returncode:
            return ToolResult(output=output, error=f"recovery exited with code {proc.returncode}")
        return ToolResult(output=output)


__all__ = ["HealthMonitor", "InfrastructureHealth"]
