"""Install a skill from a link: not only its ``SKILL.md`` but the folder it lives in.

A skill on GitHub is a folder — ``SKILL.md`` plus the ``scripts/``, ``references/`` and
``assets/`` its instructions point at. Fetching only the ``SKILL.md`` gives the model
instructions that tell it to read files that are not there, so the whole folder comes
down: the repository is fetched as a tarball (``codeload.github.com``), the one folder
is taken out of it, and the rest is left alone.

Two links are understood:

* a **folder** — ``https://github.com/<owner>/<repo>/tree/<ref>/<path>``: that one
  folder is installed;
* a **file** — ``https://github.com/<owner>/<repo>/blob/<ref>/<path>/SKILL.md`` or a
  ``raw.githubusercontent.com`` link: the folder around that file is installed.

The request goes through the split proxy when there is one: GitHub is not reachable
from a machine inside China without an overseas exit.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanomuse.logger import logger

#: a folder link: the folder's path in the repository
_GITHUB_TREE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$")
#: a file link on github.com: the folder is the path without the file name
_GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
#: a file link on raw.githubusercontent.com
_RAW_FILE = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$")

#: what a skill folder may hold besides SKILL.md
RESOURCE_DIRS = ("scripts", "references", "assets")
#: files in a skill folder that are part of the skill
MAX_SKILL_BYTES = 64 * 1024
#: a file that comes with a skill (a script, a reference, an asset): bigger than a
#: SKILL.md, still small
MAX_FILE_BYTES = 2 * 1024 * 1024
#: the whole tarball: a repository of skills is a few MB
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
#: how many files one skill folder may bring
MAX_FILES = 200


@dataclass
class SkillBundle:
    """One skill folder, ready to be written to disk: the ``SKILL.md`` and what came
    with it."""

    name: str
    description: str
    text: str
    files: dict[str, bytes] = field(default_factory=dict)
    source: str = ""

    def summary(self) -> str:
        extra = len(self.files)
        return (
            f"{self.name} ({self.description[:80]}"
            + (f", {extra} file{'s' if extra != 1 else ''}" if extra else "")
            + ")"
        )

    def digest(self) -> str:
        """A fingerprint of what this bundle contains.

        The same link tomorrow can serve different content: the branch moved, the tag was
        moved, the account was taken over. A fingerprint of the bytes is what makes "is the
        skill I installed still the skill I installed" a question with an answer.
        """
        digest = hashlib.sha256()
        digest.update(self.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.text.encode("utf-8"))
        for rel in sorted(self.files):
            digest.update(b"\x00")
            digest.update(rel.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(self.files[rel])
        return digest.hexdigest()


#: the file inside an installed skill folder that says where it came from and what it was
SOURCE_FILE = "source.json"


def write_source(folder: Path, bundle: SkillBundle) -> Path:
    """Write where the skill came from and what it contained, next to the skill.

    The record is what an owner reads to answer "what is installed here, and is it still
    what I installed". It names the source and the fingerprint of the content.
    """
    path = folder / SOURCE_FILE
    path.write_text(
        json.dumps(
            {
                "source": bundle.source,
                "digest": bundle.digest(),
                "files": sorted(bundle.files),
                "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    return path


def read_source(folder: Path) -> dict[str, Any]:
    """What the record says, or an empty one when the skill came from somewhere else."""
    path = folder / SOURCE_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def github_source(url: str) -> tuple[str, str, str, str] | None:
    """``(owner, repo, ref, folder)`` for a link into a GitHub repository, or None.

    ``folder`` is the path of the skill folder inside the repository (without the file
    name for a file link)."""
    url = (url or "").strip()
    m = _GITHUB_TREE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        return owner, repo, ref, path.strip("/")
    m = _GITHUB_BLOB.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        return owner, repo, ref, folder.strip("/")
    m = _RAW_FILE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        return owner, repo, ref, folder.strip("/")
    return None


def _split_proxy(workspace: Path | None) -> str | None:
    """The split proxy's URL for this workspace, or None when there is none.

    The proxy is an aid, not a requirement: a machine that can reach GitHub on its own
    gets a direct connection."""
    try:
        from nanomuse.tools.net_proxy import playwright_proxy

        cfg = playwright_proxy(workspace)
        if cfg:
            return cfg.get("server")
    except Exception:  # noqa: BLE001 - the proxy is an aid, not a requirement
        logger.debug("skills: no split proxy for {}", workspace)
    return None


async def fetch_archive(owner: str, repo: str, ref: str, workspace: Path | None = None) -> bytes:
    """The repository as a tarball. ``ValueError`` when it does not come."""
    import httpx

    url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{ref}"
    proxy = _split_proxy(workspace)
    try:
        async with (
            httpx.AsyncClient(timeout=60, follow_redirects=True, proxy=proxy) as client,
            client.stream("GET", url, headers={"User-Agent": "nanoMuse"}) as r,
        ):
            if r.status_code >= 400:
                raise ValueError(f"HTTP {r.status_code} for {owner}/{repo}@{ref}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("that repository is too large to download")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise ValueError(f"could not download {owner}/{repo}@{ref}: {type(exc).__name__}") from exc


def _member_files(archive: bytes, folder: str) -> list[tuple[str, bytes]]:
    """The files of ``folder`` inside the tarball, as ``(relative path, bytes)``.

    The tarball's top directory is ``<repo>-<ref>/``; the folder is matched inside it.
    What comes down is the whole folder: ``SKILL.md``, the three resource directories
    (``scripts/``, ``references/``, ``assets/``) and the files that sit next to it —
    a skill's own notes (``reference.md``), its forms, its licence. Nothing else."""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except tarfile.TarError as exc:
        raise ValueError(f"that download is not a readable archive: {exc}") from exc

    wanted = folder.strip("/")
    out: list[tuple[str, bytes]] = []
    for member in tf.getmembers():
        if not member.isfile():
            continue
        name = member.name
        # strip the archive's own top directory
        parts = name.split("/", 1)
        if len(parts) != 2:
            continue
        rel = parts[1]
        if wanted:
            if not rel.startswith(wanted + "/"):
                continue
            rel = rel[len(wanted) + 1 :]
        if not rel:
            continue
        head = rel.split("/", 1)[0]
        # the resource directories, and the files next to SKILL.md
        if head != "SKILL.md" and head not in RESOURCE_DIRS and "/" in rel:
            continue
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        limit = MAX_SKILL_BYTES if rel == "SKILL.md" else MAX_FILE_BYTES
        if member.size > limit:
            continue
        if len(out) >= MAX_FILES:
            break
        handle = tf.extractfile(member)
        if handle is None:
            continue
        out.append((rel, handle.read()))
    return out


def parse_bundle(files: list[tuple[str, bytes]]) -> SkillBundle:
    """The ``SKILL.md`` and the files beside it → one skill."""
    from nanomuse.skills.library import parse_front_matter

    text = ""
    extra: dict[str, bytes] = {}
    for rel, data in files:
        if rel == "SKILL.md":
            text = data.decode("utf-8", errors="replace")
        else:
            extra[rel] = data
    if not text:
        raise ValueError("that folder has no SKILL.md in it")
    meta, _body = parse_front_matter(text)
    name = str(meta.get("name") or "").strip().lower()
    description = str(meta.get("description") or "").strip()
    if not name:
        raise ValueError("that SKILL.md has no `name` in its front matter")
    if not description:
        raise ValueError("that SKILL.md has no `description` in its front matter")
    return SkillBundle(name=name, description=description, text=text, files=extra)


async def install_from_url(url: str, workspace: Path | None = None) -> SkillBundle:
    """The skill at a link, with everything that comes with it.

    ``ValueError`` when the link is not one this understands or when it does not answer."""
    source = github_source(url)
    if source is None:
        raise ValueError(
            "give a link to a skill folder on GitHub "
            "(https://github.com/<owner>/<repo>/tree/<branch>/<path>) "
            "or to a SKILL.md in one"
        )
    owner, repo, ref, folder = source
    archive = await fetch_archive(owner, repo, ref, workspace)
    files = _member_files(archive, folder)
    if not files:
        raise ValueError(f"no skill found at {owner}/{repo}/{folder or 'root'}")
    bundle = parse_bundle(files)
    bundle.source = f"https://github.com/{owner}/{repo}/tree/{ref}/{folder}".rstrip("/")
    return bundle


def write_bundle(bundle: SkillBundle, own_dir: Path) -> Path:
    """Write the skill folder into ``own_dir``; the folder of the skill's name.

    An existing folder of the same name is replaced: installing again is how a skill
    is upgraded."""
    folder = own_dir / bundle.name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(bundle.text, "utf-8")
    # the files that came with it
    for rel, data in bundle.files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # files of an earlier version that this one does not have any more
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(folder).as_posix()
        if rel in ("SKILL.md", SOURCE_FILE) or rel in bundle.files:
            continue
        head = rel.split("/", 1)[0]
        if head in RESOURCE_DIRS or "/" not in rel:
            path.unlink()
    # where it came from and what it contained, so the owner can tell this skill apart
    # from another one of the same name
    write_source(folder, bundle)
    return folder


def bundle_view(bundle: SkillBundle, folder: Path) -> dict[str, Any]:
    """What the app shows for an installed skill."""
    return {
        "name": bundle.name,
        "description": bundle.description,
        "source": "yours",
        "enabled": True,
        "path": str(folder),
        "files": sorted(bundle.files),
        "installed_from": bundle.source,
        "digest": bundle.digest(),
    }


__all__ = [
    "MAX_FILES",
    "RESOURCE_DIRS",
    "SkillBundle",
    "bundle_view",
    "fetch_archive",
    "github_source",
    "install_from_url",
    "parse_bundle",
    "write_bundle",
]
