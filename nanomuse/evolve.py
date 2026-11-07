"""Learning from a conversation that just happened.

nanoMuse can write a skill and can remember a fact — but both wait for the person to
ask for them. The system prompt says so on purpose: a skill is a standing instruction
for every later session, and a memory line is a fact about one person. Both are worth
having and neither should appear because the agent felt like it.

This module is the other half: a look back over a conversation that has just
finished, and a proposal of what was worth keeping. A job that worked, with the
specifics that made it work — which account, which format, which limits. A durable
fact the person gave on the way. The model proposes; this module decides.

What it decides:

- Every proposal carries a quote from the conversation it came from, and the quote is
  looked for in the transcript. A proposal that cannot quote itself is dropped: the
  model may not hand over something the conversation does not hold.
- A skill the person already has, or a fact already in memory, is dropped as
  something already known.
- A name that is not a name, a skill with no steps, a fact with no content — dropped,
  each with a reason, so the skip is visible instead of silent.

What it does not decide: nothing is written here. The proposals leave as a message,
the person says yes or no, and what they approve is saved by the tools that already
know how to save it — a skill through ``skills``, a fact through ``remember`` — each
of which asks the person once more before anything lands. An agent that could
change itself without anyone in the loop would be a different, and worse, thing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from nanomuse.llm.base import BaseLLM
from nanomuse.memory.consolidate import _objects
from nanomuse.schema import Message, Role

EVOLVE_PROMPT = """You are looking back over a conversation you just had with one person. Your job: notice what was worth keeping, and propose it.

Two kinds of thing are worth keeping:
- a skill: a job that worked, or that the person showed you how to do, and that they will want done again. Write the steps with the specifics that made it work: which account, which format, which limits, which wording they liked.
- a memory: a durable fact about the person: how they want things done, a preference, a name, an account, a rule they gave you.

Rules:
- Propose only what the conversation holds. No invention, no generalising, no "next time I should probably also…".
- A one-off request, a failed attempt, and anything the person asked you not to remember are not worth keeping.
- Skip what is already known: a skill that already exists, a fact already held.
- Every proposal needs a `quote`: a few words copied exactly from the conversation that show why you are proposing it. If you cannot quote it, do not propose it.
- Write in the person's own language.

Conversation:
{transcript}

Answer with a JSON array only, no prose. Items:
{{"kind": "skill", "name": "<kebab-case>", "description": "<one line: when to use it>", "body": "<the steps, as markdown, with the specifics>", "quote": "<copied from the conversation>", "reason": "<a few words>"}}
{{"kind": "memory", "content": "<one short fact>", "category": "<preference|profile|contact|constraint|general>", "quote": "<copied from the conversation>", "reason": "<a few words>"}}"""

MAX_SKILLS = 3
MAX_MEMORIES = 3
MAX_BODY = 4000
MAX_TRANSCRIPT = 12000
MAX_TOOL_OUTPUT = 240

# a skill name is what ``/name`` and the file name are made of: lowercase, hyphens
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class SkillProposal:
    """A job worth doing again, and the specifics that made it work."""

    name: str
    description: str
    body: str
    quote: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "quote": self.quote,
            "reason": self.reason,
        }


@dataclass
class MemoryProposal:
    """A durable fact about the person."""

    content: str
    category: str
    quote: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "category": self.category,
            "quote": self.quote,
            "reason": self.reason,
        }


@dataclass
class Evolution:
    """What a look-back found worth keeping — proposed, never written."""

    skills: list[SkillProposal] = field(default_factory=list)
    memories: list[MemoryProposal] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # proposals that failed a check

    @property
    def empty(self) -> bool:
        return not self.skills and not self.memories

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": [s.to_dict() for s in self.skills],
            "memories": [m.to_dict() for m in self.memories],
            "skipped": self.skipped,
        }

    def message(self, zh: bool = False) -> str:
        """The message the person reads — and the agent reads on the next turn.

        It carries the wording of the skill and the wording of the fact, so that one
        word is enough for the agent to save them: it copies them into ``skills`` and
        ``remember``, which ask the person once more before anything is written.
        """
        if self.empty:
            return ""
        if zh:
            head = "刚才这次对话里学到几件事，要不要存下来？"
            tail = (
                "回「存」我就写进技能库和长期记忆（照例会再弹一次审批给你确认）；回「不用」就算了。"
            )
        else:
            head = "A few things came out of that conversation. Keep them?"
            tail = (
                'Reply "keep" and I will write them into the skill library and long-term '
                'memory (you will get the usual approval card); reply "no" and that is that.'
            )
        lines = [head, ""]
        for skill in self.skills:
            lines.append(f"**技能 `{skill.name}`**：{skill.description}")
            lines.append("")
            lines.append(skill.body)
            lines.append("")
        for memory in self.memories:
            lines.append(f"**记忆**（{memory.category}）：{memory.content}")
            lines.append("")
        lines.append(tail)
        return "\n".join(lines)


def transcript(messages: list[Message], limit: int = MAX_TRANSCRIPT) -> str:
    """The conversation as the model should read it back.

    What was said and what came of it: the person's turns, the assistant's words, and
    the tools that ran. A tool's output is trimmed — a page of HTML says less about
    what the person wants than the sentence that asked for it.
    """
    lines: list[str] = []
    for message in messages:
        text = (message.content or "").strip()
        if message.role == Role.USER and text:
            lines.append(f"Person: {text}")
        elif message.role == Role.ASSISTANT:
            if text:
                lines.append(f"Assistant: {text}")
            for call in message.tool_calls or []:
                lines.append(f"Assistant: (used {call.name})")
        elif message.role == Role.TOOL and text:
            lines.append(f"Tool {message.name or ''}: {text[:MAX_TOOL_OUTPUT]}")
    joined = "\n".join(lines)
    return joined[-limit:] if len(joined) > limit else joined


def _norm(text: str) -> str:
    """The words only, lowercased, without any space.

    Two lines that differ in spacing are the same line, and a quote copied with a
    line break in it is still the same words.
    """
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _quoted(quote: str, source: str) -> bool:
    """Is the quote really from the conversation?"""
    needle = _norm(quote)
    return bool(needle) and needle in _norm(source)


def plan(
    reply: str,
    source: str,
    known_skills: Iterable[str] = (),
    known_memories: Iterable[str] = (),
) -> Evolution:
    """Turn the model's reply into checked proposals.

    The model may only propose what the conversation holds: every proposal has to
    quote it, and the quote is looked for. A skill the person already has, a fact
    already in memory, a name that is not a name — each is dropped, with the reason
    kept so the skip is visible.
    """
    out = Evolution()
    names = {n.strip().lower() for n in known_skills}
    memories = [_norm(m) for m in known_memories if _norm(m)]
    for item in _objects(reply):
        kind = str(item.get("kind") or "").strip().lower()
        quote = str(item.get("quote") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not quote or not _quoted(quote, source):
            out.skipped.append("a proposal without a quote from the conversation")
            continue
        if kind == "skill":
            name = str(item.get("name") or "").strip().lower()
            description = str(item.get("description") or "").strip()
            body = str(item.get("body") or "").strip()
            if not _NAME_RE.match(name):
                out.skipped.append(f"skill name {name!r} is not a name")
                continue
            if not description or not body:
                out.skipped.append(f"skill {name!r} has no description or no steps")
                continue
            if len(body) > MAX_BODY:
                out.skipped.append(f"skill {name!r} is too long to keep")
                continue
            if name in names:
                out.skipped.append(f"skill {name!r} is already there")
                continue
            if len(out.skills) >= MAX_SKILLS:
                out.skipped.append(f"skill {name!r}: only {MAX_SKILLS} at a time")
                continue
            names.add(name)
            out.skills.append(SkillProposal(name, description, body, quote, reason))
        elif kind == "memory":
            content = str(item.get("content") or "").strip()
            category = str(item.get("category") or "general").strip() or "general"
            if not content:
                out.skipped.append("a memory proposal with no content")
                continue
            norm = _norm(content)
            if any(norm == m or norm in m or m in norm for m in memories):
                out.skipped.append(f"memory {content!r} is already known")
                continue
            if len(out.memories) >= MAX_MEMORIES:
                out.skipped.append(f"memory {content!r}: only {MAX_MEMORIES} at a time")
                continue
            memories.append(norm)
            out.memories.append(MemoryProposal(content, category, quote, reason))
        else:
            out.skipped.append(f"a proposal of kind {kind!r}")
    return out


async def propose(
    messages: list[Message],
    llm: BaseLLM,
    known_skills: Iterable[str] = (),
    known_memories: Iterable[str] = (),
) -> Evolution:
    """Look back over the conversation and propose what to keep."""
    source = transcript(messages)
    if not source:
        return Evolution()
    reply = await llm.ask_complete(
        [Message.user(EVOLVE_PROMPT.format(transcript=source))], tools=None
    )
    return plan(reply.content or "", source, known_skills, known_memories)


def speaks_chinese(messages: list[Message]) -> bool:
    """Whether the person writes in Chinese — the proposal is worded to match."""
    for message in messages:
        if message.role == Role.USER:
            for char in message.content or "":
                if "一" <= char <= "鿿":
                    return True
    return False


def task_finished(messages: list[Message]) -> bool:
    """Did the conversation end with a job that was called done?

    ``terminate`` is how the agent says the request is handled. A conversation that
    stopped on the step limit, on an error or on a question has no finished job, and
    there is nothing to learn from a job that never was.
    """
    last_calls: list[Any] = []
    for message in messages:
        if message.role == Role.ASSISTANT and message.tool_calls:
            last_calls = list(message.tool_calls)
    return any(getattr(call, "name", "") == "terminate" for call in last_calls)


__all__ = [
    "EVOLVE_PROMPT",
    "Evolution",
    "MemoryProposal",
    "SkillProposal",
    "plan",
    "propose",
    "speaks_chinese",
    "task_finished",
    "transcript",
]
