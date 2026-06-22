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
python bbagent_mcp.py --selftest   # offline smoke test of every tool
claude                             # start Claude Code here, then ask:
#   "Use bbagent to manage my team to win this week."
```

Claude Code calls `full_plan`, `check_il`, `optimize_lineup`, `scout_trades`,
`review_declined_trades`, etc. and proposes a batch. Agent guidance lives in
`CLAUDE.md`. Writes stay dry-run until `BBAGENT_COMMIT_WRITES=1` (in `.mcp.json`
`env`).

### c) Anthropic API LLM loop

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python espn_agent.py run "Manage my team to win this week."
```

Same tools, driven by an API model instead of Claude Code. Costs credits.

---

## 3. Run it on a schedule (host on a server)

Fantasy management is a daily job, so the robust pattern is a **scheduled run**,
not a long-lived daemon. Both methods below assume Setup (§1) is done on the host
and you've completed the write verification (§4) before using `--commit`.

### a) Deterministic — recommended for unattended runs

This is the most reliable to automate: one deterministic command, no model in the
loop. It logs to `actions.log` and emails the report each run.

**Linux/macOS (cron)** — daily at 9:00am:

```cron
0 9 * * *  cd /path/to/bbagent && /usr/bin/python3 espn_agent.py plan --execute --commit >> cron.log 2>&1
```

**Windows (Task Scheduler)** — daily at 9:00am:

```powershell
schtasks /create /tn bbagent-daily /sc daily /st 09:00 ^
  /tr "cmd /c cd /d C:\path\to\bbagent && python espn_agent.py plan --execute --commit"
```

**Docker / any host** — loop in a tiny shell wrapper (`run-loop.sh`):

```bash
#!/usr/bin/env bash
cd "$(dirname "$0")"
while true; do
  python espn_agent.py plan --execute --commit >> loop.log 2>&1
  sleep 86400          # once a day
done
```

Drop `--commit` (or `--execute`) to email yourself recommendations without
touching the roster. Secrets come from `.env` on the host — never bake them into
the image; mount `.env` or inject `BBAGENT_*` env vars.

### b) Claude Code (headless) — LLM-driven on your subscription

Claude Code runs non-interactively with `-p`, so you can schedule it too. It
drives the same MCP server, on your Pro/Max subscription (no API key). The host
must have `claude` installed and logged in (`claude` once, interactively, to
authenticate).

```bash
claude -p "Use bbagent to run a full management pass: optimize the lineup, work
the waivers, and send any fair trade offers. Then summarize what you did." \
  --mcp-config .mcp.json \
  --permission-mode acceptEdits
```

Schedule that line via cron / Task Scheduler exactly as in (a). For it to submit
moves, set `BBAGENT_COMMIT_WRITES=1` in `.mcp.json`'s `env` block. Because a
model is in the loop, prefer this when you want judgment on close calls; prefer
(a) when you want determinism and the lowest chance of surprise.

> Headless Claude Code needs permissions pre-granted (`--permission-mode`) since
> no human is there to approve tool calls. Scope it to this project directory.

### c) Anthropic API loop on a schedule

```bash
ANTHROPIC_API_KEY=sk-ant-... python espn_agent.py run \
  "Run a full management pass and act on it." >> loop.log 2>&1
```

Same scheduling; this one bills API credits per run.

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
