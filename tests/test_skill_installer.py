"""The skill installer: a GitHub folder comes down whole — SKILL.md and the
scripts/, references/ and assets/ beside it."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from nanomuse.skills.installer import (
    SkillBundle,
    github_source,
    install_from_url,
    parse_bundle,
    read_source,
    write_bundle,
)

SKILL_MD = """---
name: pdf
description: Read, write and fill PDF files.
---

# PDF

Read `reference.md` for the details.
"""


def make_tarball(files: dict[str, bytes]) -> bytes:
    """A tarball shaped like GitHub's: a top directory ``<repo>-<ref>/``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, data in files.items():
            info = tarfile.TarInfo(f"skills-main/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ------------------------------------------------------------------ the link forms
def test_github_source_reads_every_link_form():
    assert github_source("https://github.com/o/r/tree/main/skills/pdf") == (
        "o",
        "r",
        "main",
        "skills/pdf",
    )
    # a file link: the folder is the path without the file name
    assert github_source("https://github.com/o/r/blob/v1/skills/pdf/SKILL.md") == (
        "o",
        "r",
        "v1",
        "skills/pdf",
    )
    assert github_source("https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md") == (
        "o",
        "r",
        "main",
        "skills/pdf",
    )
    assert github_source("https://example.com/x") is None


# ------------------------------------------------------------------ the tarball
def test_parse_bundle_reads_the_front_matter():
    files = [
        ("SKILL.md", SKILL_MD.encode()),
        ("reference.md", b"details\n"),
        ("scripts/fill.py", b"print('hi')\n"),
    ]
    bundle = parse_bundle(files)
    assert bundle.name == "pdf"
    assert bundle.description == "Read, write and fill PDF files."
    assert set(bundle.files) == {"reference.md", "scripts/fill.py"}
    assert "2 files" in bundle.summary()


def test_parse_bundle_needs_a_name_and_a_description():
    with pytest.raises(ValueError, match="no `name`"):
        parse_bundle([("SKILL.md", b"---\ndescription: d\n---\nbody\n")])
    with pytest.raises(ValueError, match="no `description`"):
        parse_bundle([("SKILL.md", b"---\nname: x\n---\nbody\n")])
    with pytest.raises(ValueError, match="no SKILL.md"):
        parse_bundle([("reference.md", b"x")])


# ------------------------------------------------------------------ writing it down
def test_write_bundle_lands_the_folder(tmp_path: Path):
    bundle = SkillBundle(
        name="pdf",
        description="Read, write and fill PDF files.",
        text=SKILL_MD,
        files={"reference.md": b"details\n", "scripts/fill.py": b"print('hi')\n"},
    )
    folder = write_bundle(bundle, tmp_path)
    assert (folder / "SKILL.md").read_text() == SKILL_MD
    assert (folder / "reference.md").read_text() == "details\n"
    assert (folder / "scripts" / "fill.py").exists()

    # installing again replaces it: a file of the older version that the new one does
    # not have any more is gone
    bundle.files = {"reference.md": b"new details\n"}
    write_bundle(bundle, tmp_path)
    assert (folder / "reference.md").read_text() == "new details\n"
    assert not (folder / "scripts" / "fill.py").exists()


# ------------------------------------------------------------------ the whole way
async def test_install_from_url_downloads_the_folder(monkeypatch: pytest.MonkeyPatch):
    archive = make_tarball(
        {
            "skills/pdf/SKILL.md": SKILL_MD.encode(),
            "skills/pdf/reference.md": b"details\n",
            "skills/pdf/scripts/fill.py": b"print('hi')\n",
            "skills/pdf/notes.txt": b"not part of the skill\n",
            "skills/other/SKILL.md": b"---\nname: other\ndescription: d\n---\nbody\n",
        }
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=archive)

    real = httpx.AsyncClient

    def client(**kw):  # noqa: ANN003
        kw["transport"] = httpx.MockTransport(handler)
        return real(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    bundle = await install_from_url("https://github.com/o/r/tree/main/skills/pdf")
    assert bundle.name == "pdf"
    # the whole folder comes down: the notes next to SKILL.md and the scripts
    assert set(bundle.files) == {"reference.md", "notes.txt", "scripts/fill.py"}
    # the folder of another skill does not
    assert "other" not in bundle.files
    assert seen and "codeload.github.com" in seen[0]


async def test_install_from_url_rejects_a_link_it_does_not_know():
    with pytest.raises(ValueError, match="skill folder on GitHub"):
        await install_from_url("https://example.com/skill")


def test_an_installed_skill_records_where_it_came_from(tmp_path: Path):
    """The same link tomorrow can serve different content, so the install is pinned.

    The record names the source and the fingerprint of what was installed, next to the
    skill. It is what answers "is this still the skill I installed".
    """
    bundle = SkillBundle(
        name="pdf",
        description="Read, write and fill PDF files.",
        text=SKILL_MD,
        files={"reference.md": b"the details"},
        source="https://github.com/owner/repo/tree/main/skills/pdf",
    )
    folder = write_bundle(bundle, tmp_path)

    record = read_source(folder)
    assert record["source"] == "https://github.com/owner/repo/tree/main/skills/pdf"
    assert record["digest"] == bundle.digest()
    assert record["files"] == ["reference.md"]
    assert record["installed_at"]

    # the record is not part of the skill's own content
    assert (folder / "source.json").is_file()
    assert not (folder / "source.json").read_text("utf-8").count("SKILL.md")


def test_the_fingerprint_follows_the_content(tmp_path: Path):
    """A different skill of the same name is a different skill."""
    one = SkillBundle(name="pdf", description="d", text=SKILL_MD, source="a")
    same = SkillBundle(name="pdf", description="d", text=SKILL_MD, source="b")
    other = SkillBundle(name="pdf", description="d", text=SKILL_MD + "more", source="a")

    assert one.digest() == same.digest(), "the source is not part of the content"
    assert one.digest() != other.digest(), "different content, different fingerprint"


def test_installing_again_replaces_the_record(tmp_path: Path):
    """Upgrading a skill updates what it says it is."""
    first = SkillBundle(
        name="pdf",
        description="d",
        text=SKILL_MD,
        files={"reference.md": b"one"},
        source="https://example.com/one",
    )
    folder = write_bundle(first, tmp_path)
    second = SkillBundle(
        name="pdf",
        description="d",
        text=SKILL_MD,
        files={"reference.md": b"two"},
        source="https://example.com/two",
    )
    write_bundle(second, tmp_path)

    record = read_source(folder)
    assert record["source"] == "https://example.com/two"
    assert record["digest"] == second.digest()
    assert (folder / "reference.md").read_bytes() == b"two"


async def test_an_install_records_what_arrived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The audit trail answers "what arrived", not only "what ran"."""
    from nanomuse.config import SkillsSettings
    from nanomuse.sentinel.audit import AuditLog
    from nanomuse.skills.library import SkillLibrary
    from nanomuse.tools.skills_tool import Skills

    archive = make_tarball(
        {
            "skills/pdf/SKILL.md": SKILL_MD.encode(),
            "skills/pdf/reference.md": b"details\n",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=archive)

    real = httpx.AsyncClient

    def client(**kw):  # noqa: ANN003
        kw["transport"] = httpx.MockTransport(handler)
        return real(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    own = tmp_path / "skills"
    own.mkdir(parents=True, exist_ok=True)
    lib = SkillLibrary(SkillsSettings(disabled=[]), own_dir=own)
    audit_path = tmp_path / "audit.jsonl"
    tool = Skills(library=lib, audit=AuditLog(audit_path))

    result = await tool.execute(action="install", url="https://github.com/o/r/tree/main/skills/pdf")
    assert result.ok

    entry = [
        line for line in audit_path.read_text("utf-8").splitlines() if '"skill_install"' in line
    ]
    assert len(entry) == 1
    record = json.loads(entry[0])
    assert record["target"] == "pdf"
    assert record["source"] == "https://github.com/o/r/tree/main/skills/pdf"
    assert record["digest"]
    assert record["files"] == ["reference.md"]
