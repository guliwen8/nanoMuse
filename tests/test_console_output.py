"""A program's output read in the console's code page (nanomuse/stdio.py)."""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import pytest

from nanomuse import stdio
from nanomuse.tools.shell import _run

CHINESE = "活动代码页: 936"


def test_bytes_that_are_utf8_are_read_as_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a tool that names its encoding stays readable on a console whose code page is 936
    monkeypatch.setattr(stdio, "console_encoding", lambda: "cp936")
    assert stdio.decode_output(CHINESE.encode("utf-8")) == CHINESE


def test_bytes_that_are_not_utf8_are_read_in_the_console_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stdio, "console_encoding", lambda: "cp936")
    assert stdio.decode_output(CHINESE.encode("cp936")) == CHINESE


def test_a_utf16_answer_with_a_byte_order_mark_comes_back_as_text() -> None:
    # wsl.exe answers its own `-l -q` this way, and the mark is what says so
    assert stdio.decode_output(CHINESE.encode("utf-16")) == CHINESE
    assert stdio.decode_output(codecs.BOM_UTF16_BE + CHINESE.encode("utf-16-be")) == CHINESE


def test_a_utf8_mark_is_read_off_rather_than_shown() -> None:
    # Notepad's "UTF-8 with a mark", and what `reg export` writes in the other order: the text
    # is UTF-8 either way, and the mark itself is not something the model should read
    assert stdio.decode_output(codecs.BOM_UTF8 + CHINESE.encode("utf-8")) == CHINESE


def test_a_console_that_reports_utf8_is_not_read_in_another_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 936 bytes under a UTF-8 console are broken bytes, and are shown as such rather than
    # guessed at: guessing is what turned the answer into something that looked reasoned
    monkeypatch.setattr(stdio, "console_encoding", lambda: "utf-8")
    assert "�" in stdio.decode_output(CHINESE.encode("cp936"))


def test_bytes_that_fit_no_encoding_are_replaced_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stdio, "console_encoding", lambda: "ascii")  # a POSIX C locale
    assert stdio.decode_output(b"\xff\xff\xff") == "���"
    monkeypatch.setattr(stdio, "console_encoding", lambda: "no-such-codec")
    assert stdio.decode_output(b"plain ascii") == "plain ascii"


def test_the_code_page_is_a_codec_python_knows() -> None:
    codecs.lookup(stdio.console_encoding())


def test_the_console_code_page_is_a_number_or_nothing() -> None:
    # the probe asks ctypes and takes whatever the platform does with that: on Windows a
    # number, everywhere else the attribute is not there and the answer is None
    page = stdio._console_code_page()
    assert page is None or (isinstance(page, int) and page > 0)


async def test_a_command_written_in_cp936_reaches_the_model_as_chinese(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # exactly what `cmd /c chcp` does on a Chinese Windows: 936 bytes into a pipe
    monkeypatch.setattr(stdio, "console_encoding", lambda: "cp936")
    script = f"import sys; sys.stdout.buffer.write({CHINESE!r}.encode('cp936'))"
    result = await _run([sys.executable, "-c", script], tmp_path, 60.0, False)
    assert CHINESE in result.output
    assert "�" not in result.output


async def test_the_terminal_approval_card_says_what_will_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the person agreeing in the terminal must see the program and its arguments, not only
    # the name of the action
    from rich.console import Console

    from nanomuse.console import ConsoleUI
    from nanomuse.ui import ApprovalRequest

    console = Console(record=True, width=120)
    ui = ConsoleUI(console)

    monkeypatch.setattr(Console, "input", lambda *_a, **_kw: "n")
    request = ApprovalRequest(
        tool="health",
        args={"action": "recover", "name": "restart browser"},
        summary="Restart the browser service?",
        risk="sensitive",
        reasons=["the browser VNC probe is unhealthy"],
        grant_options=["once"],
        detail="/usr/bin/systemctl restart nanomuse-browser.service",
    )
    await ui.ask_approval(request)
    shown = console.export_text()
    assert "/usr/bin/systemctl restart nanomuse-browser.service" in shown


async def test_the_terminal_approval_card_keeps_the_environment_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the environment carries the secrets; only the program and the arguments are shown
    from rich.console import Console

    from nanomuse.console import ConsoleUI
    from nanomuse.ui import ApprovalRequest

    console = Console(record=True, width=120)
    ui = ConsoleUI(console)
    monkeypatch.setattr(Console, "input", lambda *_a, **_kw: "n")
    request = ApprovalRequest(
        tool="health",
        args={"action": "recover", "name": "restart browser"},
        summary="Restart the browser service?",
        risk="sensitive",
        reasons=[],
        grant_options=["once"],
        detail="/usr/bin/systemctl restart nanomuse-browser.service",
    )
    await ui.ask_approval(request)
    shown = console.export_text()
    assert "SERVICE_TOKEN" not in shown
    assert "sk-" not in shown
