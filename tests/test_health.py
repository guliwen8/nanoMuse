"""The health centre only observes declared state and never exposes its targets.

A recovery is the other half and it is a privilege: it runs a program the owner declared,
it changes process state, and it may only run on a permit the Sentinel minted for that
one call.  These tests hold that line: a denied call runs nothing, a call the model
reaches without the Sentinel runs nothing, a program that changed after it was approved
runs nothing, and the grant that covers a recovery is bound to the recovery and to the
content of its program.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from nanomuse.config import (
    HealthFileProbeSettings,
    HealthHTTPProbeSettings,
    HealthLoginStateSettings,
    HealthPIDProbeSettings,
    HealthRecoverySettings,
    HealthSettings,
    HealthTCPProbeSettings,
    SentinelSettings,
)
from nanomuse.health import HealthMonitor, InfrastructureHealth
from nanomuse.schema import Function, ToolCall
from nanomuse.sentinel import AuditLog, Sentinel
from nanomuse.ui import ApprovalDecision, ApprovalRequest, HeadlessUI
from nanomuse.vault import CredentialVault


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        return None

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()


class ScopedUI(HeadlessUI):
    """Approves with a chosen scope and keeps the requests for inspection."""

    def __init__(self, scope: str = "once", approve: bool = True):
        super().__init__(approve=approve)
        self.scope = scope
        self.requests: list[ApprovalRequest] = []

    async def ask_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        self._log("approval", request.summary)
        return ApprovalDecision(approved=self.approve, scope=self.scope)  # type: ignore[arg-type]


def _by_name(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in view["checks"]}


def _recover_call(name: str = "restart relay") -> ToolCall:
    return ToolCall(
        function=Function(name="health", arguments=json.dumps({"action": "recover", "name": name}))
    )


def _tool(
    tmp_path: Path,
    settings: HealthSettings,
    vault: CredentialVault,
) -> InfrastructureHealth:
    return InfrastructureHealth(
        monitor=HealthMonitor(settings, vault, tmp_path),
        vault=vault,
        data_dir=tmp_path,
    )


def _script(tmp_path: Path, name: str, body: str) -> Path:
    """An executable POSIX program the recovery can run."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ----------------------------------------------------------------------------- the read-only view
def test_health_monitor_reports_declared_checks_without_targets(tmp_path: Path) -> None:
    backup = tmp_path / "backup.done"
    backup.write_text("ok", encoding="utf-8")
    login = tmp_path / "login-state.json"
    login.write_text(
        json.dumps(
            {
                "ok": True,
                "checked_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    pid = tmp_path / "service.pid"
    pid.write_text(str(os.getpid()), encoding="utf-8")
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    vault.set("HEALTH_TOKEN", "secret-value")

    httpd = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        settings = HealthSettings(
            min_free_mb=0,
            min_free_percent=0,
            backups=[HealthFileProbeSettings(name="NAS backup", path=backup)],
            login_states=[HealthLoginStateSettings(name="Platform login", path=login)],
            pid_files=[HealthPIDProbeSettings(name="websockify", path=pid)],
            tcp=[
                HealthTCPProbeSettings(
                    name="console listener", host="127.0.0.1", port=httpd.server_port
                )
            ],
            http=[
                HealthHTTPProbeSettings(
                    name="relay",
                    url=f"http://127.0.0.1:{httpd.server_port}/health",
                    expected_status=[204],
                )
            ],
        )
        view = HealthMonitor(settings, vault, tmp_path).view()
    finally:
        httpd.shutdown()
        httpd.server_close()

    checks = _by_name(view)
    assert view["state"] == "ok"
    assert {
        "disk",
        "vault",
        "NAS backup",
        "Platform login",
        "websockify",
        "console listener",
        "relay",
    } == set(checks)
    assert all(item["state"] == "ok" for item in checks.values())
    rendered = json.dumps(view)
    assert str(tmp_path) not in rendered
    assert "127.0.0.1" not in rendered
    assert "secret-value" not in rendered


def test_health_monitor_marks_stale_backup_and_login_state_failed(tmp_path: Path) -> None:
    backup = tmp_path / "old-backup.done"
    backup.write_text("old", encoding="utf-8")
    old = time.time() - 7200
    os.utime(backup, (old, old))
    login = tmp_path / "expired.json"
    login.write_text(
        json.dumps(
            {
                "ok": True,
                "checked_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    settings = HealthSettings(
        backups=[HealthFileProbeSettings(name="backup", path=backup, max_age_hours=1)],
        login_states=[HealthLoginStateSettings(name="login", path=login)],
    )

    view = HealthMonitor(settings, vault, tmp_path).view()
    checks = _by_name(view)
    assert view["state"] == "error"
    assert checks["backup"]["state"] == "error"
    assert checks["login"] == {
        "name": "login",
        "kind": "login",
        "state": "error",
        "age_seconds": 0,
        "max_age_seconds": 172800,
        "expires_in_seconds": checks["login"]["expires_in_seconds"],
    }
    assert checks["login"]["expires_in_seconds"] <= 0


def test_vault_health_does_not_create_a_key_while_checking_a_broken_vault(tmp_path: Path) -> None:
    vault_file = tmp_path / "vault.enc"
    key_file = tmp_path / "vault.key"
    vault_file.write_bytes(b"not a vault")
    view = HealthMonitor(HealthSettings(), CredentialVault(vault_file, key_file), tmp_path).view()

    vault = _by_name(view)["vault"]
    assert vault == {"name": "vault", "kind": "vault", "state": "error", "error": "key_unavailable"}
    assert not key_file.exists()


def test_health_disk_probe_works_without_statvfs(tmp_path: Path) -> None:
    """The disk check must work on Windows, where ``os.statvfs`` does not exist."""
    import nanomuse.health as health_module

    assert "statvfs" not in Path(health_module.__file__).read_text("utf-8")
    view = HealthMonitor(HealthSettings(), CredentialVault(tmp_path / "v.enc"), tmp_path).view()
    disk = _by_name(view)["disk"]
    assert disk["kind"] == "disk"
    assert disk["state"] in {"ok", "warn"}
    assert disk["free_mb"] >= 0


def test_one_broken_probe_does_not_blank_the_whole_view(tmp_path: Path) -> None:
    """A probe that raises is its own error; the other checks still answer."""
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    monitor = HealthMonitor(HealthSettings(), vault, tmp_path)

    def explode() -> dict[str, Any]:
        raise RuntimeError("probe blew up")

    check = monitor._probe("exploding", "tcp", explode)
    assert check == {
        "name": "exploding",
        "kind": "tcp",
        "state": "error",
        "error": "RuntimeError",
    }
    view = monitor.view()
    assert {"disk", "vault"} == {item["name"] for item in view["checks"]}


def test_health_http_probe_survives_a_broken_answer(tmp_path: Path) -> None:
    """A half-written or non-HTTP answer is this probe's problem, not the view's."""

    class Broken(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            self.wfile.write(b"SSH-2.0-OpenSSH_9.0\r\n")
            self.wfile.flush()

    server = HTTPServer(("127.0.0.1", 0), Broken)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    settings = HealthSettings(
        http=[
            HealthHTTPProbeSettings(
                name="wrong port",
                url=f"http://127.0.0.1:{server.server_port}/health",
            )
        ]
    )
    try:
        check = HealthMonitor(settings, vault, tmp_path).check("wrong port")
    finally:
        server.shutdown()
        server.server_close()

    assert check["state"] == "error"
    assert check["name"] == "wrong port"


async def test_health_recovery_approval_card_shows_the_program_it_will_run(
    tmp_path: Path,
) -> None:
    """The card has to say what runs, or the person approves a program they cannot see.

    The program and the arguments are the owner's own configuration, so showing them shows
    the owner their own setup.  The environment is not shown: it carries the secrets.
    """
    program = _script(tmp_path, "recover", "exit 0")
    pid = tmp_path / "relay.pid"
    pid.write_text("999999", encoding="utf-8")
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=str(program),
                args=["--now"],
                cwd=tmp_path,
                env={"SERVICE_TOKEN": "{{vault:SERVICE_TOKEN}}"},
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    vault.set("SERVICE_TOKEN", "a-secret-value")
    tool = _tool(tmp_path, settings, vault)

    ui = ScopedUI(scope="once")
    gate = Sentinel(SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), ui, vault=vault)
    await gate.guard(_recover_call("restart relay"), tool)

    assert len(ui.requests) == 1
    detail = ui.requests[0].detail
    assert str(program) in detail
    assert "--now" in detail
    assert "a-secret-value" not in detail
    assert "SERVICE_TOKEN" not in detail, "the environment carries the secrets and stays out"
    assert "relay process" in ui.requests[0].summary or "restart relay" in ui.requests[0].summary


async def test_health_recovery_waits_before_running_the_same_recovery_again(
    tmp_path: Path,
) -> None:
    """A probe that stays unhealthy must not become a loop of processes.

    Each run starts a program that lives for up to timeout_s, so the model asking again and
    again would keep starting them.  The cooldown refuses the second attempt and says how
    long is left.
    """
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text("999999", encoding="utf-8")  # a pid that is not running
    marker = tmp_path / "ran"
    program = _script(tmp_path, "recover", f'echo ran >> "{marker}"')
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=str(program),
                cwd=tmp_path,
                cooldown_s=300,
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)
    gate = Sentinel(
        SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), HeadlessUI(approve=True), vault=vault
    )

    first = await gate.guard(_recover_call("restart relay"), tool)
    assert first.error and "still unhealthy" in first.error, "the probe is still down"
    runs = marker.read_text("utf-8").count("ran")
    assert runs == 1, "the program ran once"

    second = await gate.guard(_recover_call("restart relay"), tool)
    assert second.error and "ran" in second.error
    assert marker.read_text("utf-8").count("ran") == 1, "the second attempt is refused"


async def test_health_recovery_cooldown_can_be_turned_off(tmp_path: Path) -> None:
    """An owner who wants a retry loop can say so."""
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text("999999", encoding="utf-8")
    marker = tmp_path / "ran"
    program = _script(tmp_path, "recover", f'echo ran >> "{marker}"')
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=str(program),
                cwd=tmp_path,
                cooldown_s=0,
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)
    gate = Sentinel(
        SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), HeadlessUI(approve=True), vault=vault
    )

    await gate.guard(_recover_call("restart relay"), tool)
    await gate.guard(_recover_call("restart relay"), tool)
    assert marker.read_text("utf-8").count("ran") == 2, "no cooldown, so both attempts run"


def test_health_recovery_refuses_a_secret_in_the_clear_in_argv(tmp_path: Path) -> None:
    """A secret in the arguments is visible in the process list and in the audit record.

    The environment is the way to pass one.  The check is on the argument's name, so a
    secret under an ordinary name is not caught: nothing knows what a string means.
    """
    with pytest.raises(ValueError, match="secret in the clear"):
        HealthRecoverySettings(
            name="restart tunnel",
            probe="tunnel",
            program=sys.executable,
            args=["--api-token=abc123def456ghi789"],
        )
    with pytest.raises(ValueError, match="secret in the clear"):
        HealthRecoverySettings(
            name="restart tunnel",
            probe="tunnel",
            program=sys.executable,
            args=["--token", "abc123def456ghi789"],
        )
    # an ordinary argument name, and a short value, are not secrets
    HealthRecoverySettings(
        name="restart tunnel",
        probe="tunnel",
        program=sys.executable,
        args=["--service", "nas", "--token-mode", "refresh"],
    )


def test_health_tool_and_monitor_agree_on_the_recoveries(tmp_path: Path) -> None:
    """One list of recoveries, read by both.

    The tool runs the program and the monitor checks the probe it repairs. Two copies of
    the list could drift, and a recovery the tool offers but the monitor does not know
    would run against nothing.
    """
    pid = tmp_path / "relay.pid"
    pid.write_text("999999", encoding="utf-8")
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay", probe="relay process", program=sys.executable
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)

    assert [r.name for r in tool.recoveries] == ["restart relay"]
    # a recovery the owner declares later is visible to the tool without a second copy
    settings.recoveries.append(
        HealthRecoverySettings(name="restart backup", probe="relay process", program=sys.executable)
    )
    assert [r.name for r in tool.recoveries] == ["restart relay", "restart backup"]
    assert tool._recovery("restart backup") is not None


# ----------------------------------------------------------------------------- the declaration
def test_health_recovery_configuration_rejects_login_targets_and_secret_argv(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="PID, TCP or HTTP"):
        HealthSettings(
            login_states=[HealthLoginStateSettings(name="login", path=tmp_path / "login.json")],
            recoveries=[
                HealthRecoverySettings(name="login again", probe="login", program=sys.executable)
            ],
        )
    with pytest.raises(ValueError, match="absolute path"):
        HealthRecoverySettings(name="restart tunnel", probe="tunnel", program="service")
    with pytest.raises(ValueError, match="args cannot contain"):
        HealthRecoverySettings(
            name="restart tunnel",
            probe="tunnel",
            program=sys.executable,
            args=["{{vault:RECOVERY_TOKEN}}"],
        )
    with pytest.raises(ValueError, match="must use the vault"):
        HealthRecoverySettings(
            name="restart tunnel",
            probe="tunnel",
            program=sys.executable,
            env={"RECOVERY_TOKEN": "plain-secret"},
        )


def test_health_http_probe_does_not_forward_a_secret_header_across_redirects(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(204)
            self.end_headers()

    destination = HTTPServer(("127.0.0.1", 0), Destination)
    threading.Thread(target=destination.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{destination.server_port}/target"

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

    redirect = HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=redirect.serve_forever, daemon=True).start()
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    vault.set("HEALTH_AUTH", "Bearer health-secret")
    settings = HealthSettings(
        min_free_mb=0,
        min_free_percent=0,
        http=[
            HealthHTTPProbeSettings(
                name="redirecting relay",
                url=f"http://127.0.0.1:{redirect.server_port}/health",
                headers={"Authorization": "{{vault:HEALTH_AUTH}}"},
                expected_status=[302],
            )
        ],
    )
    try:
        check = HealthMonitor(settings, vault, tmp_path).check("redirecting relay")
    finally:
        redirect.shutdown()
        redirect.server_close()
        destination.shutdown()
        destination.server_close()

    assert check["state"] == "ok" and check["status"] == 302
    assert seen == []
    assert "health-secret" not in json.dumps(check)


# ----------------------------------------------------------------------------- the grant binding
def test_health_recovery_grant_changes_with_declaration_and_executable(
    tmp_path: Path,
) -> None:
    program = tmp_path / "recover"
    program.write_text("first", encoding="utf-8")
    recovery = HealthRecoverySettings(
        name="restart relay", probe="relay process", program=str(program), args=["one"]
    )
    first = InfrastructureHealth._grant_target(recovery)
    changed_args = recovery.model_copy(update={"args": ["two"]})
    assert InfrastructureHealth._grant_target(changed_args) != first
    time.sleep(0.001)
    program.write_text("second and changed", encoding="utf-8")
    assert InfrastructureHealth._grant_target(recovery) != first


def test_health_recovery_grant_changes_when_the_program_is_rewritten_in_place(
    tmp_path: Path,
) -> None:
    """A program rewritten with the same length and its mtime put back is a different program.

    The fingerprint follows the content of the file, not its metadata, so the standing
    approval for the old content does not cover the new one.
    """
    program = tmp_path / "recover"
    program.write_text("AAAA", encoding="utf-8")
    recovery = HealthRecoverySettings(
        name="restart relay", probe="relay process", program=str(program)
    )
    first = InfrastructureHealth._grant_target(recovery)

    marker = program.stat()
    program.write_text("BBBB", encoding="utf-8")
    os.utime(program, (marker.st_atime, marker.st_mtime))

    assert InfrastructureHealth._grant_target(recovery) != first


async def test_health_recovery_conversation_grant_stays_bound_to_that_recovery(
    tmp_path: Path,
) -> None:
    """ "For this conversation" is a permission for this recovery, not for the health tool.

    A recovery added afterwards, or one whose program changed, is a different capability
    and has to be approved on its own.
    """
    program = _script(tmp_path, "recover", "exit 0")
    pid = tmp_path / "relay.pid"
    pid.write_text("999999", encoding="utf-8")  # a pid that is not running

    def recovery_for(prog: Path, name: str) -> HealthRecoverySettings:
        return HealthRecoverySettings(name=name, probe="relay process", program=str(prog))

    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid)],
        recoveries=[recovery_for(program, "restart relay")],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)

    ui = ScopedUI(scope="conversation")
    gate = Sentinel(SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), ui, vault=vault)
    token = gate.begin_task("keep the relay up", conversation="thread-1")
    try:
        await gate.guard(_recover_call("restart relay"), tool)
        assert len(ui.requests) == 1
        # the grant is bound to the recovery, not to the tool
        keys = {g.key for g in gate.active_grants()}
        assert keys and all(key.startswith("health:") for key in keys)
        assert "health" not in keys

        # a recovery the owner declared later is not covered by the earlier permission
        other = _script(tmp_path, "other", "exit 0")
        settings.recoveries.append(recovery_for(other, "restart backup"))
        await gate.guard(_recover_call("restart backup"), tool)
        assert len(ui.requests) == 2, "a recovery nobody approved is asked about"
    finally:
        gate.end_task(token)


async def test_health_recovery_grant_does_not_survive_a_changed_program(tmp_path: Path) -> None:
    """An approval about a program is an approval about that program's content."""
    program = _script(tmp_path, "recover", "exit 0")
    pid = tmp_path / "relay.pid"
    pid.write_text("999999", encoding="utf-8")
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay", probe="relay process", program=str(program)
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)

    ui = ScopedUI(scope="always")
    gate = Sentinel(SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), ui, vault=vault)
    await gate.guard(_recover_call("restart relay"), tool)
    assert len(ui.requests) == 1

    # the owner replaces the program: the standing permission is about the old content
    program.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    await gate.guard(_recover_call("restart relay"), tool)
    assert len(ui.requests) == 2, "a changed program is a different capability"


async def test_health_recovery_tool_grant_does_not_cover_a_recovery(tmp_path: Path) -> None:
    """A permission for the whole health tool does not authorize a program to run."""
    program = _script(tmp_path, "recover", "exit 0")
    pid = tmp_path / "relay.pid"
    pid.write_text("999999", encoding="utf-8")
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay", probe="relay process", program=str(program)
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)

    ui = ScopedUI(scope="always")
    gate = Sentinel(SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), ui, vault=vault)
    # a standing permission for the whole tool
    gate.grants.add("health", None, "always")
    await gate.guard(_recover_call(), tool)
    assert len(ui.requests) == 1, "the tool-wide permission does not authorize the program"


# ----------------------------------------------------------------------------- the permit
async def test_health_recovery_is_sentinel_gated_and_redacts_process_output(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "relay.pid"
    secret = "recovery-secret-value"
    script = (
        "import os; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text({str(os.getpid())!r}, encoding='utf-8'); "
        "print(os.environ['RECOVERY_TOKEN'])"
    )
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=sys.executable,
                args=["-c", script],
                cwd=tmp_path,
                env={"RECOVERY_TOKEN": "{{vault:RECOVERY_TOKEN}}"},
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    vault.set("RECOVERY_TOKEN", secret)
    tool = _tool(tmp_path, settings, vault)

    denied_audit = AuditLog(tmp_path / "denied-audit.jsonl")
    denied = await Sentinel(
        SentinelSettings(), denied_audit, HeadlessUI(approve=False), vault=vault
    ).guard(_recover_call(), tool)
    assert denied.error and "Sentinel blocked" in denied.error
    assert not pid_file.exists()

    allowed_audit = AuditLog(tmp_path / "allowed-audit.jsonl")
    allowed = await Sentinel(
        SentinelSettings(), allowed_audit, HeadlessUI(approve=True), vault=vault
    ).guard(_recover_call(), tool)
    assert allowed.ok
    assert pid_file.read_text("utf-8") == str(os.getpid())
    assert secret not in allowed.output
    assert "[REDACTED:RECOVERY_TOKEN]" in allowed.output
    audit_text = (tmp_path / "allowed-audit.jsonl").read_text("utf-8")
    assert secret not in audit_text
    assert sys.executable not in audit_text
    assert "restart relay" in audit_text
    assert '"channel": "cli"' in audit_text


async def test_health_recovery_without_a_sentinel_permit_runs_nothing(tmp_path: Path) -> None:
    """A call that never went through the Sentinel has no permit and runs no program."""
    pid_file = tmp_path / "relay.pid"
    script = (
        "import os; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text({str(os.getpid())!r}, encoding='utf-8')"
    )
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=sys.executable,
                args=["-c", script],
                cwd=tmp_path,
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)

    result = await tool.execute(action="recover", name="restart relay")
    assert result.error and "not approved" in result.error
    assert not pid_file.exists()


async def test_health_recovery_permit_cannot_be_replayed(tmp_path: Path) -> None:
    """A permit is good for one call.  A second call needs the Sentinel again."""
    pid_file = tmp_path / "relay.pid"
    script = (
        "import os; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text({str(os.getpid())!r}, encoding='utf-8')"
    )
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=sys.executable,
                args=["-c", script],
                cwd=tmp_path,
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)
    gate = Sentinel(
        SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), HeadlessUI(approve=True), vault=vault
    )

    first = await gate.guard(_recover_call(), tool)
    assert first.ok
    pid_file.unlink()

    # the permit of the approved call is spent: the same call, run again, is refused
    replay = await tool.execute(action="recover", name="restart relay")
    assert replay.error and "not approved" in replay.error
    assert not pid_file.exists()


async def test_health_recovery_refuses_a_program_that_changed_after_the_approval(
    tmp_path: Path,
) -> None:
    """The program is checked again right before it starts, not only when it was assessed.

    The window that matters is between the approval and the run: a program replaced there
    is not the program the user agreed to, so it does not start.
    """
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text("999999", encoding="utf-8")  # a pid that is not running
    marker = tmp_path / "ran"
    original = _script(tmp_path, "recover", f'echo original > "{marker}"')

    class _SwappingMonitor(HealthMonitor):
        """Replaces the program while the "before" probe runs, which is the window."""

        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.swapped = False

        def check(self, name: str) -> dict[str, Any]:
            result = super().check(name)
            if not self.swapped:
                self.swapped = True
                original.write_text(f'#!/bin/sh\necho tampered > "{marker}"\n', encoding="utf-8")
            return result

    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=str(original),
                cwd=tmp_path,
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = InfrastructureHealth(
        monitor=_SwappingMonitor(settings, vault, tmp_path),
        vault=vault,
        data_dir=tmp_path,
    )

    gate = Sentinel(
        SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), HeadlessUI(approve=True), vault=vault
    )
    result = await gate.guard(_recover_call(), tool)
    assert result.error, "the program is not the one that was approved"
    assert "changed" in result.error
    assert not marker.exists()


async def test_health_recovery_runs_only_while_its_probe_is_unhealthy(tmp_path: Path) -> None:
    """A healthy probe means there is nothing to repair."""
    pid_file = tmp_path / "relay.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    marker = tmp_path / "ran"
    program = _script(tmp_path, "recover", f'echo ran > "{marker}"')
    settings = HealthSettings(
        pid_files=[HealthPIDProbeSettings(name="relay process", path=pid_file)],
        recoveries=[
            HealthRecoverySettings(
                name="restart relay",
                probe="relay process",
                program=str(program),
                cwd=tmp_path,
                verify_after_s=0,
            )
        ],
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    tool = _tool(tmp_path, settings, vault)
    gate = Sentinel(
        SentinelSettings(), AuditLog(tmp_path / "a.jsonl"), HeadlessUI(approve=True), vault=vault
    )

    result = await gate.guard(_recover_call(), tool)
    assert result.error and "healthy" in result.error
    assert not marker.exists()


# ------------------------------------------------------------------ the view never waits
def test_a_slow_probe_does_not_hold_the_view(tmp_path: Path) -> None:
    """The health endpoint answers a supervisor, so one endpoint that never answers must
    not make the whole view wait for it: the view is bounded and the probe is reported
    as unknown."""
    settings = HealthSettings(
        http=[HealthHTTPProbeSettings(name="slow", url="http://10.255.255.1/", timeout_s=30)]
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    monitor = HealthMonitor(settings, vault, tmp_path)
    started = time.monotonic()
    view = monitor.view()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the view waited {elapsed:.1f}s for a slow probe"
    assert view["state"] == "unknown"
    slow = next(c for c in view["checks"] if c["name"] == "slow")
    assert slow["state"] == "unknown"


def test_the_probes_run_together_rather_than_one_after_another(tmp_path: Path) -> None:
    """Two endpoints that each take a second cost the view one second, not two."""
    settings = HealthSettings(
        http=[
            HealthHTTPProbeSettings(name="a", url="http://10.255.255.1/", timeout_s=30),
            HealthHTTPProbeSettings(name="b", url="http://10.255.255.2/", timeout_s=30),
        ]
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    monitor = HealthMonitor(settings, vault, tmp_path)
    started = time.monotonic()
    monitor.view()
    elapsed = time.monotonic() - started
    # serial would be the sum of the two timeouts, together it is the deadline
    assert elapsed < 5.0, f"the probes ran one after the other ({elapsed:.1f}s)"


def test_a_collected_view_is_reused_for_a_few_seconds(tmp_path: Path) -> None:
    """The probes are read-only, so a view that was collected a moment ago is as good
    as a fresh one, and reusing it keeps a polling supervisor cheap."""
    settings = HealthSettings()
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    monitor = HealthMonitor(settings, vault, tmp_path)
    first = monitor.view()
    second = monitor.view()
    assert first is second, "the second view should be the collected one"
    # a change is noticed once the cache has gone by
    monitor._cached = (time.monotonic() - monitor.VIEW_CACHE_S - 1, first)
    third = monitor.view()
    assert third is not first


def test_a_stale_view_does_not_survive_the_cache(tmp_path: Path) -> None:
    """A view that says a check is broken must not keep saying so for longer than the
    cache allows, and a fresh view reports the truth again."""
    settings = HealthSettings()
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    monitor = HealthMonitor(settings, vault, tmp_path)
    stale = {"state": "error", "checks": []}
    monitor._cached = (time.monotonic() - monitor.VIEW_CACHE_S - 1, stale)
    view = monitor.view()
    assert view["state"] == "ok", "a stale error must not survive the cache"
    assert view is not stale


def test_the_view_keeps_the_order_the_owner_wrote(tmp_path: Path) -> None:
    """The view reads the same every time, so the checks are in the order they were
    declared."""
    settings = HealthSettings(
        backups=[
            HealthFileProbeSettings(name="second", path=tmp_path / "b"),
            HealthFileProbeSettings(name="first", path=tmp_path / "a"),
        ]
    )
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    monitor = HealthMonitor(settings, vault, tmp_path)
    view = monitor.view()
    names = [c["name"] for c in view["checks"]]
    assert names == ["disk", "vault", "second", "first"]
