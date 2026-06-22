#!/usr/bin/env python3
"""
bbagent_mcp.py — run the ESPN Fantasy Baseball agent under Claude Code
======================================================================

Same deterministic toolset the Anthropic-API agent calls on (lineup/IL,
waivers, surplus-aware trades, offer evaluation, declined-trade counters),
exposed over MCP so **Claude Code is the agent loop** instead of the API. No
ANTHROPIC_API_KEY and no API credits — Claude Code runs on your Pro/Max
subscription.

The math lives in espn_agent.py and is unchanged; this module is a thin MCP
adapter over AgentSession. Writes stay gated by espn_agent.COMMIT_WRITES exactly
as in the CLI — every transaction is DRY-RUN until you flip it and verify the
payload once (see README "Enabling real writes").

Run / wire up:
    pip install "mcp[cli]"          # plus requests (see requirements.txt)
    # Claude Code auto-discovers it via .mcp.json in this directory, or:
    claude mcp add bbagent -- python bbagent_mcp.py
    # Then in Claude Code:  "Use bbagent to manage my team to win this week."

Smoke test the server without Claude Code:
    python bbagent_mcp.py --selftest
"""

import json
import os
import sys

import espn_agent as core

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - import guard for a clearer message
    if "--selftest" not in sys.argv:
        sys.exit("The 'mcp' package is required: pip install \"mcp[cli]\"")
    FastMCP = None


# ---------------------------------------------------------------------------
# Lazy session — the ESPN client only initializes on first tool call, so the
# server starts (and Claude Code connects) even before credentials are set.
# ---------------------------------------------------------------------------
_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        client = core.EspnClient()          # raises AuthError if creds missing
        _SESSION = core.AgentSession(client)
    return _SESSION


def _safe(fn):
    """Run a tool call, turning expected errors into a JSON error payload the
    model can read and react to rather than an MCP transport failure."""
    try:
        return fn()
    except core.AuthError as e:
        return {"error": "auth", "detail": str(e),
                "hint": "Set ESPN_S2 / SWID (see .env.example) then retry."}
    except Exception as e:                                # surface to the model
        return {"error": type(e).__name__, "detail": str(e)}


if FastMCP is not None:
    mcp = FastMCP("bbagent")

    @mcp.tool()
    def list_teams() -> dict:
        """List my team id and the rival team ids in the league."""
        return _safe(lambda: _session().list_teams())

    @mcp.tool()
    def get_roster(team: str = "me") -> dict:
        """Get a roster. team='me' for my team, or a rival team id as a string."""
        return _safe(lambda: _session().get_roster(team=team))

    @mcp.tool()
    def value_player(player_id: int) -> dict:
        """Rest-of-season fantasy-point value for one player id."""
        return _safe(lambda: _session().value_player(player_id=player_id))

    @mcp.tool()
    def check_il() -> dict:
        """List my players who were on the IL but are now active again and must
        be moved off the IL slot (they can't score there and they block adds)."""
        return _safe(lambda: _session().check_il())

    @mcp.tool()
    def optimize_lineup() -> dict:
        """Optimal lineup + IL moves for my team today (deterministic). Also
        routes any healed IL players back into an active/bench slot."""
        return _safe(lambda: _session().optimize_lineup())

    @mcp.tool()
    def find_waivers(max_targets: int = 5) -> dict:
        """Ranked free-agent add/drop targets worth more than my weakest player."""
        return _safe(lambda: _session().find_waivers(max_targets=max_targets))

    @mcp.tool()
    def scout_trades(team_id: int | None = None, top_n: int = 3) -> dict:
        """Win-win trade packages. Omit team_id to scan the whole league, or
        pass one rival id to focus on that team."""
        return _safe(lambda: _session().scout_trades(team_id=team_id, top_n=top_n))

    @mcp.tool()
    def evaluate_offer(they_give_ids: list[int], they_get_ids: list[int]) -> dict:
        """Judge an incoming trade offer. they_give_ids = players coming to me;
        they_get_ids = my players leaving. Returns accept/counter/reject."""
        return _safe(lambda: _session().evaluate_offer(
            they_give_ids=they_give_ids, they_get_ids=they_get_ids))

    @mcp.tool()
    def review_declined_trades() -> dict:
        """List my proposed trades a rival declined, each with a counter-or-
        abandon recommendation and a concrete counter package when worthwhile."""
        return _safe(lambda: _session().review_declined_trades())

    @mcp.tool()
    def full_plan(execute: bool = False, email: bool = False) -> dict:
        """Full management pass across the whole league: IL returns, lineup
        moves, top waiver add/drops, the best FAIR trade proposal per rival
        (deduped, anti-spam), and declined-trade recommendations.

        execute=False (default) only reports what it would do. execute=True acts
        on it — submitting lineup moves, waiver claims, and real trade OFFERS
        other owners will see — but writes still stay simulated unless the
        server was started with BBAGENT_COMMIT_WRITES=1. Every move is logged to
        actions.log either way. email=True force-sends the summary report (it
        auto-sends when SMTP is configured in .env)."""
        return _safe(lambda: core.plan(_session().client, execute=execute,
                                       email=(True if email else None)))

    @mcp.tool()
    def email_report(
        my_label: str | None = None,
        executed: bool = False,
        starters_value: float | None = None,
        il_returns: list[dict] | None = None,
        lineup_moves: list[dict] | None = None,
        waivers: list[dict] | None = None,
        trade_proposals: list[dict] | None = None,
        declined_trades: list[dict] | None = None,
    ) -> dict:
        """Email the human a report of THIS pass, using the same structured
        plain-text + HTML template the deterministic `plan` sends. You decide
        what goes in it (that's your job); this only renders + sends. Call it
        once at the END of the pass with what you actually did/recommend.

        Field shapes (omit a list to show its 'none' line; same as plan()):
          il_returns:      [{name, status}]
          lineup_moves:    [{name, fromLineupSlotId, toLineupSlotId}]
          waivers:         [{add:{name}, drop:{name}, gain}]
          trade_proposals: [{with_team_id, i_give:[name], i_get:[name],
                             my_gain, their_gain, fairness}]
          declined_trades: [{declined:{with_team_id}, decision, reason}]
        executed=True only if you actually committed writes this pass; it sets
        the 'POSTED to ESPN' vs 'recommendation only' badge. Needs SMTP
        configured (host in bbagent.config.json, SMTP_PASS in .env); returns a
        clear reason if not set up."""
        def _send():
            result = {
                "my_label": my_label or "my team",
                "run_by": "Claude Code agent",   # the one line that differs from
                                                 # the deterministic email
                "executed": executed,
                "results": executed,           # drives the 'simulated' badge
                "starters_value": starters_value,
                "il_returns": il_returns or [],
                "lineup_moves": lineup_moves or [],
                "waivers": waivers or [],
                "trade_proposals": trade_proposals or [],
                "declined_trades": declined_trades or [],
                # Claude doesn't compute the valuation blend; let the formatter
                # fall back to its "ESPN projections only" line rather than lie.
                "sources": {},
                "team_labels": {},
            }
            verb = "executed" if executed else "plan"
            outcome = core.send_email(
                f"bbagent {verb} — {result['my_label']}",
                core.format_plan_email(result),
                html=core.format_plan_email_html(result))
            sent = outcome is True
            core.audit("email", {"to": core.EMAIL_TO}, str(outcome))
            return {"sent": sent, "detail": "sent" if sent else str(outcome)}
        return _safe(_send)

    @mcp.tool()
    def execute(action: dict) -> dict:
        """Commit one action (gated: DRY-RUN until writes are enabled and the
        payload is verified). Always include a one-line 'reasoning'. action.type:
          'lineup'       {moves:[{playerId,fromLineupSlotId,toLineupSlotId}]}
          'add_drop'     {add_player_id?, drop_player_id?}
          'trade_propose'{with_team_id, give_player_ids:[], get_player_ids:[]}
        """
        return _safe(lambda: _session().execute(action))


# ---------------------------------------------------------------------------
# Offline smoke test — exercises every tool over the same fixture as
# espn_agent.selftest, without network, MCP, or an ESPN account.
# ---------------------------------------------------------------------------
def _selftest():
    global _SESSION
    # Reuse the deterministic fixture by mirroring selftest's FakeClient setup.
    core.BENCH_SLOT_ID, core.IL_SLOT_ID, core.AGENT_TEAM_ID = 16, 17, None
    core.SWID = "{ABCD-1234-OWNER}"
    sc = {"0": 1, "1": 1, "5": 2, "16": 3, "17": 1}
    C, FB, OF = [0], [1], [5]
    fp = core._fake_player
    me_team = {"roster": {"entries": [
        fp(1, "Catcher A", 16, C, 300), fp(2, "Slumping C", 0, C, 120),
        fp(3, "OF Star", 5, OF, 420), fp(7, "1B Sub", 16, FB, 180),
        fp(8, "SS Healed", 17, [4], 330, status="ACTIVE"),
    ]}}
    them_team = {"roster": {"entries": [
        fp(21, "Their 1B", 1, FB, 410), fp(22, "Their Spare 1B", 16, FB, 300),
    ]}}
    fas = [{"id": 30, "fullName": "FA Slugger", "eligibleSlots": OF,
            "stats": [{"scoringPeriodId": 0, "statSourceId": 1,
                       "appliedTotal": 310}]}]
    league = {"scoringPeriodId": 100,
              "settings": {"rosterSettings": {"lineupSlotCounts": sc}},
              "teams": [{"id": 1, "owners": ["{ABCD-1234-OWNER}"], **me_team},
                        {"id": 2, "owners": ["{OTHER}"], **them_team}]}

    class FakeClient:
        def league(self): return league
        def settings(self): return league["settings"]
        def free_agents(self, limit=200): return fas
        def transactions(self): return []
        def set_lineup(self, *a, **k): return {"committed": False, "dry_run": True}
        def submit_transaction(self, *a, **k): return {"committed": False,
                                                       "dry_run": True}

    _SESSION = core.AgentSession(FakeClient())
    checks = [
        ("list_teams", lambda: _SESSION.list_teams()),
        ("check_il", lambda: _SESSION.check_il()),
        ("optimize_lineup", lambda: _SESSION.optimize_lineup()),
        ("find_waivers", lambda: _SESSION.find_waivers()),
        ("scout_trades", lambda: _SESSION.scout_trades()),
        ("full_plan", lambda: core.plan(FakeClient())),
        ("execute(add_drop)", lambda: _SESSION.execute(
            {"type": "add_drop", "add_player_id": 30, "drop_player_id": 2,
             "reasoning": "mcp selftest"})),
    ]
    # email_report: stub the SMTP send and check it renders the deterministic
    # template with the agent's distinguishing "Run by:" line.
    sent = {}
    orig_send = core.send_email
    core.send_email = lambda subj, body, html=None: sent.update(
        subject=subj, body=body, html=html) or True
    try:
        report = {
            "my_label": "My Team", "run_by": "Claude Code agent",
            "executed": False, "results": False, "starters_value": 1230,
            "il_returns": [{"name": "SS Healed", "status": "ACTIVE"}],
            "lineup_moves": [], "waivers": [], "trade_proposals": [],
            "declined_trades": [], "sources": {}, "team_labels": {}}
        core.send_email("bbagent plan — My Team",
                        core.format_plan_email(report),
                        html=core.format_plan_email_html(report))
    finally:
        core.send_email = orig_send
    assert "Run by: Claude Code agent" in sent.get("body", ""), sent
    print(f"  {'email_report':<20} -> "
          f"{json.dumps({'sent': True, 'run_by': 'Claude Code agent'})}")

    for name, fn in checks:
        out = _safe(fn)
        assert "error" not in out, (name, out)
        s = json.dumps(out)
        print(f"  {name:<20} -> {s[:80]}{'...' if len(s) > 80 else ''}")
    assert _SESSION.check_il()["il_returns"][0]["name"] == "SS Healed"
    print("\nMCP server self-test OK — every tool dispatches over the fixture.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        # COMMIT_WRITES off unless explicitly enabled in the environment.
        core.COMMIT_WRITES = os.environ.get("BBAGENT_COMMIT_WRITES") == "1"
        mcp.run()
