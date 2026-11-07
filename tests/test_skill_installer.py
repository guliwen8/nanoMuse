"""The skill installer: a GitHub folder comes down whole — SKILL.md and the
scripts/, references/ and assets/ beside it."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest

from nanomuse.skills.installer import (
    SkillBundle,
    github_source,
    install_from_url,
    parse_bundle,
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
