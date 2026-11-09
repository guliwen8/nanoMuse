# Supply chain governance: a plan

This is a plan, not an implementation. It records what the Skill and MCP supply chains do
today, what they do not do, and the order in which the gaps are worth closing. All five
steps of the order below have landed; what is left is the standing gap, named at the end.

The rule the whole plan follows: **a capability that arrives from outside is a capability the
owner has to be able to see, pin and revoke.** The Sentinel already holds the line once a tool
is running; this is about the two moments before that, where a skill or an MCP server becomes
part of the agent's standing behaviour.

## What arrives from outside

Two things can plant a standing instruction in the agent:

- a **skill** (`nanomuse/skills/`): a folder with a `SKILL.md` and optional `scripts/`,
  `references/` and `assets/` directories. Its text is a standing instruction for every later
  session; its scripts are runnable code.
- an **MCP server** (`[[mcp.servers]]`): a process or an endpoint that offers tools. Its tool
  descriptions are injected into the model's context and its outputs come back as tool
  results.

Both are powerful by design. Both are also, today, mostly trusted on the word "the owner put
it there".

## Where the chains stand today

Facts, each checked in the code:

| What | Today | Where |
|---|---|---|
| Skill source | a GitHub URL or a local path | `nanomuse/skills/installer.py` (`github_source`, `install_from_url`) |
| Skill download limit | 64 MB archive, 200 files, 64 KB `SKILL.md`, 2 MB per file | `installer.py:41-48` |
| Skill extraction | only the wanted folder, only `SKILL.md` and the three resource directories, no dotfiles | `installer.py:135-180` |
| Skill content check | **now a digest**: the SHA-256 of the installed content, kept next to the skill; no signature | `installer.py` (`SkillBundle.digest`) |
| Skill source recorded | **now yes**: `source.json` in the installed folder holds the source, a SHA-256 of the installed content, the file list and the install time | `installer.py` (`write_source`, `read_source`, `SOURCE_FILE`) |
| Skill digest shown | **now yes**: `skills list` shows the source and the digest prefix | `nanomuse/cli.py` (`skills_list`) |
| Skill install risk | `SENSITIVE`, with a warning when it replaces a skill of the same name | `nanomuse/tools/skills_tool.py:70-86` |
| Skill install audit | **now yes**: the install event carries the source, the digest and the file list | `nanomuse/tools/skills_tool.py`, `nanomuse/server/service.py` |
| MCP server config | `command`/`args`/`env` or `url`, plus `risk`, `egress`, `reads_private_data` and per-tool overrides | `nanomuse/config.py:598-643` |
| MCP process start | stdio via the MCP client, with the library's default environment plus the configured `env` | `nanomuse/tools/mcp_tools.py:336-346` |
| MCP tool naming | `server__tool`, sanitised and cut at 64 characters; a collision skips the whole server | `mcp_tools.py:48-51`, `mcp_tools.py:252-270` |
| MCP tool risk | the server's `risk`, overridden per tool, further informed by the MCP `readOnlyHint` annotations | `mcp_tools.py:65-105` |
| MCP tool description | comes from the server, cut at 1000 characters, **and marked as data when it reaches the model** | `mcp_tools.py:284` |
| MCP `instructions` | the owner's own text for that server, put into the system prompt, bounded at 4000 characters | `config.py:617-621`, `nanomuse/agent/core.py:195-210` |
| MCP executable | **now resolved**: a `command` that is not absolute is looked up on `PATH`, and the program that ran is recorded with its SHA-256; `pin` refuses a program whose content changed | `mcp_tools.py` (`_program`) |
| MCP install audit | the standard tool-call audit entry, with the arguments | `nanomuse/sentinel/audit.py` |

## The gaps, in the order they matter

### 1. Nothing pins what was fetched

A skill is installed from a URL and the bytes are not recorded. The same URL tomorrow serves
different content: the branch moved, the tag was moved, the account was taken over. There is
nothing to compare against, so a changed skill is indistinguishable from the same skill, and
a rollback is "install the old one again and hope".

**Fix.** Record the source and a SHA-256 of the installed content next to the skill (a
`source.json` inside the folder, or a line in the library's own index). Show the digest in
`skills list` and in the app. When the folder on disk no longer matches the recorded digest,
say so.

**Cost.** Small. **Value.** Makes "is the skill I installed still the skill I installed" a
question with an answer.

### 2. The MCP server's own text reaches the model's context

A tool's `description` comes from the server and is injected into the model's context
(`mcp_tools.py:284`, cut at 1000 characters). The server decides what the model is told
about the tool it offers, and it can say anything in that field. The `instructions` field is
different: it is written in the owner's own configuration and put into the system prompt
(`nanomuse/agent/core.py:195-210`), so it is the owner's text, not the server's.

**Fix.** The tool description is the part to fix: mark it as data the server supplied, so a
description that reads like an instruction is not followed as one. The `instructions` field
needs only a length bound.

**Cost.** Small to medium (the prompt format changes). **Value.** Closes the path from
"server connected" to "server decides what the agent does".

### 3. An MCP server runs as a process with no pin at all

A `command` is run as given. The environment is the library's default plus the configured
`env`, which is a reasonable floor, but the binary is whatever is on `PATH` today. An
`npx`-style server is a moving target: the version that runs is whatever the registry serves.

**Fix.** Two steps. First, prefer an absolute path and refuse a bare command that is not
absolute, the way the health recovery's `program` already does. Second, for a server that is
meant to be pinned, record the resolved executable's digest at install time and check it when
the server starts.

**Cost.** Medium. **Value.** Turns "the server I configured" into "the server that is
running".

### 4. Neither chain can be revoked as a group

There is a revoke for a Sentinel grant and a disable for a skill. There is no list of what is
currently installed and able to act, so "what can this agent do that I did not write myself"
has no single answer, and an incident cannot be closed by answering "turn it off".

**Fix.** One page: the installed skills with their sources and digests, the connected MCP
servers with their risk declarations and the tools each one contributes, and the standing
permissions. It is the same inventory the Permissions page already has, extended to what
arrived from outside.

**Cost.** Medium. **Value.** The inventory is what every other control in this plan is
checked against.

### 5. The audit trail records the call, not the arrival

An installation is an ordinary sensitive tool call, so the decision and the arguments are in
the audit log. What is not there is the content that was fetched: the digest, the file list,
the resolved server. An incident review can see that a skill was installed and cannot see
what was installed.

**Fix.** Record the digest and the file list on the install event, and the server's tool list
on the connect event. Both are short.

**Cost.** Small. **Value.** Makes the audit trail answer "what arrived", not just "what ran".

## The order

1. **Record the source and the digest** for skills, and show them. This is the smallest change
   with the clearest payoff, and everything else can be built on it. **Done.**
2. **Audit the arrivals**: the install event carries the digest and the file list. **Done.**
3. **The inventory**: one place that lists skills, servers and standing permissions. **Done.**
4. **Bound the server's voice**: mark the server-supplied tool description as data, and cap
   the length of the owner's `instructions`. **Done.**
5. **Pin the executable**: absolute paths for MCP commands, a digest for the ones meant to be
   pinned. **Done.**

Steps 1 and 2 are one afternoon. Step 3 is a page. Steps 4 and 5 are the ones that need
care, because they change what the model sees and how servers start.

## What has landed

All five steps are implemented on this branch:

- **Step 1.** `SkillBundle.digest()` hashes the installed content, and `write_source()` puts
  the source, the digest, the file list and the install time into `source.json` inside the
  installed folder. The file is excluded from the installer's own cleanup so a re-install
  does not delete it. `Skill` in `nanomuse/skills/library.py` reads that record back, and
  `skills list` shows the source and the digest prefix.
- **Step 2.** The install event in the audit log carries the source, the digest and the file
  list, both for the `Skills` tool and for the server's install path.
- **Step 3.** `GET /api/inventory` and `nanomuse inventory` list the skills with their
  sources and digests, the MCP servers with the program each one runs and the tools it
  contributes, and the standing permissions. The app shows the same list under
  *Settings → What can act*. Nothing in it is a secret: a source, a digest, a risk and a
  scope.
- **Step 4.** The copy of a tool's description that reaches the model is marked as
  server-supplied data; the app still shows the description as the server wrote it. The
  owner's own `instructions` are bounded at 4000 characters, and the prompt says whose
  words they are.
- **Step 5.** A `command` that is not an absolute path is looked up on `PATH`, and the
  program that actually ran is recorded with its SHA-256. A server may be pinned with
  `pin = "<sha256>"`: the program is then started only when the file on disk still has
  that content.

## The gap that is left

A digest says what arrived and what runs. It does not say what is trusted: the plan does
not try to decide which skills and servers are trustworthy. That is the owner's judgement,
and the inventory only makes the judgement possible to make.

Two things are still open, and neither is a gap in the plan:

- A skill's digest covers the folder's content, but a skill that runs a script reaches
  the network on its own terms. The Sentinel governs the tools; it does not govern what a
  skill's own script does once it is running.
- A pinned MCP server is pinned to the program on disk. A server that reaches out over
  the network is still trusting whatever the other end answers.

## What this plan does not do

- It does not try to decide which skills and servers are trustworthy. That is the owner's
  judgement, and the plan only makes the judgement possible to make.
- It does not sandbox a server's process. That is a much larger piece of work, and the MCP
  client's stdio transport is a reasonable boundary for now.
- It does not replace the Sentinel. The Sentinel governs what a tool may do; this plan governs
  what may become a tool in the first place.
