# Health centre and recovery

The health centre is a read-only window into the machine around the runtime, plus a small
number of repairs the owner declares in advance. This page is the operational version of
[configuration.md → `[health]`](configuration.md#health): what to check, how to declare a
recovery, how the approval works, and how to verify a change before it goes live.

Nothing here runs by itself. Reading the health view is a plain HTTP call; a recovery is a
tool call that the Sentinel has to allow, and it only runs while the probe it repairs is
unhealthy.

## What the endpoint answers

`GET /api/health` is a liveness endpoint, so it answers without a token. That answer is
deliberately small:

```json
{"ok": true, "version": "1.0.9", "auth": true}
```

The runtime snapshot, the model routes and the infrastructure checks describe the machine
and the services on it, so they need the token:

```bash
curl -s -H "Authorization: Bearer $NANOMUSE_SERVER_TOKEN" http://127.0.0.1:8787/api/health
```

```json
{
  "ok": true,
  "version": "1.0.9",
  "auth": true,
  "runtime": {
    "llm": {"active": "primary", "routes": [...]},
    "scheduled_deliveries": {"pending": 0, "running": 0, "done": 0, "next_attempt_at": null},
    "infrastructure": {
      "state": "ok",
      "checks": [
        {"name": "disk", "kind": "disk", "state": "ok", "free_mb": 51234, "free_percent": 41.2},
        {"name": "vault", "kind": "vault", "state": "ok", "configured": true, "entries": 7},
        {"name": "NAS backup", "kind": "backup", "state": "ok", "age_seconds": 3600, "max_age_seconds": 172800}
      ]
    }
  }
}
```

Every check answers with its configured name, kind, state and, where it makes sense, a
status and a latency. A path, a URL, a header, a cookie or a vault value never appears in
the answer, so the endpoint is safe to leave readable. The probe names are yours, though, so
treat the detailed answer as owner information.

A failing probe degrades on its own: one broken endpoint is one `error` entry and the other
checks still answer.

The endpoint answers a supervisor, so it never waits on a remote endpoint. The probes run
together rather than one after the other, the whole view is bounded at two seconds, and a
probe that has not answered by then is one `unknown` entry: the view says it does not know
rather than pretending the check passed. A collected view is reused for five seconds, so a
supervisor polling every thirty seconds costs one round of probes, not one per poll.

## Declaring the checks

Start with the checks that cost nothing and catch the most: the data volume, the vault, and
the age of the last backup.

```toml
[health]
enabled = true
min_free_mb = 1024
min_free_percent = 5

[[health.backups]]
name = "NAS backup"
path = "/srv/nanomuse-backups/latest.done"
max_age_hours = 48

[[health.pid_files]]
name = "websockify"
path = "/run/websockify.pid"

[[health.tcp]]
name = "browser VNC"
host = "127.0.0.1"
port = 5900
timeout_s = 3

[[health.http]]
name = "relay"
url = "https://relay.example/healthz"
expected_status = [200, 204]
timeout_s = 5
```

A backup check reads the modification time of a file or directory. A login-state check reads
a JSON file that an existing, read-only checker writes:

```json
{"ok": true, "checked_at": "2026-01-01T00:00:00+00:00", "expires_at": "2026-01-08T00:00:00+00:00"}
```

`ok: false`, `logged_in: false`, a stale check or a past `expires_at` is unhealthy; a token
inside its warning window is a warning. nanoMuse never runs that checker, never opens a
browser, never refreshes a cookie and never solves a challenge. A login failure, a
challenge or an account risk control stays a human task.

An HTTP probe may carry a credential, and the credential may live in the vault:

```toml
[[health.http]]
name = "creator API"
url = "https://api.example.com/session"
headers = { Authorization = "{{vault:CREATOR_API_TOKEN}}" }
expected_status = [200]
```

The placeholder is resolved for that one request and never appears in the answer, the log
line or the audit record. HTTP probes do not follow redirects, so an authorization header
cannot travel to a host the checked service picks; list a redirect status in
`expected_status` only when the redirect itself means healthy.

## Declaring a recovery

A recovery binds one PID, TCP or HTTP check to an exact program and argument array. The probe
it repairs has to exist in the same configuration:

```toml
[[health.tcp]]
name = "browser VNC"
host = "127.0.0.1"
port = 5900
timeout_s = 3

[[health.recoveries]]
name = "restart browser service"
probe = "browser VNC"
program = "/usr/bin/systemctl"
args = ["restart", "nanomuse-browser.service"]
timeout_s = 30
verify_after_s = 3
cooldown_s = 60
egress = false
env = { SERVICE_TOKEN = "{{vault:BROWSER_SERVICE_TOKEN}}" }
```

The rules the runtime enforces:

- `probe` must name a PID, TCP or HTTP check. A backup, login-state, disk or vault check
  cannot be recovered; those are the ones a person has to look at.
- `program` must be an absolute path, and the program is started with an argument array and
  no shell. `program`, `args` and `cwd` reject `{{vault:NAME}}`, so a secret can never
  reach argv (and `ps`).
- An argument whose name says it carries a credential (`--token`, `--api-key`, `--password`
  and the like) is refused when it carries a value in the clear, so a secret does not end
  up in the process list or in the audit record. Pass it through `env` instead.
- A secret reaches the program through `env` only. An environment name that looks like a
  credential (`KEY`, `TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL`, `AUTH`, `COOKIE`, `SESSION`)
  is rejected unless its value comes from the vault.
- `cooldown_s` is how long to wait before the same recovery may run again (60 seconds by
  default, `0` turns it off). A probe that stays unhealthy must not turn the recovery into a
  loop of processes, each living for up to `timeout_s`.
- The model sees the recovery's name and nothing else: not the program, not the arguments,
  not the environment.

Put the secret in the vault first:

```bash
nanomuse vault set BROWSER_SERVICE_TOKEN     # prompts; the value is never echoed back
```

## What the approval means

A recovery is a sensitive call, so the default Sentinel mode shows a card before it runs.
The approval is bound to the recovery, not to the tool:

- The grant is keyed on a fingerprint of the whole declaration **and the content of the
  program file**. Editing the program, its arguments, the environment, the probe, or the
  bytes of the executable invalidates the standing approval, and the next call asks again.
- The fingerprint is checked again immediately before the program starts, so a file
  replaced between the approval and the run does not start.
- The approval is good for that one call. A call that never went through the Sentinel has
  no permit and runs nothing.
- A "for this conversation" permission stays bound to that one recovery. It does not widen
  to the whole `health` tool, and it does not cover a recovery the owner declared later.

So a standing permission cannot become a general "this machine may restart things" switch:
each distinct recovery, and each change to it, is a separate decision.

The card shows the program and the arguments under "Exactly what will run", so the person
sees what they are agreeing to rather than a name. The environment is not shown: it carries
the secrets.

To settle a recovery without a card, use an explicit Sentinel rule:

```toml
[sentinel]
rules = [
  { tool = "health", match = { action = "recover" }, action = "allow", reason = "owner-approved recoveries" }
]
```

Read that rule as what it is: it lets any configured recovery run unattended. Keep the
list of recoveries short and keep the programs narrow.

## Trying it before it goes live

Check the view first, with the token:

```bash
curl -s -H "Authorization: Bearer $NANOMUSE_SERVER_TOKEN" \
  http://127.0.0.1:8787/api/health | jq '.runtime.infrastructure'
```

Then ask the agent for the read-only view (it shows the same checks and the names of the
configured recoveries):

```
看看健康状态
```

To try a recovery, stop the service on purpose and let the agent repair it:

```
browser VNC 掉了，用恢复动作修一下
```

The result names the recovery and shows the before and after states. The audit record keeps
the decision and the redacted states:

```bash
jq -c 'select(.tool == "health")' ~/.nanomuse/audit.jsonl | tail -5
```

A recovery that does not fix the probe says so plainly: the result carries
`recovery finished but the probe is still unhealthy`, and the probe is still failing in the
next read.

## Notes for a container

- The health check inside the container asks `/api/health` without a token and gets the
  small liveness answer, which is what it needs.
- A recovery that manages a service outside the container (systemd, a bare process) needs
  that program inside the container and the privileges it needs. The runtime does not add
  any: `program` runs as the container's user, with the capabilities the image dropped.
  Prefer a program that talks to the host's service manager over one that needs root.
- The PID, TCP and HTTP checks run from inside the container. A `127.0.0.1` probe sees the
  container's own loopback, so check the host's address, or run that probe on the host
  instead.

## What this does not do

- It does not log in anywhere, refresh a cookie, solve a challenge or clear an account
  risk control. Those stay with a person.
- It does not back up or restore anything. The backup check only reports how old the last
  backup is.
- It does not repair the disk or the vault.
- It does not run a shell. A recovery is one program with fixed arguments.
