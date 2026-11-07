"""Skills: recipes for jobs, as SKILL.md folders."""

from nanomuse.skills.installer import (
    SkillBundle,
    github_source,
    install_from_url,
    write_bundle,
)
from nanomuse.skills.library import (
    BUILT_IN,
    BUILTIN_DIR,
    NAME_RE,
    SLASH,
    YOURS,
    Skill,
    SkillLibrary,
    fetch_skill_text,
    load_skill,
    parse_front_matter,
    raw_skill_url,
    render_skill,
)

__all__ = [
    "BUILTIN_DIR",
    "BUILT_IN",
    "NAME_RE",
    "SLASH",
    "YOURS",
    "Skill",
    "SkillBundle",
    "SkillLibrary",
    "fetch_skill_text",
    "github_source",
    "install_from_url",
    "load_skill",
    "parse_front_matter",
    "raw_skill_url",
    "render_skill",
    "write_bundle",
]
