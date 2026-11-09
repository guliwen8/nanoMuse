# 健康中心与恢复动作

健康中心是运行时周边机器的一扇只读窗口，加上少量由 owner 事先声明的修复动作。这一页是
[configuration.md → `[health]`](configuration.md#health) 的运维版：检查什么、怎么声明恢复动作、
审批是怎么算的，以及上线前怎么验证。

这里没有任何东西会自己跑起来。读健康视图是一次普通 HTTP 调用；恢复动作是一次必须由哨兵放行的
工具调用，而且只在它要修的那个探针不健康时才运行。

## 接口会回答什么

`GET /api/health` 是给外部守护进程用的存活接口，因此不需要令牌也能访问。这个回答刻意保持最小：

```json
{"ok": true, "version": "1.0.9", "auth": true}
```

运行时快照、模型路由和基础设施检查都会说出这台机器和它上面的服务，因此需要令牌：

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

每一项检查只给出配置中的名称、类别、状态，以及在合适时的状态码和延迟。路径、URL、请求头、
Cookie 或保险库里的值都不会出现在回答里，所以这个接口可以放心地保持可读。不过探针名称是你自己
起的，所以详细回答仍按 owner 信息对待。

某个探针失败只会影响它自己：一个坏掉的接口是一条 `error` 记录，其余检查照常回答。

这个接口是给守护进程用的，所以它不会一直等一个远端接口。各个探针是一起跑的，不是一个接一个；
整个视图最多等两秒，到点还没回答的探针算一条 `unknown`：视图说它不知道，而不是假装这项检查通过了。
采到的视图会复用五秒，所以每三十秒问一次的守护进程，每轮只跑一次探针，而不是每问一次跑一次。

## 声明检查

先从成本最低、又能覆盖最多问题的检查开始：数据卷、保险库，以及上次备份的年龄。

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

备份检查只读文件或目录的修改时间。登录态检查读一个由现成的只读检查程序写出的 JSON 文件：

```json
{"ok": true, "checked_at": "2026-01-01T00:00:00+00:00", "expires_at": "2026-01-08T00:00:00+00:00"}
```

`ok: false`、`logged_in: false`、检查过期或 `expires_at` 已到期都会报不健康；令牌进入设定的到期
预警窗口时会报警告。nanoMuse 不会运行这个检查程序，不会打开浏览器、刷新 Cookie 或处理验证码。
登录失效、验证码和账号风控仍然交给人工。

HTTP 探针可以带凭据，而且凭据可以放在保险库里：

```toml
[[health.http]]
name = "creator API"
url = "https://api.example.com/session"
headers = { Authorization = "{{vault:CREATOR_API_TOKEN}}" }
expected_status = [200]
```

占位符只在这一次请求中解析，不会出现在回答、日志行或审计记录里。HTTP 探针不跟随重定向，因此
授权请求头不会被所检查的服务带到另一个主机；只有重定向本身代表健康时，才把对应的 3xx 状态写进
`expected_status`。

## 声明恢复动作

恢复动作把一个 PID、TCP 或 HTTP 检查绑定到一段确定的程序和参数。它要修的那个探针必须出现在同一份
配置里：

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

运行时会强制这些规则：

- `probe` 必须指向一个 PID、TCP 或 HTTP 检查。备份、登录态、磁盘和保险库不能被恢复，这些正是要
  让人来看的。
- `program` 必须是绝对路径，程序用参数数组启动，不经过 Shell。`program`、`args` 和 `cwd` 禁止
  `{{vault:NAME}}`，因此机密不会进入 argv（也就不会出现在 `ps` 里）。
- 名字表明要带凭据的参数（`--token`、`--api-key`、`--password` 这类）在明文带值时会被拒绝，
  这样机密既不会进进程列表，也不会进审计记录。请改用 `env` 传入。
- 机密只能通过 `env` 交给程序。名字看起来像凭据的环境变量（`KEY`、`TOKEN`、`SECRET`、`PASSW`、
  `CREDENTIAL`、`AUTH`、`COOKIE`、`SESSION`），除非值来自保险库，否则一律拒绝。
- `cooldown_s` 是同一个恢复动作再次运行前要等的时长（默认 60 秒，设为 `0` 关闭）。探针一直
  不健康时，恢复动作不能变成一串进程循环，每个进程最多活 `timeout_s`。
- 模型只看到恢复动作的名称：看不到程序、参数和环境变量。

先把机密放进保险库：

```bash
nanomuse vault set BROWSER_SERVICE_TOKEN     # 交互输入，值不会回显
```

## 审批是怎么算的

恢复动作属于敏感调用，默认的哨兵模式会先显示一张卡片。这张批准绑定的是恢复动作本身，而不是工具：

- 授权键绑定整份配置声明**以及程序文件的内容**。改程序、改参数、改环境、改探针，或者可执行文件
  的字节发生变化，长期授权立即失效，下次调用会重新询问。
- 指纹在程序启动前会再次核对，所以在批准与真正运行之间被换掉的文件不会启动。
- 这次批准只对那一次调用有效。没有经过哨兵触发的调用拿不到执行许可，什么都不会运行。
- 「本次对话内」的权限仍然绑定在这一个恢复动作上。它不会扩大到整个 `health` 工具，也不会覆盖
  owner 之后才声明的恢复动作。

所以长期授权不会变成「这台机器上的服务可以随便重启」的开关：每个不同的恢复动作，以及它每次的
变化，都是一个单独的决定。

审批卡片在「将要执行的内容」这一块显示程序和参数，人看到的是自己同意运行什么，而不只是一个
名称。环境变量不显示：里面带着密钥。

想在没有卡片的情况下放行，用一条明确的哨兵规则：

```toml
[sentinel]
rules = [
  { tool = "health", match = { action = "recover" }, action = "allow", reason = "owner-approved recoveries" }
]
```

这条规则要按它字面的意思理解：它允许任何已配置的恢复动作无人值守地运行。请把恢复动作的数量控制
在很少几个，程序本身也保持收窄。

## 上线前先试一遍

先带令牌看视图：

```bash
curl -s -H "Authorization: Bearer $NANOMUSE_SERVER_TOKEN" \
  http://127.0.0.1:8787/api/health | jq '.runtime.infrastructure'
```

再让智能体给出只读视图（它显示同样的检查，以及已配置的恢复动作名称）：

```
看看健康状态
```

要试恢复动作，就故意把服务停掉，让智能体修好它：

```
browser VNC 掉了，用恢复动作修一下
```

结果会给出恢复动作名称，以及恢复前后的状态。审计记录保留这次决定和脱敏后的前后状态：

```bash
jq -c 'select(.tool == "health")' ~/.nanomuse/audit.jsonl | tail -5
```

没修好的恢复动作会直说：结果里是 `recovery finished but the probe is still unhealthy`，下一次读取
时那个探针仍然是坏的。

## 容器里的几点注意

- 容器内的健康检查不带令牌询问 `/api/health`，拿到的是那个小的存活回答，正好是它需要的。
- 管理容器外服务的恢复动作（systemd、裸进程），需要在容器内放入对应程序和它需要的权限。运行时
  不会自己加权限：`program` 以容器的用户身份运行，能力也按镜像里丢弃后的算。相比需要 root 的
  程序，更推荐一个能和宿主服务管理器对话的程序。
- PID、TCP 和 HTTP 检查都在容器内执行。`127.0.0.1` 探针看到的是容器自己的回环，所以要填宿主
  地址，或者干脆把那个探针放在宿主机上跑。

## 这里不做什么

- 不登录任何地方，不刷新 Cookie，不处理验证码，不清除账号风控。这些都交给人。
- 不备份也不恢复任何东西。备份检查只报告上次备份有多旧。
- 不修磁盘，也不修保险库。
- 不跑 Shell。恢复动作就是一个程序加固定的参数。
