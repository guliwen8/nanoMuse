# The look-back

nanoMuse can write a skill and it can remember a fact — but both wait for you to ask.
"From now on do it like that" is a request; "save that" is another request. Say
nothing and nothing changes.

The look-back is the other half: when a task is over, the agent looks back over the
conversation, asks itself what was worth keeping, and puts the answer in front of
you.

```
a task ends
   └─► the look-back (in the background, one model call)
          ├─► a skill: a job that worked, with the specifics that made it work
          ├─► a memory: a durable fact you gave on the way
          └─► one message: keep these?
                    └─► you say "keep" ──► it saves them with skills / remember
                                             └─► the usual approval card
```

## The two kinds

| | what it is | an example |
|---|---|---|
| **a skill** | a job that worked, or that you showed it how to do, and that you will want done again — written with the specifics that made it work: which account, which format, which limits, which wording you liked | "Post a moment: three images, the text under 60 characters, show me the caption before it goes out" |
| **a memory** | a durable fact about you: how you want things done, a preference, a name, an account, a rule you gave | "The main Xiaohongshu account is 柠檬不加冰" |

## The ground rule: every proposal points at where it came from {#ground-rule}

The model proposes; the module decides. This is the one rule that matters:

> **Every proposal carries a `quote` copied verbatim from the conversation, and the
> module goes looking for that sentence. If it is not there, the proposal is
> dropped.**

So it cannot hand you something you never said — a proposal that cannot quote itself
does not count. On top of that:

- a skill name has to be a name (lowercase, hyphens, up to 64 characters)
- a skill needs a description and steps
- a skill you already have is not proposed again; a fact already in memory is not
  proposed again
- at most three skills and three memories are kept in one pass

The reasons for dropping something are kept, not dropped silently.

## It writes nothing itself {#nothing-written}

The look-back produces proposals. It does not write a single byte. Each completed review is also recorded in the append-only audit trail with only its outcome and proposal counts, never the proposed text or the conversation content.

Writing is the next step, and it goes through the two paths that already existed:
`skills` (save a skill) and `remember` (remember a fact). Both are `sensitive` — the
Sentinel asks you first ([Sentinel](sentinel.md)). So what actually lands is:

1. the look-back proposes → one message
2. you say "keep" → it calls `skills` / `remember`
3. the Sentinel shows the approval card → you confirm
4. only then is anything written

**An agent that could change itself with nobody watching would be a different, and
worse, thing.** What this is for is "teach it once, it remembers, and it uses it by
itself next time" — not "it decides who it is".

## When it happens {#when}

- **A foreground turn ended and the job was called done** (the model called
  `terminate`). A turn that did not get there (the step limit, an error, a question
  left hanging) has no finished job to learn from.
- **Background work does not look back**: routines and goal checks never talked to
  you, so they do not interrupt you either.
- It does not talk over you: while you are already typing the next thing, the
  proposal waits for the next finished turn.

## Turning it off {#off}

```toml
[agent]
self_review = false
```

On by default. It costs one short model call per finished turn — reading the
conversation and producing proposals — and changes nothing by itself.

## Compared with asking it to remember {#versus-asking}

| | asking | the look-back |
|---|---|---|
| what triggers it | you say "remember how to do this" | a turn ends |
| what it looks at | what you said | what **worked** in the conversation, and the facts you **gave** |
| who decides | you | the model proposes, the module judges by the rules above |
| what lands | you say save → approval | the same: proposal → you say keep → approval |

The look-back does not decide for you that something is worth keeping. It puts the
candidates in front of you, and holds itself to each one pointing at the words it
came from.

Code: `nanomuse/evolve.py` (the engine), `_look_back()` in `nanomuse/server/service.py`
(when it happens). Tests: `tests/test_evolve.py`.
