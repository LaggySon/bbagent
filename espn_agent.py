#!/usr/bin/env python3
"""
ESPN Fantasy Baseball — competitive agent for ONE solely-owned team
===================================================================

Scope: this agent owns and operates a single independent team to win an H2H
points league. Lineup + IL optimization, waiver add/drop, and trades (proposing
out, evaluating incoming). No shared control with any other roster.

WHAT'S KNOWN vs WHAT YOU MUST SUPPLY
------------------------------------
KNOWN / WORKS:
  - Read layer (rosters, free agents, settings, transactions) — reliable shape.
  - Config + credentials from env / bbagent.local.json (never in source).
  - AGENT_TEAM_ID auto-resolves from your SWID.
  - Valuation, lineup/IL optimization, IL-return detection, waiver ranking,
    trade scoring, declined-trade counter logic — pure, deterministic, and
    exercised by `selftest` with no network or API key.
  - Write payloads (lineup / add-drop / trade) implemented to ESPN's v3
    transactions protocol, gated behind COMMIT_WRITES.
YOU MUST SUPPLY:
  - ESPN_S2 + SWID cookies (see .env.example).
  - A one-time browser verification of the write payload before --commit:
    every dry-run echoes the exact `would_post` body to diff against a real
    captured POST (see README "Enabling real writes").
  - Projection quality: valuation defaults to ESPN's own projections, which are
    mediocre. plug_projections() / projections.py let you override with better
    numbers — the agent is only as smart as what feeds valuation.

Audit: every proposed/committed action is appended to actions.log with its
reasoning, so as commissioner you can show the agent made each call.

Requirements:  pip install -r requirements.txt   (anthropic only for `run`)
Run order:     python espn_agent.py selftest        # offline, proves the logic
               python espn_agent.py discover         # slot ids + team match
               python espn_agent.py plan             # dry-run full management pass
               python espn_agent.py run "<goal>"     # LLM agent loop (dry-run writes)
"""

import argparse
import datetime as _dt
import json
import os
import sys
from itertools import combinations

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
# Secrets and IDs are read from the environment or an (untracked) local JSON
# file so they never live in source control. Resolution order, last wins:
#   1. defaults below   2. bbagent.local.json   3. environment variables
# See .env.example / README for the full list of keys.

def _here(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _load_dotenv():
    """Load a .env file (KEY=value lines) into os.environ if present, without
    overwriting variables already set in the real environment. No dependency."""
    try:
        with open(_here(".env"), encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _load_json(name):
    """Optional JSON config file next to this script. Missing/invalid -> {}."""
    try:
        with open(_here(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_load_dotenv()            # .env -> environment (env vars still win if pre-set)
# Config layering, lowest priority first:
#   bbagent.config.json  — non-secret tuning knobs, SAFE TO COMMIT
#   bbagent.local.json   — personal / secret overrides, gitignored
# Either may be absent. Secrets belong in .env or bbagent.local.json — never in
# the committable bbagent.config.json.
_CONFIG = _load_json("bbagent.config.json")
_LOCAL = _load_json("bbagent.local.json")


def _cfg(key, default=None, cast=str):
    """Resolve one config value, highest priority first:
        env var BBAGENT_<KEY> > bbagent.local.json > bbagent.config.json > default
    Keys match case-insensitively in the JSON files."""
    env = os.environ.get("BBAGENT_" + key)
    raw = None
    if env is not None and env != "":
        raw = env
    else:
        for store in (_LOCAL, _CONFIG):
            if key.lower() in store:
                raw = store[key.lower()]; break
            if key in store:
                raw = store[key]; break
    if raw is None:
        return default
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _as_bool(v):
    """Coerce a config value to bool: accepts 1/0, true/false, yes/no, on/off."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _as_set(v):
    """Comma- or space-separated string (or list) -> set of upper-case tokens."""
    if isinstance(v, (list, tuple, set)):
        items = v
    else:
        items = str(v).replace(",", " ").split()
    return {str(x).strip().upper() for x in items if str(x).strip()}


LEAGUE_ID = _cfg("LEAGUE_ID", None, int)   # set in .env (private) — required
SEASON = _cfg("SEASON", 2026, int)
GAME = _cfg("GAME", "flb")  # flb = baseball

ESPN_S2 = _cfg("ESPN_S2", "")
SWID = _cfg("SWID", "")      # ESPN member id, braces included e.g. {AAAA-...}

# AGENT_TEAM_ID is auto-resolved from SWID at runtime when left unset; you can
# still pin it explicitly via config if the SWID->team match is ambiguous.
AGENT_TEAM_ID = _cfg("AGENT_TEAM_ID", None, int)
BENCH_SLOT_ID = _cfg("BENCH_SLOT_ID", 16, int)   # confirmed from league mSettings
IL_SLOT_ID = _cfg("IL_SLOT_ID", 17, int)         # confirmed from league mSettings
# Injury statuses that make a player unstartable (and IL-eligible). Override if
# your league treats DTD/etc. differently. Comma/space separated.
UNAVAILABLE_STATUSES = _as_set(_cfg(
    "UNAVAILABLE_STATUSES", "OUT,TEN_DAY_DL,FIFTEEN_DAY_DL,SIXTY_DAY_DL"))

MODEL = _cfg("MODEL", "claude-sonnet-4-6")   # opus-4-8 for harder trade judgment
COMMIT_WRITES = _as_bool(_cfg("COMMIT_WRITES", False))  # real POSTs only if True
AUDIT_FILE = _cfg("AUDIT_FILE", "actions.log")
HTTP_RETRIES = _cfg("HTTP_RETRIES", 3, int)
FREE_AGENT_LIMIT = _cfg("FREE_AGENT_LIMIT", 200, int)

# ---- valuation / scouting --------------------------------------------------
USE_SCOUTING = _as_bool(_cfg("USE_SCOUTING", True))   # blend the outside source
SCOUTING_WEIGHT = _cfg("SCOUTING_WEIGHT", 0.6, float)  # external share of blend
SEASON_GAMES = _cfg("SEASON_GAMES", 162, int)          # for ROS pace projection

# ---- decision thresholds (tune the agent's aggressiveness) -----------------
MAX_TRADES = _cfg("MAX_TRADES", 3, int)        # trade offers per pass
MAX_WAIVERS = _cfg("MAX_WAIVERS", 1, int)      # add/drops per pass
WAIVER_MARGIN = _cfg("WAIVER_MARGIN", 1.05, float)   # FA must beat drop by this x
TRADE_BALANCE_TOL = _cfg("TRADE_BALANCE_TOL", 0.15, float)   # sticker-value drift
TRADE_FAIRNESS_RATIO = _cfg("TRADE_FAIRNESS_RATIO", 0.5, float)  # min gain split
TRADE_MAX_PER_SIDE = _cfg("TRADE_MAX_PER_SIDE", 2, int)   # players per package
OFFER_ACCEPT_DELTA = _cfg("OFFER_ACCEPT_DELTA", 1.0, float)   # accept if > this
OFFER_REJECT_DELTA = _cfg("OFFER_REJECT_DELTA", -3.0, float)  # reject if < this

# Email summary (optional). Set these to have `plan` email a readable report of
# everything it decided/did. Gmail etc. need an app password, not your login.
SMTP_HOST = _cfg("SMTP_HOST", "")
SMTP_PORT = _cfg("SMTP_PORT", 587, int)
SMTP_USER = _cfg("SMTP_USER", "")
SMTP_PASS = _cfg("SMTP_PASS", "")
EMAIL_FROM = _cfg("EMAIL_FROM", "")          # defaults to SMTP_USER if unset
EMAIL_TO = _cfg("EMAIL_TO", "")              # comma-separated recipients

READ_BASE = _cfg("READ_BASE",
                 "https://lm-api-reads.fantasy.espn.com/apis/v3/games")
WRITE_BASE = _cfg("WRITE_BASE",
                  "https://lm-api-writes.fantasy.espn.com/apis/v3/games")


# ===========================================================================
# AUDIT
# ===========================================================================
def audit(action, detail, reasoning):
    rec = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "committed": COMMIT_WRITES,
        "detail": detail,
        "reasoning": reasoning,
    }
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


# ===========================================================================
# EMAIL SUMMARY  (optional; stdlib only)
# ===========================================================================
def _sources_line(result):
    """One-line description of which data sources drove the decisions."""
    s = result.get("sources") or {}
    if s.get("mlb_statsapi"):
        w = s.get("blend_weight_external", 0.6)
        gr = (s.get("scouting_info") or {}).get("games_remaining")
        tail = f", {gr} games left" if gr is not None else ""
        return (f"Valuation: blend of MLB StatsAPI ({int(w*100)}%) + "
                f"ESPN projections ({int((1-w)*100)}%){tail}")
    if s.get("scouting_error"):
        return (f"Valuation: ESPN projections only "
                f"(scouting unavailable: {s['scouting_error']})")
    return "Valuation: ESPN projections only"


def format_plan_email(result):
    """Render a plan() result as a plain-text email body — everything the agent
    decided and (if executed) did. Uses owner+team labels and slot names rather
    than raw ids/numbers."""
    acted = result.get("executed")
    labels = result.get("team_labels") or {}
    posted = " (POSTED to ESPN)" if acted else \
             (" (simulated — not posted)" if result.get("results") else
              " (recommendation only — nothing sent)")
    L = []
    L.append(f"bbagent management pass — {result.get('my_label', 'my team')}"
             f"{posted}")
    L.append(f"Generated {_dt.datetime.now().isoformat(timespec='seconds')}")
    L.append(f"Run by: {result.get('run_by') or 'deterministic engine'}")
    L.append(_sources_line(result))
    L.append(f"Optimal starters value: {result.get('starters_value')}")
    L.append("")

    rets = result.get("il_returns") or []
    if rets:
        L.append("IL RETURNS (healed, must leave the IL):")
        for r in rets:
            L.append(f"  - {r['name']} ({r['status']})")
        L.append("")

    moves = result.get("lineup_moves") or []
    L.append(f"LINEUP / IL MOVES ({len(moves)}):")
    for m in moves:
        L.append(f"  - {m['name']}: {slot_name(m['fromLineupSlotId'])} -> "
                 f"{slot_name(m['toLineupSlotId'])}")
    if not moves:
        L.append("  (lineup already optimal)")
    L.append("")

    waivers = result.get("waivers") or []
    L.append(f"WAIVER MOVES ({len(waivers)}):")
    for w in waivers:
        L.append(f"  - ADD {w['add']['name']} / DROP {w['drop']['name']}  "
                 f"(+{w['gain']})")
    if not waivers:
        L.append("  (no worthwhile add/drop)")
    L.append("")

    props = result.get("trade_proposals") or []
    L.append(f"TRADE OFFERS SENT ({len(props)}):" if acted
             else f"TRADE OFFERS TO SEND ({len(props)}):")
    for p in props:
        tid = p["with_team_id"]
        who = labels.get(tid, f"team {tid}")
        L.append(f"  - to {who}:")
        L.append(f"      give {', '.join(p['i_give'])}")
        L.append(f"      get  {', '.join(p['i_get'])}")
        L.append(f"      (me +{p['my_gain']} / them +{p['their_gain']}, "
                 f"fairness {p['fairness']})")
    if not props:
        L.append("  (no fair trade found)")
    L.append("")

    declined = result.get("declined_trades") or []
    if declined:
        L.append(f"DECLINED-TRADE FOLLOW-UPS ({len(declined)}):")
        for d in declined:
            tid = (d.get("declined") or {}).get("with_team_id")
            who = labels.get(tid, f"team {tid}") if tid is not None else ""
            prefix = f"with {who} — " if who else ""
            L.append(f"  - {prefix}{d.get('decision')}: {d.get('reason')}")
        L.append("")

    L.append("Full machine-readable log: actions.log")
    return "\n".join(L)


def format_plan_email_html(result):
    """Render a plan() result as a clean, mobile-friendly HTML email."""
    import html as _html
    acted = result.get("executed")
    labels = result.get("team_labels") or {}
    esc = _html.escape

    badge = ("POSTED to ESPN" if acted else
             "simulated — not posted" if result.get("results") else
             "recommendation only")
    badge_color = "#16a34a" if acted else "#6b7280"

    def chip(text, bg="#eef2ff", fg="#3730a3"):
        return (f'<span style="display:inline-block;padding:1px 7px;'
                f'border-radius:10px;background:{bg};color:{fg};font-size:12px;'
                f'font-weight:600">{esc(text)}</span>')

    def section(title, n):
        return (f'<tr><td style="padding:18px 22px 4px"><span style="font-size:'
                f'13px;font-weight:700;letter-spacing:.04em;color:#111827;'
                f'text-transform:uppercase">{esc(title)}</span>'
                f'<span style="color:#9ca3af;font-size:13px"> · {n}</span>'
                f'</td></tr>')

    def row(html_inner):
        return (f'<tr><td style="padding:3px 22px;font-size:14px;color:#374151">'
                f'{html_inner}</td></tr>')

    P = []
    P.append('<table role="presentation" width="100%" cellpadding="0" '
             'cellspacing="0" style="max-width:560px;margin:0 auto;'
             'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
             'background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;'
             'overflow:hidden">')
    # header
    P.append('<tr><td style="padding:20px 22px;background:#0f172a;color:#fff">'
             '<div style="font-size:18px;font-weight:700">⚾ bbagent</div>'
             f'<div style="font-size:14px;color:#cbd5e1;margin-top:2px">'
             f'{esc(result.get("my_label","my team"))}</div>'
             f'<div style="margin-top:8px">{chip(badge, "#1e293b", "#fff")}'
             f' <span style="color:#94a3b8;font-size:12px">'
             f'{esc(_dt.datetime.now().strftime("%a %b %d, %Y %I:%M %p"))}'
             f'</span></div></td></tr>')
    # meta
    P.append(row(f'<span style="color:#6b7280;font-size:12px">'
                 f'Run by: {esc(result.get("run_by") or "deterministic engine")}'
                 f'<br>{esc(_sources_line(result))}<br>Optimal starters value: '
                 f'<b>{esc(str(result.get("starters_value")))}</b></span>'))

    rets = result.get("il_returns") or []
    if rets:
        P.append(section("IL returns — must leave the IL", len(rets)))
        for r in rets:
            P.append(row(f'{esc(r["name"])} '
                         f'{chip(r["status"], "#fef2f2", "#b91c1c")}'))

    moves = result.get("lineup_moves") or []
    P.append(section("Lineup / IL moves", len(moves)))
    if moves:
        for m in moves:
            P.append(row(f'<b>{esc(m["name"])}</b> &nbsp;'
                         f'{chip(slot_name(m["fromLineupSlotId"]),"#f3f4f6","#6b7280")}'
                         f' <span style="color:#9ca3af">→</span> '
                         f'{chip(slot_name(m["toLineupSlotId"]))}'))
    else:
        P.append(row('<span style="color:#9ca3af">lineup already optimal</span>'))

    waivers = result.get("waivers") or []
    P.append(section("Waiver moves", len(waivers)))
    if waivers:
        for w in waivers:
            P.append(row(
                f'{chip("ADD","#ecfdf5","#047857")} <b>{esc(w["add"]["name"])}</b>'
                f' &nbsp; {chip("DROP","#fef2f2","#b91c1c")} {esc(w["drop"]["name"])}'
                f' <span style="color:#16a34a;font-weight:600">+{w["gain"]}</span>'))
    else:
        P.append(row('<span style="color:#9ca3af">no worthwhile add/drop</span>'))

    props = result.get("trade_proposals") or []
    P.append(section("Trade offers sent" if acted else "Trade offers to send",
                     len(props)))
    if props:
        for p in props:
            who = labels.get(p["with_team_id"], f'team {p["with_team_id"]}')
            fair_pct = int(p["fairness"] * 100)
            P.append(row(
                f'<div style="border:1px solid #e5e7eb;border-radius:8px;'
                f'padding:8px 10px;margin:4px 0">'
                f'<div style="font-weight:600;color:#111827">{esc(who)}</div>'
                f'<div style="margin-top:3px"><span style="color:#b91c1c">▲ give</span> '
                f'{esc(", ".join(p["i_give"]))}</div>'
                f'<div><span style="color:#047857">▼ get</span> '
                f'{esc(", ".join(p["i_get"]))}</div>'
                f'<div style="margin-top:4px;color:#6b7280;font-size:12px">'
                f'me +{p["my_gain"]} · them +{p["their_gain"]} · '
                f'{chip(f"fairness {fair_pct}%","#eff6ff","#1d4ed8")}</div></div>'))
    else:
        P.append(row('<span style="color:#9ca3af">no fair trade found</span>'))

    declined = result.get("declined_trades") or []
    if declined:
        P.append(section("Declined-trade follow-ups", len(declined)))
        for d in declined:
            tid = (d.get("declined") or {}).get("with_team_id")
            who = labels.get(tid, f"team {tid}") if tid is not None else ""
            dec = d.get("decision", "")
            col = "#1d4ed8" if dec == "counter" else "#6b7280"
            P.append(row(f'{chip(dec, "#eff6ff", col)} '
                         f'{esc((who + " — ") if who else "")}'
                         f'<span style="color:#6b7280">{esc(d.get("reason",""))}</span>'))

    P.append('<tr><td style="padding:16px 22px;color:#9ca3af;font-size:11px;'
             'border-top:1px solid #f3f4f6">Full machine-readable log: '
             'actions.log</td></tr>')
    P.append('</table>')
    return ('<div style="background:#f3f4f6;padding:20px 0">'
            + "".join(P) + '</div>')


def send_email(subject, body, html=None):
    """Send an email via configured SMTP. `body` is plain text; `html`, if
    given, is sent as an alternative part (nicely-formatted clients show it).
    Returns True on success, or a reason string if not sent — never raises so it
    can't break a management pass."""
    import smtplib
    from email.message import EmailMessage
    if not (SMTP_HOST and EMAIL_TO):
        return "email not configured (set SMTP_HOST and EMAIL_TO)"
    sender = EMAIL_FROM or SMTP_USER
    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return True
    except Exception as e:                                # never break the pass
        return f"email send failed: {type(e).__name__}: {e}"


# ===========================================================================
# ESPN CLIENT  (read = reliable; write = gated + capture-required)
# ===========================================================================
class AuthError(RuntimeError):
    """Raised when ESPN rejects the cookies (missing/expired ESPN_S2 / SWID)."""


class EspnClient:
    # Browser-like headers; ESPN's edge rejects some default user-agents.
    _BASE_HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "X-Fantasy-Source": "kona",
        "X-Fantasy-Platform": "kona-PROD",
    }

    def __init__(self, retries=None):
        import requests
        if retries is None:
            retries = HTTP_RETRIES
        if not ESPN_S2 or not SWID:
            raise AuthError(
                "ESPN_S2 and SWID are not set. Provide them via environment "
                "(BBAGENT_ESPN_S2 / BBAGENT_SWID) or bbagent.local.json. "
                "See .env.example.")
        if not LEAGUE_ID:
            raise AuthError(
                "LEAGUE_ID is not set. Put BBAGENT_LEAGUE_ID in your .env "
                "(it's private; not committed). See .env.example.")
        self._retries = retries
        self._session = requests.Session()
        self._session.headers.update(self._BASE_HEADERS)
        self._session.cookies.update({"espn_s2": ESPN_S2, "SWID": SWID})

    # ---- low-level HTTP with retry + clear auth errors ----
    def _request(self, method, url, **kw):
        import requests
        last = None
        for attempt in range(self._retries):
            try:
                r = self._session.request(method, url, timeout=30, **kw)
            except requests.RequestException as e:        # transient network
                last = e
                continue
            if r.status_code in (401, 403):
                raise AuthError(
                    f"ESPN returned {r.status_code} — cookies are missing, "
                    "expired, or lack access to this league. Refresh ESPN_S2 "
                    "and SWID from a logged-in browser session.")
            if r.status_code >= 500 and attempt < self._retries - 1:
                continue                                  # retry server error
            r.raise_for_status()
            return r
        raise RuntimeError(f"request to {url} failed after retries: {last}")

    def _get(self, *views, extra_headers=None):
        url = f"{READ_BASE}/{GAME}/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}"
        params = [("view", v) for v in views]
        r = self._request("GET", url, params=params, headers=extra_headers or {})
        return r.json()

    def settings(self):
        return self._get("mSettings")["settings"]

    def league(self):
        return self._get("mRoster", "mTeam", "mSettings")

    def free_agents(self, limit=None):
        if limit is None:
            limit = FREE_AGENT_LIMIT
        flt = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                           "limit": limit,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        data = self._get("kona_player_info",
                         extra_headers={"x-fantasy-filter": json.dumps(flt)})
        players = data.get("players", [])
        return [p.get("player", p) for p in players]

    def transactions(self):
        """Pending/recent transactions, including trades I proposed and their
        status (PENDING / ACCEPTED / DECLINED). Used to react to rivals'
        responses to my trade offers."""
        data = self._get("mTransactions2", "mPendingTransactions")
        return (data.get("transactions") or
                data.get("pendingTransactions") or [])

    # ---- write protocol (ESPN v3 transactions endpoint) -------------------
    # The transactions endpoint takes a single transaction object. Lineup
    # changes are a ROSTER transaction whose items each move one player between
    # lineup slots; add/drops and trades are TRANSACTION/TRADE types. All are
    # gated behind COMMIT_WRITES — until you flip it (and verify once against a
    # throwaway move) every call returns the exact payload it *would* POST.
    def _txn_url(self):
        return (f"{WRITE_BASE}/{GAME}/seasons/{SEASON}/segments/0/leagues/"
                f"{LEAGUE_ID}/transactions/")

    def _post_txn(self, body):
        url = self._txn_url()
        if not COMMIT_WRITES:
            return {"committed": False, "would_post": {"url": url, "body": body}}
        r = self._request("POST", url,
                          headers={"Content-Type": "application/json"},
                          data=json.dumps(body))
        # Some successful ESPN writes return an empty body.
        try:
            payload = r.json()
        except ValueError:
            payload = {"status": r.status_code}
        return {"committed": True, "response": payload}

    def set_lineup(self, team_id, moves, scoring_period):
        """Move players between lineup slots on the owned team.

        moves: [{playerId, fromLineupSlotId, toLineupSlotId}, ...]
        """
        body = {
            "isLeagueManager": False,
            "teamId": team_id,
            "type": "ROSTER",
            "scoringPeriodId": scoring_period,
            "executionType": "EXECUTE",
            "items": [{"playerId": m["playerId"], "type": "LINEUP",
                       "fromLineupSlotId": m["fromLineupSlotId"],
                       "toLineupSlotId": m["toLineupSlotId"]} for m in moves],
        }
        return self._post_txn(body)

    def submit_transaction(self, team_id, items, ttype, scoring_period):
        """Add/drop and trade propose/respond. `items` follow ESPN's shape:
          add:   {playerId, type:"ADD", toTeamId}
          drop:  {playerId, type:"DROP", fromTeamId}
          trade: {playerId, type:"TRADE", fromTeamId, toTeamId}
        """
        body = {
            "isLeagueManager": False,
            "teamId": team_id,
            "type": ttype,
            "scoringPeriodId": scoring_period,
            "executionType": "EXECUTE",
            "items": items,
        }
        return self._post_txn(body)


# ===========================================================================
# VALUATION  (pluggable; defaults to ESPN projections — admittedly mediocre)
# ===========================================================================
_PROJECTION_OVERRIDE = {}  # playerId -> ROS points, set via plug_projections()


def plug_projections(mapping):
    """Override valuation with better external ROS projections."""
    _PROJECTION_OVERRIDE.update(mapping)


def player_name(p):
    return p.get("fullName") or p.get("name") or f"player {p.get('id')}"


def espn_ros_value(p):
    """ESPN's own rest-of-season estimate for a player (no external source).
      - ESPN projected season total minus actual-to-date, else season total.
    NOTE: ESPN stat-block semantics (statSourceId 0=actual/1=projected,
    scoringPeriodId 0 = season aggregate) are widely-used but VERIFY against
    your own data — extraction is defensive but not guaranteed.
    """
    proj_season = actual = None
    for s in p.get("stats", []):
        if s.get("scoringPeriodId", 0) != 0:
            continue
        applied = s.get("appliedTotal")
        if applied is None:
            continue
        if s.get("statSourceId") == 1:
            proj_season = applied
        elif s.get("statSourceId") == 0:
            actual = applied
    if proj_season is None:
        return 0.0
    if actual is not None and proj_season > actual:
        return float(proj_season - actual)   # crude ROS
    return float(proj_season)


def ros_value(p):
    """Rest-of-season fantasy-point value used everywhere. Prefers an external
    override (set via plug_projections / blend_projections) over ESPN's own."""
    pid = p.get("id")
    if pid in _PROJECTION_OVERRIDE:
        return float(_PROJECTION_OVERRIDE[pid])
    return espn_ros_value(p)


def blend_projections(client, external, weight=0.6):
    """Combine an external source (e.g. MLB-StatsAPI ROS values) with ESPN's own
    projections and install the blend as the override used by valuation.

    weight is the external source's share (0..1); ESPN gets the rest. Players
    only one source knows about keep that source's number. This is what lets a
    management pass DECIDE using BOTH sources rather than trusting either alone.
    Returns the blended {id: value} mapping for inspection.
    """
    # ESPN value per player id, across rosters + free agents
    league = client.league()
    espn_val = {}
    for t in league["teams"]:
        for e in t.get("roster", {}).get("entries", []):
            pl = e["playerPoolEntry"]["player"]
            espn_val[pl["id"]] = espn_ros_value(pl)
    for f in client.free_agents():
        espn_val[f.get("id")] = espn_ros_value(f)

    blended = {}
    for pid in set(espn_val) | set(external):
        e = espn_val.get(pid)
        x = external.get(pid)
        if e is not None and x is not None:
            blended[pid] = round(weight * x + (1 - weight) * e, 1)
        else:
            blended[pid] = round(x if x is not None else e, 1)
    plug_projections(blended)
    return blended


# ===========================================================================
# ROSTER MODEL
# ===========================================================================
# ESPN baseball lineup-slot ids -> human label. Bench/IL filled from config so
# they track this league's actual non-scoring slots.
_SLOT_NAMES = {
    0: "C", 1: "1B", 2: "2B", 3: "3B", 4: "SS", 5: "OF", 6: "2B/SS",
    7: "1B/3B", 8: "LF", 9: "CF", 10: "RF", 11: "DH", 12: "UTIL",
    13: "P", 14: "SP", 15: "RP", 16: "BE", 17: "IL", 19: "IF",
}


def slot_name(slot_id):
    """Human label for a lineup-slot id, e.g. 5 -> 'OF', 16 -> 'BE', 17 -> 'IL'."""
    if slot_id == BENCH_SLOT_ID:
        return "BE"
    if slot_id == IL_SLOT_ID:
        return "IL"
    return _SLOT_NAMES.get(slot_id, f"slot {slot_id}")


def active_slot_ids(slot_counts):
    skip = {str(BENCH_SLOT_ID), str(IL_SLOT_ID)}
    return {int(s): c for s, c in slot_counts.items()
            if int(c) > 0 and s not in skip}


def roster_players(team):
    out = []
    for e in team.get("roster", {}).get("entries", []):
        p = e["playerPoolEntry"]["player"]
        out.append({"id": p["id"], "name": player_name(p),
                    "slot": e["lineupSlotId"],
                    "eligible": p.get("eligibleSlots", []),
                    "status": p.get("injuryStatus", "ACTIVE"),
                    "value": ros_value(p)})
    return out


# ===========================================================================
# LINEUP + IL OPTIMIZER  (deterministic)
# ===========================================================================
def optimize_lineup(players, slot_counts):
    """
    Greedy assignment maximizing total ROS value across active slots, honoring
    eligibility, with injured players routed to IL when IL slots exist.
    (Greedy is near-optimal; swap in scipy linear_sum_assignment for the exact
    max-weight matching if you want guaranteed-optimal.)
    Returns list of move dicts {playerId, name, fromLineupSlotId, toLineupSlotId}.
    """
    active = active_slot_ids(slot_counts)
    openings = []  # (slotId) expanded by count
    for slot, cnt in active.items():
        openings.extend([slot] * cnt)

    healthy = sorted([p for p in players
                      if p["status"] not in UNAVAILABLE_STATUSES],
                     key=lambda x: x["value"], reverse=True)
    assigned, used_slots_idx = {}, set()
    for p in healthy:
        for i, slot in enumerate(openings):
            if i in used_slots_idx:
                continue
            if slot in p["eligible"]:
                assigned[p["id"]] = slot
                used_slots_idx.add(i)
                break

    moves = []
    for p in players:
        target = assigned.get(p["id"])
        if target is None:
            il = (IL_SLOT_ID if (p["status"] in UNAVAILABLE_STATUSES
                                 and IL_SLOT_ID is not None
                                 and IL_SLOT_ID in p["eligible"]) else BENCH_SLOT_ID)
            target = il
        if target != p["slot"]:
            moves.append({"playerId": p["id"], "name": p["name"],
                          "fromLineupSlotId": p["slot"], "toLineupSlotId": target})
    return moves


# ===========================================================================
# IL WATCH  (players who healed and must come off the IL slot)
# ===========================================================================
def il_returns(players):
    """Players sitting in the IL slot whose injury status is now active.

    ESPN won't let you start a player parked on the IL, and an occupied IL slot
    blocks adds; a healed player there is dead weight until moved. Flags each so
    the lineup optimizer (or you) can route them back to an active/bench slot.
    """
    out = []
    for p in players:
        if p["slot"] == IL_SLOT_ID and p["status"] not in UNAVAILABLE_STATUSES:
            out.append({"id": p["id"], "name": p["name"],
                        "status": p["status"], "value": p["value"]})
    return out


# ===========================================================================
# WAIVERS / ADD-DROP
# ===========================================================================
def waiver_targets(my_players, free_agents, max_targets=5, margin=None):
    """Free agents worth more than my weakest droppable (non-IL) player."""
    if margin is None:
        margin = WAIVER_MARGIN
    droppable = sorted([p for p in my_players if p["slot"] != IL_SLOT_ID],
                       key=lambda x: x["value"])
    if not droppable:
        return []
    fa = sorted(({"id": f["id"], "name": player_name(f), "value": ros_value(f),
                  "eligible": f.get("eligibleSlots", [])} for f in free_agents),
                key=lambda x: x["value"], reverse=True)
    out = []
    for f in fa:
        worst = droppable[0]
        if f["value"] > worst["value"] * margin:   # margin to avoid churn
            out.append({"add": f, "drop": worst,
                        "gain": round(f["value"] - worst["value"], 1)})
            droppable = droppable[1:] + [f]
            droppable.sort(key=lambda x: x["value"])
        if len(out) >= max_targets:
            break
    return out


# ===========================================================================
# TRADES  (value core deterministic; plausibility/judgment via LLM, optional)
# ===========================================================================
def starters_value(players, slot_counts):
    """Sum of values of the optimal active lineup — the metric trades should raise."""
    active = active_slot_ids(slot_counts)
    openings = []
    for slot, cnt in active.items():
        openings.extend([slot] * cnt)
    healthy = sorted([p for p in players
                      if p["status"] not in UNAVAILABLE_STATUSES],
                     key=lambda x: x["value"], reverse=True)
    used, total = set(), 0.0
    for p in healthy:
        for i, slot in enumerate(openings):
            if i in used:
                continue
            if slot in p["eligible"]:
                used.add(i)
                total += p["value"]
                break
    return total


def _packages(pool, max_size):
    combos = []
    for size in range(1, max_size + 1):
        combos.extend(combinations(pool, size))
    return combos


def propose_trades(my_players, their_players, slot_counts,
                   max_per_side=None, top_n=3, balance_tol=None,
                   fairness_ratio=None):
    """
    Multi-player package search that only surfaces trades a rational rival
    would actually consider — not lopsided "spit in the face" offers.

    A package must clear THREE gates:
      1. Win-win: raises BOTH teams' OPTIMAL starting-lineup value (d_me, d_them
         both > 0). Modeling the other team's positional needs falls out for
         free — their starters-value rises only if the package fills a slot they
         actually start, so surplus-for-surplus deals surface naturally.
      2. Balanced sticker value: the raw summed values of the two packages are
         within balance_tol of each other (don't ship a star for scrubs).
      3. Comparable benefit (fairness_ratio): the smaller lineup-gain is at
         least fairness_ratio x the larger — so neither side makes out wildly
         better than the other. This is the gate that kills the insulting
         "+416 for me / +42 for them" deal: 42/416 = 0.10 < 0.5, rejected.

    Ranked by the BALANCED outcome: total value created (d_me + d_them), with
    the closer-to-even split winning ties — so the best mutually-fair deals come
    first, not the ones that merely maximize my own gain.

    Pools are capped at the top 8 assets per side to keep the combinatorics
    sane (~36x36 packages/team). Returns up to top_n packages, best first.

    max_per_side / balance_tol / fairness_ratio default to the configured
    TRADE_* knobs when not passed explicitly.
    """
    if max_per_side is None:
        max_per_side = TRADE_MAX_PER_SIDE
    if balance_tol is None:
        balance_tol = TRADE_BALANCE_TOL
    if fairness_ratio is None:
        fairness_ratio = TRADE_FAIRNESS_RATIO
    sc = slot_counts
    base_me = starters_value(my_players, sc)
    base_them = starters_value(their_players, sc)
    give_pool = sorted(my_players, key=lambda x: x["value"], reverse=True)[:8]
    get_pool = sorted(their_players, key=lambda x: x["value"], reverse=True)[:8]

    out, seen = [], set()
    for give in _packages(give_pool, max_per_side):
        gv = sum(p["value"] for p in give)
        give_ids = frozenset(p["id"] for p in give)
        for get in _packages(get_pool, max_per_side):
            tv = sum(p["value"] for p in get)
            hi = max(gv, tv, 1.0)
            if abs(gv - tv) / hi > balance_tol:        # gate 2: sticker balance
                continue
            get_ids = frozenset(p["id"] for p in get)
            new_me = [p for p in my_players if p["id"] not in give_ids] + list(get)
            new_them = [p for p in their_players
                        if p["id"] not in get_ids] + list(give)
            d_me = starters_value(new_me, sc) - base_me
            d_them = starters_value(new_them, sc) - base_them
            if d_me <= 0 or d_them <= 0:                # gate 1: win-win
                continue
            lo, his = min(d_me, d_them), max(d_me, d_them)
            if lo < fairness_ratio * his:              # gate 3: comparable benefit
                continue
            key = (give_ids, get_ids)
            if key in seen:
                continue
            seen.add(key)
            # fairness = 1.0 for a perfectly even split, ->0 as it skews
            fairness = round(lo / his, 2)
            out.append({"i_give": [p["name"] for p in give],
                        "i_get": [p["name"] for p in get],
                        "my_gain": round(d_me, 1),
                        "their_gain": round(d_them, 1),
                        "fairness": fairness,
                        "score": round(d_me + d_them, 1),
                        "give_ids": sorted(give_ids),
                        "get_ids": sorted(get_ids)})
    # biggest mutual pie first; even splits break ties (more acceptable)
    out.sort(key=lambda x: (x["score"], x["fairness"]), reverse=True)

    # Prune wasteful packages: drop A if some B trades a subset on both sides
    # for at least as much gain (i.e. you could remove players and do as well).
    def dominated(a, b):
        return (set(b["give_ids"]) <= set(a["give_ids"]) and
                set(b["get_ids"]) <= set(a["get_ids"]) and
                (b["give_ids"], b["get_ids"]) != (a["give_ids"], a["get_ids"]) and
                b["my_gain"] >= a["my_gain"] - 1e-9 and
                b["their_gain"] >= a["their_gain"] - 1e-9)

    lean = [a for a in out if not any(dominated(a, b) for b in out if b is not a)]
    return lean[:top_n]


def select_trade_proposals(my_players, others, slot_counts, max_proposals=3,
                           pending_player_ids=None):
    """Pick the actual trade offers to SEND, across the whole league.

    Scans every rival, takes each team's single best fair package, then greedily
    accepts the highest-value ones subject to two anti-spam / anti-conflict
    rules so the proposals are sane to fire autonomously:
      - one pending offer per rival team (don't bombard a single owner), and
      - no player appears in two simultaneous offers (can't trade the same guy
        to two teams), also respecting players already in a pending trade.
    Returns up to max_proposals dicts, each with with_team_id / give_player_ids
    / get_player_ids and the gain+fairness metadata.
    """
    best_per_team = []
    for tid, roster in others.items():
        pkgs = propose_trades(my_players, roster, slot_counts, top_n=1)
        if pkgs:
            best_per_team.append({**pkgs[0], "with_team_id": tid})
    best_per_team.sort(key=lambda x: x["score"], reverse=True)

    chosen, used_give, used_teams = [], set(pending_player_ids or []), set()
    for t in best_per_team:
        if t["with_team_id"] in used_teams:
            continue
        give = set(t["give_ids"])
        if give & used_give:                 # a player here is already committed
            continue
        chosen.append({"with_team_id": t["with_team_id"],
                       "give_player_ids": t["give_ids"],
                       "get_player_ids": t["get_ids"],
                       "i_give": t["i_give"], "i_get": t["i_get"],
                       "my_gain": t["my_gain"], "their_gain": t["their_gain"],
                       "fairness": t["fairness"]})
        used_give |= give
        used_teams.add(t["with_team_id"])
        if len(chosen) >= max_proposals:
            break
    return chosen


def parse_declined_trades(transactions, my_team_id):
    """Pull out trades I proposed that the other side declined.

    Returns [{with_team_id, give_player_ids, get_player_ids}] from MY view
    (give = my players I offered, get = their players I wanted).
    """
    out = []
    for t in transactions or []:
        status = (t.get("status") or "").upper()
        ttype = (t.get("type") or "").upper()
        if "TRADE" not in ttype or status not in ("DECLINED", "REJECTED"):
            continue
        if t.get("teamId") != my_team_id and t.get("proposingTeamId") != my_team_id:
            continue
        give, get = [], []
        for it in t.get("items", []):
            pid = it.get("playerId")
            if pid is None:
                continue
            if it.get("fromTeamId") == my_team_id:
                give.append(pid)
            elif it.get("toTeamId") == my_team_id:
                get.append(pid)
        if give or get:
            other = next((tid for tid in (t.get("toTeamId"),
                                          t.get("proposingTeamId"),
                                          t.get("teamId"))
                          if tid not in (None, my_team_id)), None)
            out.append({"with_team_id": other, "give_player_ids": give,
                        "get_player_ids": get})
    return out


def respond_to_decline(declined, my_players, their_players, slot_counts,
                       sweeten_steps=3):
    """A rival declined my trade. Decide: counter or abandon.

    Strategy: the decline signals the other side valued the deal below their
    bar. Re-run the win-win package search against that same team — if it
    surfaces a *different* package that still helps me and helps them MORE than
    the rejected one did, offer that as a counter. Also try sweetening the
    original (adding my next-best surplus asset) up to sweeten_steps times.
    Abandon only when nothing clears both bars without overpaying.

    Returns {decision: 'counter'|'abandon', ...}.
    """
    declined_give = set(declined.get("give_player_ids", []))
    declined_get = set(declined.get("get_player_ids", []))

    # Fresh win-win search, excluding a verbatim re-offer of what was declined.
    candidates = propose_trades(my_players, their_players, slot_counts, top_n=8)
    for c in candidates:
        if (set(c["give_ids"]) == declined_give and
                set(c["get_ids"]) == declined_get):
            continue  # don't re-propose the exact deal they just rejected
        return {"decision": "counter", "reason":
                "found a package that raises the other team's value more than "
                "the rejected offer did", "counter": c}

    # No alternate win-win: try sweetening the original with surplus assets.
    base_them = starters_value(their_players, slot_counts)
    extras = sorted((p for p in my_players
                     if p["id"] not in declined_give and p["slot"] != IL_SLOT_ID),
                    key=lambda x: x["value"])  # cheapest surplus first
    give_ids = set(declined_give)
    for extra in extras[:sweeten_steps]:
        give_ids.add(extra["id"])
        new_them = ([p for p in their_players
                     if p["id"] not in declined_get] +
                    [p for p in my_players if p["id"] in give_ids])
        if starters_value(new_them, slot_counts) - base_them > 0:
            return {"decision": "counter", "reason":
                    "sweetened the original offer so it now improves their "
                    "starting lineup", "counter": {
                        "with_team_id": declined.get("with_team_id"),
                        "give_player_ids": sorted(give_ids),
                        "get_player_ids": sorted(declined_get)}}

    return {"decision": "abandon", "reason":
            "no counter improves the other team without overpaying; pursue a "
            "different target instead"}


def evaluate_offer(offer, my_players, slot_counts):
    """
    offer = {"they_give": [player dicts], "they_get": [player dicts]} from MY view.
    Returns accept / counter / reject with my value delta.
    """
    base = starters_value(my_players, slot_counts)
    incoming_ids = {p["id"] for p in offer["they_get"]}
    new_roster = [p for p in my_players if p["id"] not in incoming_ids]
    new_roster += offer["they_give"]
    delta = starters_value(new_roster, slot_counts) - base
    if delta > OFFER_ACCEPT_DELTA:
        verdict = "accept"
    elif delta > OFFER_REJECT_DELTA:
        verdict = "counter"
    else:
        verdict = "reject"
    return {"verdict": verdict, "my_value_delta": round(delta, 1)}


def llm_trade_judgment(context):
    """Optional plausibility/risk overlay. Lazy-imports anthropic."""
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL, max_tokens=600,
        system=("You judge fantasy baseball trades for an H2H points team. "
                "Given value deltas and rosters, assess realism, injury/age "
                "risk, and positional fit. Reply with a short verdict and why."),
        messages=[{"role": "user", "content": json.dumps(context)}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


# ===========================================================================
# AGENT TOOLS  (deterministic functions the LLM harness can call)
# ===========================================================================
def _norm_swid(s):
    """Normalize a SWID for comparison: strip braces, lowercase."""
    return (s or "").strip().strip("{}").lower()


def resolve_team_id(data, swid):
    """Find the team owned by this SWID in an mTeam payload. Returns the team
    id, or None if it can't be matched (multiple/zero teams own the SWID)."""
    target = _norm_swid(swid)
    if not target:
        return None
    matches = [t["id"] for t in data.get("teams", [])
               if any(_norm_swid(o) == target for o in (t.get("owners") or []))]
    return matches[0] if len(matches) == 1 else None


def team_labels(data):
    """Map team id -> 'Real Name (Team Name)' for human-readable output.
    Prefers the owner's real first/last name; falls back to the ESPN display
    handle only when no real name is on file. Always includes the team name."""
    members = {}
    for m in data.get("members", []):
        real = f"{m.get('firstName','')} {m.get('lastName','')}".strip()
        members[m.get("id")] = real or m.get("displayName") or m.get("id")
    labels = {}
    for t in data.get("teams", []):
        team_name = (t.get("name")
                     or f"{t.get('location','')} {t.get('nickname','')}".strip()
                     or f"Team {t['id']}")
        owners = [members.get(o, o) for o in (t.get("owners") or [])]
        owner = ", ".join(owners) if owners else "unknown owner"
        labels[t["id"]] = f"{owner} ({team_name})"
    return labels


def build_state(client):
    data = client.league()
    slot_counts = data["settings"]["rosterSettings"]["lineupSlotCounts"]
    teams = data["teams"]
    global AGENT_TEAM_ID
    if AGENT_TEAM_ID is None:
        AGENT_TEAM_ID = resolve_team_id(data, SWID)
    if AGENT_TEAM_ID is None:
        raise RuntimeError(
            "Could not determine AGENT_TEAM_ID from SWID. Run `discover` to "
            "list teams and set AGENT_TEAM_ID explicitly in config.")
    me = next((t for t in teams if t["id"] == AGENT_TEAM_ID), None)
    if me is None:
        raise RuntimeError(f"AGENT_TEAM_ID={AGENT_TEAM_ID} not found in league.")
    return {
        "slot_counts": slot_counts,
        "scoring_period": data.get("scoringPeriodId"),
        "team_labels": team_labels(data),
        "me": roster_players(me),
        "others": {t["id"]: roster_players(t)
                   for t in teams if t["id"] != AGENT_TEAM_ID},
    }


# ===========================================================================
# TRANSACTION ITEM BUILDERS  (high-level fields -> ESPN item shape)
# ===========================================================================
def build_add_drop_items(team_id, add_player_id=None, drop_player_id=None):
    """One free-agent add and/or one drop, on the owned team."""
    items = []
    if add_player_id is not None:
        items.append({"playerId": int(add_player_id), "type": "ADD",
                      "toTeamId": team_id})
    if drop_player_id is not None:
        items.append({"playerId": int(drop_player_id), "type": "DROP",
                      "fromTeamId": team_id})
    return items


def build_trade_items(my_team_id, their_team_id, give_player_ids, get_player_ids):
    """Propose a trade: my players (give) -> their team, their players (get) ->
    my team. Each player is one TRADE item with explicit from/to."""
    items = []
    for pid in give_player_ids:
        items.append({"playerId": int(pid), "type": "TRADE",
                      "fromTeamId": my_team_id, "toTeamId": their_team_id})
    for pid in get_player_ids:
        items.append({"playerId": int(pid), "type": "TRADE",
                      "fromTeamId": their_team_id, "toTeamId": my_team_id})
    return items


def plan(client, execute=False, max_trades=None, max_waivers=None, email=None,
         use_scouting=None, scouting_weight=None):
    """Full management pass for the owned team, across the whole league.

    Decides using BOTH data sources by default: the live MLB-StatsAPI outside
    source (scouting.py) is blended with ESPN's own projections (scouting_weight
    = the external share, ESPN gets the rest), and the blend drives every
    valuation — lineup, waivers, and trades. Set use_scouting=False to fall back
    to ESPN projections alone.

    Builds the recommended actions — lineup/IL moves, top waiver add/drops, and
    the best FAIR trade proposal per rival (deduped, anti-spam) — and, when
    `execute` is set, actually submits them via the client. Trade proposals are
    real outbound offers other owners will see, so they go out only when both
    `execute` and COMMIT_WRITES are on; otherwise this is a dry run that returns
    exactly what it *would* send. Every action is logged to actions.log.

    email: None auto-sends a summary when SMTP is configured; True forces a
    send attempt; False disables it.
    """
    # config-backed defaults (explicit args still win)
    if max_trades is None:
        max_trades = MAX_TRADES
    if max_waivers is None:
        max_waivers = MAX_WAIVERS
    if use_scouting is None:
        use_scouting = USE_SCOUTING
    if scouting_weight is None:
        scouting_weight = SCOUTING_WEIGHT

    # ---- blend the outside source with ESPN before valuing anything --------
    sources = {"espn_projections": True, "mlb_statsapi": False}
    if use_scouting:
        try:
            import scouting
            external, sinfo = scouting.build_overrides(client)
            blend_projections(client, external, weight=scouting_weight)
            sources["mlb_statsapi"] = True
            sources["scouting_info"] = sinfo
            sources["blend_weight_external"] = scouting_weight
        except Exception as e:                            # never block the pass
            sources["scouting_error"] = f"{type(e).__name__}: {e}"

    st = build_state(client)
    sc = st["slot_counts"]
    sp = st["scoring_period"]
    returns = il_returns(st["me"])
    lineup_moves = optimize_lineup(st["me"], sc)
    waivers = waiver_targets(st["me"], client.free_agents(),
                             max_targets=max(max_waivers, 5))

    # full-league trade ideas (for visibility) + the subset we'd actually send
    trades = []
    for tid, roster in st["others"].items():
        for t in propose_trades(st["me"], roster, sc):
            t["with_team"] = tid
            trades.append(t)
    trades.sort(key=lambda x: x["score"], reverse=True)

    # don't re-offer players already tied up in a pending trade
    pending_ids = set()
    declined = []
    try:
        txns = client.transactions()
        for t in txns or []:
            if "TRADE" in (t.get("type") or "").upper() and \
               (t.get("status") or "").upper() in ("PENDING", "PROPOSED"):
                for it in t.get("items", []):
                    if it.get("fromTeamId") == AGENT_TEAM_ID:
                        pending_ids.add(it.get("playerId"))
        for d in parse_declined_trades(txns, AGENT_TEAM_ID):
            their = st["others"].get(d.get("with_team_id")) or []
            declined.append({"declined": d,
                             **respond_to_decline(d, st["me"], their, sc)})
    except Exception:                                     # read may be flaky
        pass

    proposals = select_trade_proposals(st["me"], st["others"], sc,
                                        max_proposals=max_trades,
                                        pending_player_ids=pending_ids)

    # ---- log + act ---------------------------------------------------------
    # Every recommended move is logged whether or not we execute (and audit()
    # records committed=COMMIT_WRITES on each), so actions.log is the complete
    # record of what the agent decided, not just what it sent. `mode` marks the
    # intent: "execute" (acted on) vs "recommend" (print-only pass).
    mode = "execute" if execute else "recommend"
    chosen_waivers = waivers[:max_waivers]
    executed = {"lineup": None, "waivers": [], "trades": []}

    if lineup_moves:
        audit(f"{mode}:lineup", {"moves": lineup_moves},
              "Lineup/IL optimization.")
        if execute:
            executed["lineup"] = client.set_lineup(AGENT_TEAM_ID, lineup_moves, sp)
    for w in chosen_waivers:
        audit(f"{mode}:add_drop", w,
              f"Waiver: add {w['add']['name']} / drop {w['drop']['name']} "
              f"(+{w['gain']}).")
        if execute:
            items = build_add_drop_items(AGENT_TEAM_ID, w["add"]["id"],
                                         w["drop"]["id"])
            executed["waivers"].append(
                client.submit_transaction(AGENT_TEAM_ID, items, "TRANSACTION", sp))
    for p in proposals:
        audit(f"{mode}:trade_propose", p,
              f"Fair trade to team {p['with_team_id']}: give {p['i_give']} for "
              f"{p['i_get']} (me +{p['my_gain']}, them +{p['their_gain']}, "
              f"fairness {p['fairness']}).")
        if execute:
            items = build_trade_items(AGENT_TEAM_ID, p["with_team_id"],
                                      p["give_player_ids"], p["get_player_ids"])
            res = client.submit_transaction(AGENT_TEAM_ID, items,
                                            "TRADE_PROPOSAL", sp)
            executed["trades"].append({"proposal": p, "result": res})

    audit("plan", {"il_returns": returns, "lineup_moves": lineup_moves,
                   "waivers": chosen_waivers, "proposals": proposals,
                   "declined_trades": declined, "mode": mode},
          "Autonomous management pass." if execute else "Dry-run management pass.")
    labels = st["team_labels"]
    result = {"executed": bool(execute) and COMMIT_WRITES,
              "sources": sources,
              "team_labels": labels,
              "my_label": labels.get(AGENT_TEAM_ID, f"team {AGENT_TEAM_ID}"),
              "il_returns": returns, "lineup_moves": lineup_moves,
              "waivers": chosen_waivers,
              "trade_proposals": proposals, "trade_ideas": trades[:5],
              "declined_trades": declined,
              "results": executed if execute else None,
              "starters_value": round(starters_value(st["me"], sc), 1)}

    # email summary — auto when configured (email=None), or forced/suppressed
    if email is None:
        email = bool(SMTP_HOST and EMAIL_TO)
    if email:
        verb = "executed" if execute else "plan"
        outcome = send_email(f"bbagent {verb} — {result['my_label']}",
                             format_plan_email(result),
                             html=format_plan_email_html(result))
        result["email"] = "sent" if outcome is True else outcome
        audit("email", {"to": EMAIL_TO}, str(result["email"]))
    return result


# ===========================================================================
# DISCOVERY
# ===========================================================================
def discover(client):
    s = client.settings()
    sc = s.get("rosterSettings", {}).get("lineupSlotCounts", {})
    data = client.league()
    print("Slot counts (slotId -> count):")
    for k, v in sorted(sc.items(), key=lambda x: int(x[0])):
        print(f"  {k:>3}: {v}")
    print("\n-> set BENCH_SLOT_ID / IL_SLOT_ID (the non-scoring slots).\n")
    auto = resolve_team_id(data, SWID)
    print("Teams (id -> name)" +
          ("  [* = auto-matched to your SWID]" if auto else "") + ":")
    for t in data["teams"]:
        nm = t.get("name") or f"{t.get('location','')} {t.get('nickname','')}".strip()
        mark = " *" if t["id"] == auto else ""
        print(f"  {t['id']:>3}: {nm}{mark}")
    if auto:
        print(f"\n-> AGENT_TEAM_ID auto-resolves to {auto} from your SWID; "
              "no need to set it manually.")
    else:
        print("\n-> SWID didn't match exactly one team — set AGENT_TEAM_ID "
              "explicitly in config.")


# ===========================================================================
# AGENT TOOLSET  (deterministic math = tools; the LLM decides & acts)
# ===========================================================================
def _lean(players):
    return [{"id": p["id"], "name": p["name"], "value": p["value"],
             "status": p["status"], "eligible": p["eligible"],
             "slot": p["slot"]} for p in players]


class AgentSession:
    """Holds league state + free agents (cached) and exposes the agent's tools.
    Tools compute; they never decide. The LLM sequences and chooses."""

    def __init__(self, client):
        self.client = client
        self._state = None
        self._fa = None

    def state(self, refresh=False):
        if self._state is None or refresh:
            self._state = build_state(self.client)
        return self._state

    def free_agents(self, refresh=False):
        if self._fa is None or refresh:
            self._fa = self.client.free_agents()
        return self._fa

    def _all_players_by_id(self):
        st = self.state()
        idx = {p["id"]: p for p in st["me"]}
        for roster in st["others"].values():
            for p in roster:
                idx[p["id"]] = p
        return idx

    # ---- read tools ----
    def list_teams(self):
        st = self.state()
        return {"me": AGENT_TEAM_ID, "rivals": sorted(st["others"].keys())}

    def get_roster(self, team="me"):
        st = self.state()
        if team == "me":
            return {"team": "me", "players": _lean(st["me"]),
                    "starters_value": round(starters_value(st["me"],
                                                            st["slot_counts"]), 1)}
        tid = int(team)
        roster = st["others"].get(tid)
        if roster is None:
            return {"error": f"no team {tid}"}
        return {"team": tid, "players": _lean(roster)}

    def value_player(self, player_id):
        p = self._all_players_by_id().get(int(player_id))
        if not p:
            for f in self.free_agents():
                if f.get("id") == int(player_id):
                    return {"id": f["id"], "name": player_name(f),
                            "value": ros_value(f), "rostered": False}
            return {"error": "not found"}
        return {"id": p["id"], "name": p["name"], "value": p["value"],
                "status": p["status"], "rostered": True}

    # ---- analysis tools ----
    def check_il(self):
        """Players on my IL slot who have become active again and must be
        moved off the IL (they can't score there and they block adds)."""
        st = self.state()
        return {"il_returns": il_returns(st["me"])}

    def optimize_lineup(self):
        st = self.state()
        moves = optimize_lineup(st["me"], st["slot_counts"])
        return {"moves": moves, "scoring_period": st["scoring_period"]}

    def find_waivers(self, max_targets=5):
        st = self.state()
        return {"targets": waiver_targets(st["me"], self.free_agents(),
                                          max_targets=max_targets)}

    def scout_trades(self, team_id=None, max_per_side=2, top_n=3):
        st = self.state()
        sc = st["slot_counts"]
        targets = ([int(team_id)] if team_id is not None
                   else list(st["others"].keys()))
        out = []
        for tid in targets:
            roster = st["others"].get(tid)
            if not roster:
                continue
            for t in propose_trades(st["me"], roster, sc,
                                    max_per_side=max_per_side, top_n=top_n):
                t["with_team"] = tid
                out.append(t)
        out.sort(key=lambda x: x["score"], reverse=True)
        return {"packages": out[:top_n if team_id else 8]}

    def evaluate_offer(self, they_give_ids, they_get_ids):
        """they_give_ids: players coming to me. they_get_ids: my players leaving."""
        st = self.state()
        idx = self._all_players_by_id()
        offer = {"they_give": [idx[i] for i in they_give_ids if i in idx],
                 "they_get": [idx[i] for i in they_get_ids if i in idx]}
        res = evaluate_offer(offer, st["me"], st["slot_counts"])
        res["counter_ideas"] = (self.scout_trades(top_n=2)["packages"]
                                if res["verdict"] == "counter" else [])
        return res

    def review_declined_trades(self):
        """List my proposed trades that a rival declined, each with a
        counter-or-abandon recommendation (and a concrete counter package when
        countering is worthwhile)."""
        st = self.state()
        sc = st["slot_counts"]
        try:
            txns = self.client.transactions()
        except Exception as e:                            # read may be flaky
            return {"error": f"could not read transactions: "
                             f"{type(e).__name__}: {e}", "declined": []}
        declined = parse_declined_trades(txns, AGENT_TEAM_ID)
        out = []
        for d in declined:
            their = st["others"].get(d.get("with_team_id")) or []
            decision = respond_to_decline(d, st["me"], their, sc)
            out.append({"declined": d, **decision})
        return {"declined": out}

    # ---- write tool (gated) ----
    def execute(self, action):
        """Commit one action. Shapes (all gated by COMMIT_WRITES):
          {type:"lineup", moves:[{playerId,fromLineupSlotId,toLineupSlotId}]}
          {type:"add_drop", add_player_id?:int, drop_player_id?:int}
          {type:"trade_propose", with_team_id:int,
           give_player_ids:[int], get_player_ids:[int]}
        Always include a one-line `reasoning`."""
        sp = self.state()["scoring_period"]
        typ = action.get("type")
        reason = action.get("reasoning", "")
        try:
            if typ == "lineup":
                res = self.client.set_lineup(AGENT_TEAM_ID, action["moves"], sp)
            elif typ == "add_drop":
                items = build_add_drop_items(
                    AGENT_TEAM_ID, action.get("add_player_id"),
                    action.get("drop_player_id"))
                if not items:
                    return {"error": "add_drop needs add_player_id and/or "
                                     "drop_player_id"}
                res = self.client.submit_transaction(
                    AGENT_TEAM_ID, items, "TRANSACTION", sp)
            elif typ in ("trade_propose", "trade_respond"):
                items = build_trade_items(
                    AGENT_TEAM_ID, int(action["with_team_id"]),
                    action.get("give_player_ids", []),
                    action.get("get_player_ids", []))
                res = self.client.submit_transaction(
                    AGENT_TEAM_ID, items, "TRADE_PROPOSAL", sp)
            else:
                return {"error": f"unknown action type {typ}"}
        except (KeyError, TypeError, ValueError) as e:
            return {"error": f"malformed action: {type(e).__name__}: {e}"}
        audit("execute:" + str(typ), action, reason)
        return res


TOOLS = [
    {"name": "list_teams", "description": "List my team id and rival team ids.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_roster", "description": "Get a roster. team='me' or a rival id.",
     "input_schema": {"type": "object", "properties": {
         "team": {"type": "string"}}}},
    {"name": "value_player", "description": "ROS point value for one player id.",
     "input_schema": {"type": "object", "properties": {
         "player_id": {"type": "integer"}}, "required": ["player_id"]}},
    {"name": "check_il", "description": "List my players who were on the IL but "
     "are now active again and must be moved off the IL slot.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "optimize_lineup", "description": "Optimal lineup + IL moves for my "
     "team today (deterministic). Also routes healed IL players back in.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "find_waivers", "description": "Ranked free-agent add/drop targets.",
     "input_schema": {"type": "object", "properties": {
         "max_targets": {"type": "integer"}}}},
    {"name": "scout_trades", "description": "Win-win trade packages. Omit team_id "
     "to scan the whole league, or pass one rival id to focus.",
     "input_schema": {"type": "object", "properties": {
         "team_id": {"type": "integer"}, "top_n": {"type": "integer"}}}},
    {"name": "evaluate_offer", "description": "Judge an incoming offer. "
     "they_give_ids = players coming to me; they_get_ids = my players leaving.",
     "input_schema": {"type": "object", "properties": {
         "they_give_ids": {"type": "array", "items": {"type": "integer"}},
         "they_get_ids": {"type": "array", "items": {"type": "integer"}}},
         "required": ["they_give_ids", "they_get_ids"]}},
    {"name": "review_declined_trades", "description": "List my proposed trades a "
     "rival declined, each with a counter-or-abandon recommendation and a "
     "concrete counter package when countering is worthwhile.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "execute", "description": "Commit an action (gated: dry-run until "
     "the human enables writes). Always include reasoning. action.type is one "
     "of: 'lineup' {moves:[{playerId,fromLineupSlotId,toLineupSlotId}]}; "
     "'add_drop' {add_player_id?, drop_player_id?}; 'trade_propose' "
     "{with_team_id, give_player_ids:[], get_player_ids:[]}.",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "object"}}, "required": ["action"]}},
]

SYSTEM = (
    "You are the sole owner and general manager of ONE team in an ESPN H2H "
    "fantasy baseball points league. Your standing objective is to win the "
    "league. You have tools to read rosters, value players, check the IL, "
    "optimize the lineup/IL, work the waiver wire, scout trades, and evaluate "
    "incoming offers. The tools do the math; YOU decide what to pursue, in "
    "what order, and when it's worth it.\n\n"
    "Start a management pass by calling check_il: a player who has healed but "
    "is still parked on the IL slot can't score and blocks roster moves, so "
    "clearing them is the first priority. Then optimize the lineup.\n\n"
    "Call review_declined_trades to see offers a rival turned down. For each, "
    "the tool recommends counter or abandon: if it returns a counter package "
    "that you judge worthwhile, propose it with execute; otherwise drop the "
    "idea and look elsewhere. Don't re-send a deal that was just rejected.\n\n"
    "Operate only on your own team. Treat ESPN's projections as noisy — when "
    "a call is close, say so rather than feigning certainty. Use execute to "
    "act, always with a one-line reasoning. Writes are DRY-RUN until the human "
    "enables commits, so propose a clear batch of actions for approval rather "
    "than assuming anything posted. Every tool call is logged.")


def run_agent(goal, client, max_turns=12):
    import anthropic
    cl = anthropic.Anthropic()
    sess = AgentSession(client)
    dispatch = {
        "list_teams": lambda **k: sess.list_teams(),
        "get_roster": lambda **k: sess.get_roster(**k),
        "value_player": lambda **k: sess.value_player(**k),
        "check_il": lambda **k: sess.check_il(),
        "optimize_lineup": lambda **k: sess.optimize_lineup(),
        "find_waivers": lambda **k: sess.find_waivers(**k),
        "scout_trades": lambda **k: sess.scout_trades(**k),
        "evaluate_offer": lambda **k: sess.evaluate_offer(**k),
        "review_declined_trades": lambda **k: sess.review_declined_trades(),
        "execute": lambda **k: sess.execute(**k),
    }
    messages = [{"role": "user", "content": goal}]
    for _ in range(max_turns):
        resp = cl.messages.create(model=MODEL, max_tokens=2000,
                                  system=SYSTEM, tools=TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text")
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if getattr(b, "type", "") != "tool_use":
                continue
            try:
                out = dispatch[b.name](**(b.input or {}))
            except Exception as e:                       # tool errors -> model
                out = {"error": f"{type(e).__name__}: {e}"}
            audit("tool:" + b.name, b.input, "agent-initiated")
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})
    return "Stopped after max turns."


# ===========================================================================
# OFFLINE SELF-TEST  (no network, no API key)
# ===========================================================================
def _fake_player(pid, name, slot, eligible, proj, status="ACTIVE"):
    return {"playerPoolEntry": {"player": {
        "id": pid, "fullName": name, "eligibleSlots": eligible,
        "injuryStatus": status,
        "stats": [{"scoringPeriodId": 0, "statSourceId": 1, "appliedTotal": proj}],
    }}, "lineupSlotId": slot}


_MY_SWID = "{ABCD-1234-OWNER}"


def selftest():
    global BENCH_SLOT_ID, IL_SLOT_ID, AGENT_TEAM_ID, SWID
    BENCH_SLOT_ID, IL_SLOT_ID, AGENT_TEAM_ID = 16, 17, None
    SWID = _MY_SWID
    slot_counts = {"0": 1, "1": 1, "5": 2, "16": 3, "17": 1}  # C,1B,2xOF,bench,IL
    C, FB, OF = [0], [1], [5]

    me_team = {"roster": {"entries": [
        _fake_player(1, "Catcher A", 16, C, 300),      # stuck on bench
        _fake_player(2, "Slumping C", 0, C, 120),      # weak active C
        _fake_player(3, "OF Star", 5, OF, 420),
        _fake_player(4, "OF Mid", 16, OF, 260),
        _fake_player(5, "OF Bench", 16, OF, 240),
        _fake_player(6, "1B Hurt", 1, FB, 350, status="OUT"),
        _fake_player(7, "1B Sub", 16, FB, 180),
        # healed but still parked on IL — must be flagged and moved off
        _fake_player(8, "SS Healed", 17, [4], 330, status="ACTIVE"),
    ]}}
    them_team = {"roster": {"entries": [
        _fake_player(20, "Their C", 0, C, 280),
        _fake_player(21, "Their 1B", 1, FB, 410),
        _fake_player(22, "Their Spare 1B", 16, FB, 300),  # blocked behind 410: surplus
        _fake_player(23, "Their OF1", 5, OF, 200),
        _fake_player(24, "Their OF2", 5, OF, 190),
    ]}}
    fas = [{"id": 30, "fullName": "FA Slugger", "eligibleSlots": OF,
            "stats": [{"scoringPeriodId": 0, "statSourceId": 1, "appliedTotal": 310}]},
           {"id": 31, "fullName": "FA Scrub", "eligibleSlots": C,
            "stats": [{"scoringPeriodId": 0, "statSourceId": 1, "appliedTotal": 90}]}]

    me = roster_players(me_team)
    them = roster_players(them_team)

    print("=== TEAM RESOLUTION (SWID -> team id) ===")
    resolve_fixture = {"teams": [{"id": 1, "owners": [_MY_SWID]},
                                 {"id": 2, "owners": ["{OTHER-OWNER}"]}]}
    rid = resolve_team_id(resolve_fixture, SWID)
    assert rid == 1, rid
    # braces/case differences must still match
    assert resolve_team_id(resolve_fixture, "abcd-1234-owner") == 1
    print(f"  SWID auto-resolves to team {rid}")

    print("\n=== IL WATCH (healed players still on IL) ===")
    returns = il_returns(me)
    assert [r["name"] for r in returns] == ["SS Healed"], returns
    for r in returns:
        print(f"  {r['name']} is {r['status']} again — move off IL slot")

    print("\n=== LINEUP OPTIMIZER ===")
    moves = optimize_lineup(me, slot_counts)
    # the healed IL player must be routed off the IL slot
    assert any(m["name"] == "SS Healed" and m["fromLineupSlotId"] == IL_SLOT_ID
               for m in moves), "healed IL player not moved off IL"
    for m in moves:
        print(f"  move {m['name']}: slot {m['fromLineupSlotId']} -> {m['toLineupSlotId']}")
    print(f"  starters value: {starters_value(me, slot_counts):.0f}")

    print("\n=== WAIVERS ===")
    for w in waiver_targets(me, [{**f} for f in fas]):
        print(f"  ADD {w['add']['name']} / DROP {w['drop']['name']}  (+{w['gain']})")

    print("\n=== TRADE PROPOSALS (multi-package, surplus-aware) ===")
    pkgs = propose_trades(me, them, slot_counts)
    if pkgs:
        for t in pkgs:
            print(f"  give {t['i_give']} -> get {t['i_get']}  "
                  f"(me +{t['my_gain']}, them +{t['their_gain']}, score {t['score']})")
    else:
        print("  no win-win found")

    print("\n=== EVALUATE INCOMING OFFER ===")
    offer = {"they_give": [p for p in them if p["name"] == "Their 1B"],
             "they_get": [p for p in me if p["name"] == "OF Star"]}
    print(f"  {evaluate_offer(offer, me, slot_counts)}")

    print("\n=== DECLINED TRADE -> COUNTER OR ABANDON ===")
    declined = {"with_team_id": 2, "give_player_ids": [5], "get_player_ids": [22]}
    resp = respond_to_decline(declined, me, them, slot_counts)
    assert resp["decision"] in ("counter", "abandon"), resp
    print(f"  decision: {resp['decision']} — {resp['reason']}")

    print("\n=== TRANSACTION ITEM BUILDERS ===")
    ad = build_add_drop_items(1, add_player_id=30, drop_player_id=2)
    assert ad == [{"playerId": 30, "type": "ADD", "toTeamId": 1},
                  {"playerId": 2, "type": "DROP", "fromTeamId": 1}], ad
    tr = build_trade_items(1, 2, give_player_ids=[5], get_player_ids=[22])
    assert tr == [{"playerId": 5, "type": "TRADE", "fromTeamId": 1, "toTeamId": 2},
                  {"playerId": 22, "type": "TRADE", "fromTeamId": 2,
                   "toTeamId": 1}], tr
    print(f"  add/drop -> {json.dumps(ad)}")
    print(f"  trade    -> {json.dumps(tr)}")

    print("\n=== AGENT TOOLSET (offline dispatch over fixture) ===")
    league = {"scoringPeriodId": 100,
              "settings": {"rosterSettings": {"lineupSlotCounts": slot_counts}},
              "teams": [{"id": 1, "owners": [_MY_SWID], **me_team},
                        {"id": 2, "owners": ["{OTHER-OWNER}"], **them_team}]}

    # a trade I proposed (give OF Bench=5 -> get Their Spare 1B=22) was declined
    declined_txn = [{
        "type": "TRADE_PROPOSAL", "status": "DECLINED",
        "teamId": 1, "proposingTeamId": 1, "toTeamId": 2,
        "items": [{"playerId": 5, "type": "TRADE", "fromTeamId": 1,
                   "toTeamId": 2},
                  {"playerId": 22, "type": "TRADE", "fromTeamId": 2,
                   "toTeamId": 1}]}]

    class FakeClient:
        def league(self): return league
        def settings(self): return league["settings"]
        def free_agents(self, limit=200): return fas
        def transactions(self): return declined_txn
        def set_lineup(self, *a, **k): return {"committed": False, "dry_run": True}
        def submit_transaction(self, *a, **k): return {"committed": False,
                                                       "dry_run": True}

    sess = AgentSession(FakeClient())
    calls = [("list_teams", {}),
             ("get_roster", {"team": "2"}),
             ("value_player", {"player_id": 3}),
             ("check_il", {}),
             ("optimize_lineup", {}),
             ("find_waivers", {}),
             ("scout_trades", {}),
             ("evaluate_offer", {"they_give_ids": [21], "they_get_ids": [3]}),
             ("review_declined_trades", {}),
             ("execute", {"action": {"type": "lineup", "moves": [],
                                     "reasoning": "selftest"}}),
             ("execute", {"action": {"type": "add_drop", "add_player_id": 30,
                                     "drop_player_id": 2,
                                     "reasoning": "selftest add/drop"}}),
             ("execute", {"action": {"type": "trade_propose", "with_team_id": 2,
                                     "give_player_ids": [5],
                                     "get_player_ids": [22],
                                     "reasoning": "selftest trade"}})]
    for name, kw in calls:
        out = getattr(sess, name)(**kw)
        s = json.dumps(out)
        print(f"  {name:<15} -> {s[:90]}{'...' if len(s) > 90 else ''}")
    # list_teams must reflect the SWID-resolved team id, not a hardcoded one
    assert sess.list_teams()["me"] == 1, sess.list_teams()

    print("\nSelf-test OK — deterministic core + agent toolset run.")


# ===========================================================================
# CONFIG INTROSPECTION
# ===========================================================================
def show_config():
    """Print every resolved config value (secrets masked) and where it can be
    set. Handy for confirming env/.env/json overrides took effect."""
    def mask(v):
        s = str(v)
        return (s[:3] + "…" + s[-2:]) if len(s) > 8 else "set"
    rows = [
        ("LEAGUE_ID", LEAGUE_ID), ("SEASON", SEASON), ("GAME", GAME),
        ("AGENT_TEAM_ID", AGENT_TEAM_ID),
        ("BENCH_SLOT_ID", BENCH_SLOT_ID), ("IL_SLOT_ID", IL_SLOT_ID),
        ("UNAVAILABLE_STATUSES", ",".join(sorted(UNAVAILABLE_STATUSES))),
        ("ESPN_S2", mask(ESPN_S2) if ESPN_S2 else "(missing)"),
        ("SWID", mask(SWID) if SWID else "(missing)"),
        ("MODEL", MODEL), ("COMMIT_WRITES", COMMIT_WRITES),
        ("AUDIT_FILE", AUDIT_FILE), ("HTTP_RETRIES", HTTP_RETRIES),
        ("FREE_AGENT_LIMIT", FREE_AGENT_LIMIT),
        ("USE_SCOUTING", USE_SCOUTING), ("SCOUTING_WEIGHT", SCOUTING_WEIGHT),
        ("SEASON_GAMES", SEASON_GAMES),
        ("MAX_TRADES", MAX_TRADES), ("MAX_WAIVERS", MAX_WAIVERS),
        ("WAIVER_MARGIN", WAIVER_MARGIN),
        ("TRADE_BALANCE_TOL", TRADE_BALANCE_TOL),
        ("TRADE_FAIRNESS_RATIO", TRADE_FAIRNESS_RATIO),
        ("TRADE_MAX_PER_SIDE", TRADE_MAX_PER_SIDE),
        ("OFFER_ACCEPT_DELTA", OFFER_ACCEPT_DELTA),
        ("OFFER_REJECT_DELTA", OFFER_REJECT_DELTA),
        ("SMTP_HOST", SMTP_HOST or "(unset)"), ("SMTP_PORT", SMTP_PORT),
        ("SMTP_USER", SMTP_USER or "(unset)"),
        ("SMTP_PASS", mask(SMTP_PASS) if SMTP_PASS else "(unset)"),
        ("EMAIL_FROM", EMAIL_FROM or "(unset)"),
        ("EMAIL_TO", EMAIL_TO or "(unset)"),
        ("READ_BASE", READ_BASE), ("WRITE_BASE", WRITE_BASE),
    ]
    print("Resolved config (override via BBAGENT_<KEY> env / .env / "
          "bbagent.local.json):\n")
    for k, v in rows:
        print(f"  {k:<22} = {v}")


# ===========================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd",
                    choices=["discover", "selftest", "plan", "run", "config"])
    ap.add_argument("goal", nargs="?", default="Manage my team to win this week.")
    ap.add_argument("--execute", action="store_true",
                    help="plan: act on the plan (lineup, waivers, trade offers) "
                         "instead of only printing it")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan: force print-only even with --execute (no actions)")
    ap.add_argument("--commit", action="store_true",
                    help="actually POST writes to ESPN (otherwise every write "
                         "is simulated and the payload is returned)")
    ap.add_argument("--email", action="store_true",
                    help="plan: force-send the email summary (otherwise it auto-"
                         "sends only when SMTP is configured)")
    ap.add_argument("--no-email", action="store_true",
                    help="plan: never send the email summary")
    ap.add_argument("--no-scouting", action="store_true",
                    help="plan: skip the MLB-StatsAPI source, value on ESPN alone")
    ap.add_argument("--scouting-weight", type=float, default=None,
                    help="plan: external source's share of the blend (0..1, "
                         f"default {SCOUTING_WEIGHT})")
    ap.add_argument("--max-trades", type=int, default=None,
                    help=f"plan: trade offers per pass (default {MAX_TRADES})")
    ap.add_argument("--max-waivers", type=int, default=None,
                    help=f"plan: add/drops per pass (default {MAX_WAIVERS})")
    args = ap.parse_args()

    global COMMIT_WRITES
    COMMIT_WRITES = args.commit

    if args.cmd == "selftest":
        selftest(); return
    if args.cmd == "config":
        show_config(); return

    execute = args.execute and not args.dry_run
    if execute and COMMIT_WRITES:
        print("!! COMMIT_WRITES is ON — real transactions (including trade "
              "OFFERS other owners will see) will be POSTed to ESPN. Verify the "
              "write payload once first (see README).\n", file=sys.stderr)
    elif execute:
        print("-- --execute set without --commit: simulating writes "
              "(dry-run payloads returned, nothing POSTed).\n", file=sys.stderr)

    try:
        client = EspnClient()
        if args.cmd == "discover":
            discover(client)
        elif args.cmd == "plan":
            email = True if args.email else (False if args.no_email else None)
            print(json.dumps(plan(
                client, execute=execute, email=email,
                use_scouting=(False if args.no_scouting else None),
                scouting_weight=args.scouting_weight,
                max_trades=args.max_trades, max_waivers=args.max_waivers),
                indent=2))
        elif args.cmd == "run":
            print(run_agent(args.goal, client))
    except AuthError as e:
        sys.exit(f"Auth error: {e}")


if __name__ == "__main__":
    main()
