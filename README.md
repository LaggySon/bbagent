# bbagent — ESPN Fantasy Baseball agent

A competitive agent that owns and operates **one** team in an ESPN H2H points
league: lineup + IL optimization, waiver add/drop, and trades (proposing out,
reacting to declines, evaluating incoming). No shared control with any other
roster. Every proposed or committed action is appended to `actions.log` with its
reasoning, so as commissioner you can show the agent made each call.

## What's deterministic vs. what the LLM does

The **math is deterministic and offline-testable**: valuation, lineup/IL
optimization, waiver ranking, surplus-aware multi-player trade search, declined-
trade counter logic, and offer evaluation. The **LLM only sequences and
decides** — it calls these tools, weighs close calls, and proposes a batch of
actions. Writes stay DRY-RUN until you explicitly enable them.

## Setup

```bash
pip install -r requirements.txt          # anthropic only needed for `run`
cp .env.example .env                      # then fill in, and `source` it
# ...or instead:
cp bbagent.local.json.example bbagent.local.json   # fill in (gitignored)
```

Get `ESPN_S2` and `SWID` from a logged-in browser: DevTools → Application →
Cookies on `fantasy.espn.com`. `SWID` includes the curly braces.

Config resolves in this order (last wins): built-in defaults →
`bbagent.local.json` → `BBAGENT_*` environment variables. Secrets never live in
source.

## Commands

```bash
python espn_agent.py selftest          # offline, no network/key — proves the logic
python espn_agent.py discover          # slot ids + teams; auto-matches your SWID
python espn_agent.py plan              # print-only pass: IL, lineup, waivers, trades
python espn_agent.py plan --execute    # actually submit moves + send trade offers
python espn_agent.py run "<goal>"      # LLM agent loop via Anthropic API (writes dry-run)
python projections.py selftest         # offline check of the scoring pipeline
```

`plan` is **print-only by default** (logs every recommended move to
`actions.log` but sends nothing). `--execute` acts on the plan — optimal
lineup/IL moves, the top waiver add/drop, and the best *fair* trade offer per
rival (deduped so no player is offered twice and one offer max per team). Even
with `--execute`, writes stay **simulated** until you also pass `--commit` and
verify the payload once. `--no-email` / `--email` control the summary email.

### Email summary

Set the `BBAGENT_SMTP_*` / `BBAGENT_EMAIL_*` keys in `.env` (see `.env.example`)
and every `plan` run emails a readable report of everything it decided/did. For
Gmail, use an App Password, not your login.

## Two ways to run the agent

The deterministic analysis is identical in both; only *who drives the loop* differs.

| | Anthropic API (`run`) | **Claude Code (MCP)** |
|---|---|---|
| Agent loop | Anthropic API model | Claude Code (your session) |
| Auth / cost | `ANTHROPIC_API_KEY` + credits | Your Pro/Max subscription — no API key |
| Entry point | `python espn_agent.py run "<goal>"` | `bbagent_mcp.py` over MCP |

### Claude Code (no API key)

`bbagent_mcp.py` serves the same toolset over MCP, so **Claude Code is the agent
loop** — running on your Pro/Max subscription instead of API credits.

```bash
pip install "mcp[cli]"            # plus requests
python bbagent_mcp.py --selftest  # offline smoke test of every tool
```

This directory ships a `.mcp.json`, so Claude Code launched here auto-discovers
the `bbagent` server (approve it once when prompted). Or register it explicitly:

```bash
claude mcp add bbagent -- python bbagent_mcp.py
```

Then just ask, e.g. *"Use bbagent to manage my team to win this week."* Claude
Code calls `full_plan`, `check_il`, `optimize_lineup`, `scout_trades`,
`review_declined_trades`, etc., and proposes a batch of actions. Operating
guidance for the agent lives in `CLAUDE.md`. Writes stay DRY-RUN until you set
`BBAGENT_COMMIT_WRITES=1` (in `.mcp.json` `env`) and verify the payload.

`AGENT_TEAM_ID` is **auto-resolved** from your `SWID` — `discover` shows the
match (marked `*`). Pin it in config only if the match is ambiguous.

## Enabling real writes (one-time verification)

Writes are gated behind the `COMMIT_WRITES` master switch (`--commit` flag) and
use ESPN's v3 `transactions` endpoint. The payload shapes (lineup ROSTER moves,
ADD/DROP, TRADE) are implemented to ESPN's documented protocol, but **ESPN's
write API is unofficial** — verify once before trusting it:

1. Log in, open DevTools → Network (Fetch/XHR).
2. Make a real, harmless lineup move in the UI on your team; submit.
3. Compare that POST's URL + JSON body to what `plan`/`run` print as
   `would_post` (every dry-run echoes the exact payload it would send).
4. If they match, run with `--commit`. If ESPN tweaked the shape, adjust
   `EspnClient._post_txn` / the item builders to match the captured request.

Until you do this and pass `--commit`, **nothing is ever POSTed** — you only see
the payloads.

## Projections (optional but recommended)

ESPN's own projections are mediocre. `projections.py` converts external
projection CSVs into ROS point values under *your* league's actual scoring:

```python
from espn_agent import EspnClient, plug_projections
import projections as proj
client = EspnClient()
overrides, unmatched = proj.build_overrides(
    client, hitters_csv="hitters.csv", pitchers_csv="pitchers.csv")
plug_projections(overrides)
print("fix these names:", unmatched)
```

Two stat-id mappings are flagged to confirm against ESPN → Settings → Scoring
(see the docstring in `projections.py`).

## Files

- `espn_agent.py` — client, deterministic core, agent toolset, CLI + API loop.
- `bbagent_mcp.py` — MCP server exposing the toolset to Claude Code (no API key).
- `projections.py` — external-projection → league-scored value pipeline.
- `.mcp.json` — Claude Code server config (auto-discovered in this directory).
- `CLAUDE.md` — operating instructions Claude Code loads automatically.
- `actions.log` — append-only audit of every tool call and action (gitignored).
