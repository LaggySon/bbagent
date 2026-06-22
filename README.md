# bbagent — autonomous ESPN Fantasy Baseball manager

A competitive agent that owns and operates **one** team in an ESPN H2H points
league: lineup + IL optimization, waiver add/drop, and trades (proposing fair
offers out, reacting to declines, evaluating incoming). It scouts with live
**MLB StatsAPI** data blended with ESPN's projections. Every move it decides is
appended to `actions.log`, and it can email a readable report each run.

It runs two ways — the analysis is identical, only *who drives the loop* differs:

| | **Deterministic / API** | **Claude Code (MCP)** |
|---|---|---|
| Driver | built-in logic, or an Anthropic-API LLM loop | Claude Code, on your Pro/Max subscription |
| Cost | free (`plan`) / API credits (`run`) | your subscription — no API key |
| Entry point | `python espn_agent.py …` | `bbagent_mcp.py` over MCP |

---

## 1. Setup (once)

```bash
pip install -r requirements.txt     # requests; mcp[cli] for Claude Code; anthropic for `run`
cp .env.example .env                # then edit .env — see below
```

Put your **secrets + league identity** in `.env` (gitignored):

```
BBAGENT_ESPN_S2=<long cookie>
BBAGENT_SWID={<cookie-with-braces>}
BBAGENT_LEAGUE_ID=<number in your league URL>
# optional: BBAGENT_AGENT_TEAM_ID=<id>   (otherwise auto-resolved from SWID)
```

Get `ESPN_S2` and `SWID` from a logged-in browser: DevTools → Application →
Cookies on `fantasy.espn.com` (`SWID` includes the curly braces).

**Non-secret tuning** (scouting weight, trade thresholds, slot ids, email host)
lives in `bbagent.config.json`, which is safe to commit. Config resolves
highest-priority first:

```
BBAGENT_<KEY> env var  →  bbagent.local.json  →  bbagent.config.json  →  default
```

Verify what loaded (secrets masked):

```bash
python espn_agent.py config        # prints every resolved value + source
python espn_agent.py selftest      # offline, no network — proves the logic
python espn_agent.py discover      # lists teams; marks your auto-matched team *
```

---

## 2. Run it once, manually

### a) Deterministic (free, no API key, no Claude)

```bash
python espn_agent.py plan          # print-only: IL, lineup, waivers, fair trades
```

`plan` is **read-only by default** — it analyzes, logs every recommendation to
`actions.log`, emails the report if email is configured, and submits **nothing**.
To actually act, add flags (see [§4 enabling writes](#4-enabling-real-writes)):

```bash
python espn_agent.py plan --execute            # simulate the moves (dry-run payloads)
python espn_agent.py plan --execute --commit   # really submit moves + trade offers
```

Useful one-off flags: `--no-scouting`, `--scouting-weight 0.7`, `--max-trades 2`,
`--email` / `--no-email`.

### b) Claude Code (no API key — uses your subscription)

The same tools are exposed over MCP; **Claude Code is the agent loop**. From this
directory (it ships a `.mcp.json`, so the `bbagent` server auto-registers —
approve it once):

```bash
pip install "mcp[cli]"             # required for the server (the --selftest below skips it)
python bbagent_mcp.py --selftest   # offline smoke test of every tool
claude                             # start Claude Code here — see approval note below
#   then ask: "Use bbagent to manage my team to win this week."
```

> **First run: approve the server.** A project-scoped server from `.mcp.json`
> stays `⏸ Pending approval` until you approve it once in an interactive
> `claude` session (it prompts on launch; `/mcp` shows status). Until then its
> tools don't load and Claude will say it can't find them. `claude mcp list`
> should read `bbagent: ... ✓ Connected` once it's good. (`--selftest` passes
> even when the `mcp` package is missing — it uses fixtures — so a green
> selftest does *not* prove the live server will start; the `pip install` above
> is what does.)

Claude Code calls `full_plan`, `check_il`, `optimize_lineup`, `scout_trades`,
`review_declined_trades`, etc. and proposes a batch. Agent guidance lives in
`CLAUDE.md`. Writes stay dry-run until `BBAGENT_COMMIT_WRITES=1` (in `.mcp.json`
`env`).

It **emails you the same report** the deterministic version does: the
`email_report` tool renders through the identical plain-text + HTML template, so
a recipient can't tell an agent-driven pass from a deterministic one — except for
a single `Run by:` line (`Claude Code agent` vs. `deterministic engine`).
`CLAUDE.md` tells Claude to call it at the end of every pass, populated with what
it actually decided. (`full_plan` also auto-emails its dry-run plan when SMTP is
set.) SMTP config: host in `bbagent.config.json`, `SMTP_PASS` in `.env` — see §1.
Without SMTP configured it no-ops with a clear "not configured" message.

### c) Anthropic API LLM loop

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python espn_agent.py run "Manage my team to win this week."
```

Same tools, driven by an API model instead of Claude Code. Costs credits.

---

## 3. Run it on a schedule (host on a server)

Fantasy management is a daily job, so the robust pattern is a **scheduled run**,
not a long-lived daemon. Pick the run method (3.1–3.3) — that decides the
*command*. Then schedule that command with your platform's scheduler (3.4). All
of this assumes Setup (§1) is done on the host and, for live writes, that you've
completed the write verification (§4).

In the scheduler examples below, `RUN_CMD` stands for whichever command you
chose. Replace `/path/to/bbagent` (or `C:\path\to\bbagent`) with the real path,
and `python` with `python3` if that's your interpreter name.

### 3.1 Deterministic — recommended for unattended runs

Most reliable to automate: one deterministic command, no model in the loop. Logs
to `actions.log` and emails the report each run.

```
RUN_CMD = python espn_agent.py plan --execute --commit
```

Drop `--commit` (or `--execute`) to email yourself recommendations without
touching the roster.

### 3.2 Claude Code (headless) — LLM-driven on your subscription

Drives the same MCP server on your Pro/Max subscription (no API key). The host
must have `claude` installed and authenticated — either log in interactively once
(`claude`), or set a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` for a
headless host (same token the container uses, see §3.6). For it to submit moves,
set `BBAGENT_COMMIT_WRITES=1` in `.mcp.json`'s `env` block.

```
RUN_CMD = claude -p "Use bbagent to run a full management pass: optimize the lineup, work the waivers, and send any fair trade offers. Then summarize what you did." --mcp-config .mcp.json --allowedTools "mcp__bbagent__*"
```

Two things make this work headlessly, and both bite silently if missed:

- **Approve the server once, interactively, first.** A `.mcp.json` server is
  `⏸ Pending approval` until approved in a real `claude` session — and headless
  `-p` can't show that prompt, so the tools never load and the run does nothing
  useful. Run `claude` once on the host and approve `bbagent` (verify with
  `claude mcp list` → `✓ Connected`) *before* scheduling.
- **Pre-grant the tool calls.** `--permission-mode acceptEdits` only
  auto-accepts file edits, **not** tool/command calls — a headless run under it
  stalls waiting for approval. Use `--allowedTools "mcp__bbagent__*"` (allow just
  the bbagent tools, as above) or `--dangerously-skip-permissions` (allow
  everything).

Prefer this mode when you want model judgment on close calls; prefer 3.1 for pure
determinism.

### 3.3 Anthropic API loop

Same tools, driven by an API model; bills credits per run. Needs
`ANTHROPIC_API_KEY` in the environment (put it in `.env`).

```
RUN_CMD = python espn_agent.py run "Run a full management pass and act on it."
```

### 3.4 Schedule `RUN_CMD` on your platform

Each example runs daily at 9:00 AM. Secrets come from `.env` on the host (or
`BBAGENT_*` env vars) — never hard-code them in the scheduler entry.

**macOS — launchd** (per-user, survives reboots). Save as
`~/Library/LaunchAgents/sh.laggi.bbagent.plist`, then
`launchctl load ~/Library/LaunchAgents/sh.laggi.bbagent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>sh.laggi.bbagent</string>
  <key>WorkingDirectory</key><string>/path/to/bbagent</string>
  <key>ProgramArguments</key>
    <array>
      <string>/bin/sh</string><string>-lc</string>
      <string>python3 espn_agent.py plan --execute --commit</string>
    </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/path/to/bbagent/cron.log</string>
  <key>StandardErrorPath</key><string>/path/to/bbagent/cron.log</string>
</dict></plist>
```

(macOS also has cron; the Linux line below works there too.)

**Linux — cron.** `crontab -e`, then add:

```cron
0 9 * * *  cd /path/to/bbagent && python3 espn_agent.py plan --execute --commit >> cron.log 2>&1
```

For Claude Code / API on Linux, cron drops most env vars — source `.env` first:

```cron
0 9 * * *  cd /path/to/bbagent && set -a && . ./.env && set +a && RUN_CMD >> cron.log 2>&1
```

**Windows — Task Scheduler.** In PowerShell (one line):

```powershell
schtasks /create /tn bbagent-daily /sc daily /st 09:00 /tr "cmd /c cd /d C:\path\to\bbagent && python espn_agent.py plan --execute --commit >> cron.log 2>&1"
```

Swap the `python ...` part for your chosen `RUN_CMD`. Run `schtasks /run /tn
bbagent-daily` once to test it immediately.

### 3.5 Container watcher (continuous, any platform)

Platform-agnostic and the easiest to host: one always-on container that *watches*
the league on a short interval and runs a full pass every tick — no OS scheduler
to wire up. A watcher (not a once-a-day batch) is the right model here because
lineups, injuries, incoming/declined offers, and waiver windows all move on their
own clock; polling every few minutes catches them as they happen. The repo ships
a `Dockerfile`, a `.dockerignore`, and `run-loop.sh` (poll → full pass → sleep
`INTERVAL` → repeat). Build and run:

```bash
docker build -t bbagent .
docker run -d --name bbagent --restart unless-stopped --env-file .env bbagent
```

`--env-file .env` injects your secrets at run time; `.dockerignore` keeps `.env`
out of the image so they're never baked in. `--restart unless-stopped` is what
makes it "always running" across reboots and crashes.

**Running a full pass every tick is safe to repeat:** `plan` reads ESPN's pending
transactions and won't re-offer players already tied up in a pending trade,
declined offers are routed to counter-or-abandon (never blindly re-sent), and
waiver claims are bounded by `max_waivers` (default 1/pass). So a 10-minute poll
reacts fast without spamming offers or churning the roster.

Override the command or poll cadence with env vars (defaults: the deterministic
`plan --execute --commit`, every 600s = 10 min):

```bash
docker run -d --env-file .env \
  -e RUN_CMD="python espn_agent.py plan" \   # watch + recommend only, no writes
  -e INTERVAL=300 \                           # poll every 5 min
  bbagent
```

Logs go to `loop.log` inside the container (`docker logs bbagent` also works).
All three run modes work in the container — the image bundles the `claude` CLI,
so the **Claude Code (3.2)** path runs in-container too (see §3.6 for headless
auth).

### 3.6 Claude Code in the container (headless auth)

The image ships Node + the `claude` CLI, so you can drive the Claude Code run
mode without a host install. The only catch is auth: there's no interactive
browser login in a container, so inject a token at run time instead. Two options:

- **Subscription (Pro/Max, no API credits).** On a machine where you're logged
  in, run `claude setup-token` once and copy the printed token. Pass it as
  `CLAUDE_CODE_OAUTH_TOKEN`.
- **API key.** Pass `ANTHROPIC_API_KEY` (bills credits).

Put whichever you use in `.env` (it's gitignored and injected with `--env-file`),
then point `RUN_CMD` at `claude`:

```bash
docker run -d --name bbagent --restart unless-stopped --env-file .env \
  -e RUN_CMD="claude -p 'Use bbagent to run a full management pass: optimize the lineup, work the waivers, and send any fair trade offers. Then summarize what you did.' --mcp-config .mcp.json --dangerously-skip-permissions" \
  bbagent
```

Two gotchas, both unique to running headless **in a container** — neither has an
interactive escape hatch, so get them right up front:

- **No approval prompt exists.** A `.mcp.json` server normally needs a one-time
  interactive approval, which a container can't show — so `--mcp-config .mcp.json`
  alone leaves the tools `⏸ Pending` forever and the pass does nothing.
  `--dangerously-skip-permissions` bypasses both the trust approval and the
  per-call tool prompts, which is what makes the server load unattended. (On a
  host you'd instead approve once with `claude` and use the narrower
  `--allowedTools "mcp__bbagent__*"`; see §3.2.)
- **The image must have the `mcp` package.** It's in `requirements.txt`, so the
  image's `pip install` covers it — just don't strip it out. Without it the
  server fails to start even though `--selftest` (fixture-based) still passes.

Writes still stay dry-run until `BBAGENT_COMMIT_WRITES=1` in `.mcp.json`'s `env`
block. The watcher reruns this every `INTERVAL` like any other `RUN_CMD`.

---

## 4. Enabling real writes (one-time verification)

Writes are gated behind the `COMMIT_WRITES` switch (`--commit`, or
`BBAGENT_COMMIT_WRITES=1`) and use ESPN's v3 `transactions` endpoint. The payload
shapes (lineup ROSTER moves, ADD/DROP, TRADE) follow ESPN's protocol, but **the
write API is unofficial** — verify once before trusting it:

1. Log in, open DevTools → Network (Fetch/XHR).
2. Make a real, harmless lineup move in the UI; submit.
3. Compare that POST's URL + JSON body to the `would_post` payload every dry-run
   prints (`plan --execute` without `--commit`).
4. If they match, run with `--commit`. If ESPN tweaked the shape, adjust
   `EspnClient._post_txn` / the item builders to match.

Until then, **nothing is ever POSTed** — you only see the payloads.

> Note: writes succeed only for a team your `SWID` owns or co-manages. Pointing
> `AGENT_TEAM_ID` at another team is fine for analysis/email, but ESPN will
> reject write attempts to a roster you don't control.

---

## 5. Scouting & projections

By default `plan` blends live **MLB StatsAPI** rest-of-season projections with
ESPN's own (`scouting_weight` = the external share). Tune or disable via config
(`use_scouting`, `scouting_weight`) or flags (`--no-scouting`,
`--scouting-weight`). You can also feed custom projection CSVs:

```python
from espn_agent import EspnClient, plug_projections
import projections as proj
client = EspnClient()
overrides, unmatched = proj.build_overrides(
    client, hitters_csv="hitters.csv", pitchers_csv="pitchers.csv")
plug_projections(overrides)
```

---

## Files

- `espn_agent.py` — ESPN client, deterministic core, CLI, API loop, email.
- `bbagent_mcp.py` — MCP server exposing the toolset to Claude Code (no API key).
- `scouting.py` — MLB-StatsAPI outside source → league-scored ROS values.
- `projections.py` — external-CSV → league-scored value pipeline.
- `bbagent.config.json` — non-secret tuning knobs (committed).
- `.env` — secrets + league identity (gitignored; see `.env.example`).
- `.mcp.json` — Claude Code server config (auto-discovered in this directory).
- `CLAUDE.md` — operating instructions Claude Code loads automatically.
- `actions.log` — append-only audit of every move (gitignored).
- `Dockerfile` — image with Python deps + Node/`claude` CLI; runs all three run
  modes (§3.5–3.6).
- `run-loop.sh` — the container watcher loop (poll → full pass → sleep
  `INTERVAL`); the image's default `CMD`, also runnable standalone.
- `.dockerignore` — keeps secrets (`.env`), logs, and local cruft out of the image.
- `loop.log` — the container watcher's output (gitignored; `docker logs` too).
