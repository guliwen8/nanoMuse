"""The look-back: what a finished conversation leaves behind, and what it may not.

The point of the module is that the agent can get better without the person having
to teach it again — and that it cannot get better on its own. These tests hold both
ends of that: the proposals are real (they quote the conversation, they are worth
keeping), and they are harmless (nothing is written, nothing already known is
proposed twice).
"""

from __future__ import annotations

import json
from pathlib import Path

from nanomuse.config import SkillsSettings
from nanomuse.evolve import (
    EVOLVE_PROMPT,
    plan,
    propose,
    speaks_chinese,
    task_finished,
    transcript,
)
from nanomuse.llm import MockLLM
from nanomuse.memory import MemoryStore
from nanomuse.schema import Function, LLMResponse, Message, ToolCall
from nanomuse.skills import SkillLibrary

CONVERSATION = (
    "Person: 帮我发条朋友圈，带 3 张图，正文不超过 60 字\n"
    "Assistant: (used post_moment)\n"
    "Tool post_moment: posted\n"
    "Person: 好，以后朋友圈都按这个来\n"
)


def _skill(name: str, quote: str = "朋友圈都按这个来") -> dict[str, str]:
    return {
        "kind": "skill",
        "name": name,
        "description": "发一条朋友圈",
        "body": "1. 配 3 张图\n2. 正文不超过 60 字",
        "quote": quote,
        "reason": "这次做对了",
    }


def _memory(content: str, quote: str = "朋友圈都按这个来") -> dict[str, str]:
    return {
        "kind": "memory",
        "content": content,
        "category": "preference",
        "quote": quote,
        "reason": "他说的",
    }


def _reply(items: list[dict[str, str]]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _finished_conversation() -> list[Message]:
    """A conversation that ended with the agent calling the job done."""
    return [
        Message.user("帮我发条朋友圈，带 3 张图，正文不超过 60 字"),
        Message.assistant(
            tool_calls=[ToolCall(function=Function(name="post_moment", arguments="{}"))]
        ),
        Message.tool("posted", tool_call_id="c1", name="post_moment"),
        Message.assistant(
            tool_calls=[ToolCall(function=Function(name="terminate", arguments="{}"))]
        ),
        Message.user("好，以后朋友圈都按这个来"),
    ]


# ----------------------------------------------------------------- the ground rule


def test_a_proposal_has_to_quote_the_conversation():
    """The model may only propose what the conversation holds.

    The quote is looked for: a proposal that cannot point at the words it came from
    is dropped. This is what keeps the look-back from writing down things nobody
    said.
    """
    reply = _reply([_skill("post-moment", quote="something nobody ever said")])
    out = plan(reply, CONVERSATION)
    assert not out.skills
    assert out.skipped


def test_a_proposal_that_quotes_the_conversation_is_kept():
    out = plan(_reply([_skill("post-moment")]), CONVERSATION)
    assert len(out.skills) == 1
    assert out.skills[0].name == "post-moment"
    assert out.skills[0].body.startswith("1. 配 3 张图")
    assert not out.skipped


def test_the_quote_is_matched_even_when_the_spacing_differs():
    """The conversation is wrapped and indented; the words are still the same words."""
    reply = _reply([_skill("post-moment", quote="朋友圈  都按这个来")])
    assert plan(reply, CONVERSATION).skills


# ----------------------------------------------------------------- what is already known


def test_a_skill_the_person_already_has_is_not_proposed_again():
    out = plan(_reply([_skill("post-moment")]), CONVERSATION, known_skills=["post-moment"])
    assert not out.skills
    assert any("already there" in s for s in out.skipped)


def test_a_fact_already_in_memory_is_not_proposed_again():
    out = plan(
        _reply([_memory("朋友圈配 3 张图")]),
        CONVERSATION,
        known_memories=["朋友圈配 3 张图，正文不超过 60 字"],
    )
    assert not out.memories
    assert any("already known" in s for s in out.skipped)


def test_two_proposals_for_the_same_skill_leave_one():
    out = plan(_reply([_skill("post-moment"), _skill("post-moment")]), CONVERSATION)
    assert len(out.skills) == 1
    assert out.skipped


# ----------------------------------------------------------------- what a proposal must be


def test_a_skill_without_a_proper_name_is_dropped():
    out = plan(_reply([_skill("Post Moment!")]), CONVERSATION)
    assert not out.skills
    assert any("not a name" in s for s in out.skipped)


def test_a_skill_without_steps_is_dropped():
    item = _skill("post-moment")
    item["body"] = ""
    out = plan(_reply([item]), CONVERSATION)
    assert not out.skills
    assert any("no steps" in s for s in out.skipped)


def test_a_proposal_of_a_kind_nobody_asked_for_is_dropped():
    item = _skill("post-moment")
    item["kind"] = "vibes"
    out = plan(_reply([item]), CONVERSATION)
    assert not out.skills and not out.memories
    assert out.skipped


def test_only_a_few_proposals_are_kept():
    items = [_skill(f"job-{i}") for i in range(6)]
    out = plan(_reply(items), CONVERSATION)
    assert len(out.skills) <= 3
    assert out.skipped


# ----------------------------------------------------------------- what the person reads


def test_the_proposal_carries_the_wording_to_save():
    """One word from the person is enough: the message holds the skill and the fact."""
    out = plan(_reply([_skill("post-moment"), _memory("配 3 张图，正文 60 字内")]), CONVERSATION)
    text = out.message()
    assert "post-moment" in text and "配 3 张图" in text
    assert "正文不超过 60 字" in text
    assert "reply" in text.lower()


def test_the_proposal_is_written_in_the_person_s_language():
    out = plan(_reply([_skill("post-moment")]), CONVERSATION)
    assert "要不要存下来" in out.message(zh=True)
    assert "keep" in out.message().lower()


def test_an_empty_look_back_says_nothing():
    from nanomuse.evolve import Evolution

    assert Evolution().message() == ""


# ----------------------------------------------------------------- the conversation


def test_the_transcript_is_what_the_model_reads_back():
    text = transcript(_finished_conversation())
    assert "帮我发条朋友圈" in text
    assert "post_moment" in text
    assert "posted" in text


def test_a_conversation_is_only_worth_looking_back_over_once_a_job_was_called_done():
    assert task_finished(_finished_conversation())
    unfinished = [m for m in _finished_conversation() if not (m.tool_calls or [])]
    assert not task_finished(unfinished)


def test_the_language_follows_the_person():
    assert speaks_chinese([Message.user("帮我发条朋友圈")])
    assert not speaks_chinese([Message.user("post a moment for me")])


# ----------------------------------------------------------------- the whole thing


async def test_the_look_back_asks_the_model_and_checks_what_it_says():
    llm = MockLLM(
        [
            LLMResponse(
                content=_reply([_skill("post-moment"), _memory("正文不超过 60 字")]),
                finish_reason="stop",
            )
        ]
    )
    out = await propose(_finished_conversation(), llm)
    assert len(out.skills) == 1 and len(out.memories) == 1
    # the model was shown the conversation
    shown = llm.calls[0]["messages"][-1].content or ""
    assert EVOLVE_PROMPT.split("{transcript}")[0] in shown
    assert "帮我发条朋友圈" in shown


async def test_a_model_that_says_nothing_leaves_nothing_behind():
    llm = MockLLM([LLMResponse(content="[]", finish_reason="stop")])
    out = await propose(_finished_conversation(), llm)
    assert out.empty
    assert out.message() == ""


async def test_the_look_back_writes_nothing(tmp_path: Path):
    """It proposes. It does not save — the person does, through the tools that ask."""
    library = SkillLibrary(SkillsSettings(), own_dir=tmp_path / "skills")
    store = MemoryStore(tmp_path / "m.db")
    store.add("已经记着的事实")
    before = {s.name for s in library.all()}
    llm = MockLLM(
        [
            LLMResponse(
                content=_reply([_skill("post-moment"), _memory("正文不超过 60 字")]),
                finish_reason="stop",
            )
        ]
    )
    await propose(_finished_conversation(), llm)
    assert {s.name for s in library.all()} == before
    assert "post-moment" not in before
    assert len(store.all()) == 1
    assert store.all()[0].content == "已经记着的事实"


# ----------------------------------------------------------------- in the running app


async def test_after_a_turn_the_proposal_arrives_in_the_chat(settings):
    """The look-back runs when a turn ends and puts its proposal in the chat."""
    from nanomuse.server.service import MuseService

    llm = MockLLM(
        [
            LLMResponse(
                content=_reply([_skill("post-moment", quote="帮我发条朋友圈")]),
                finish_reason="stop",
            )
        ]
    )
    service = MuseService(settings, llm=llm)
    thread = service.threads["main"]
    thread.agent.messages = _finished_conversation()

    await service._look_back(thread)

    texts = [e.get("text") for e in thread.timeline.events if e.get("type") == "assistant"]
    assert texts, "the proposal never reached the chat"
    assert "post-moment" in texts[-1]
    # and the agent carries it, so the next turn can save it
    assert "post-moment" in (thread.agent.messages[-1].content or "")


async def test_a_turn_that_ended_without_a_finished_job_looks_back_over_nothing(settings):
    from nanomuse.server.service import MuseService

    llm = MockLLM([LLMResponse(content=_reply([_skill("post-moment")]), finish_reason="stop")])
    service = MuseService(settings, llm=llm)
    thread = service.threads["main"]
    thread.agent.messages = [Message.user("你好")]

    await service._look_back(thread)

    assert not llm.calls, "the model was asked about a conversation with no job in it"
    assert not [e for e in thread.timeline.events if e.get("type") == "assistant"]


async def test_the_look_back_can_be_switched_off(settings):
    from nanomuse.server.service import MuseService

    settings.agent.self_review = False
    llm = MockLLM([LLMResponse(content=_reply([_skill("post-moment")]), finish_reason="stop")])
    service = MuseService(settings, llm=llm)
    thread = service.threads["main"]
    thread.agent.messages = _finished_conversation()

    await service._look_back(thread)

    assert not llm.calls
