#!/usr/bin/env python3
"""
scouting.py — outside data source for valuation (MLB StatsAPI)
==============================================================

ESPN's own projections are mediocre, and they're what valuation falls back to.
This module brings in an OUTSIDE source — MLB's official StatsAPI
(statsapi.mlb.com): open, no API key, no scraping — pulls each player's live
season production, projects it rest-of-season by pace, scores those numbers
under YOUR league's actual rules (via projections.py), matches them to ESPN
player ids, and feeds espn_agent.plug_projections().

Why this source: it's the ground truth for what players have actually done this
season, which is a far better basis for ROS value than ESPN's preseason guesses
— especially mid-season (it's June). FanGraphs / other projection sites block
programmatic access (403/Cloudflare); MLB StatsAPI does not.

Projection model (deliberately simple and defensible):
    ROS_stat = season_stat_so_far * (team_games_remaining / team_games_played)
i.e. assume each player continues at their established season pace (durability
included) for the games left. team_games_played is estimated from the busiest
hitter's gamesPlayed; remaining = 162 - that.

Pipeline:
    from espn_agent import EspnClient, plug_projections
    import scouting
    client = EspnClient()
    overrides, info = scouting.build_overrides(client)
    plug_projections(overrides)
    print(info)            # {'source','matched','unmatched','games_remaining',...}

Offline check:  python scouting.py selftest
Live preview :  python scouting.py            (prints top-valued players)
"""

import sys

import projections as proj

STATSAPI = "https://statsapi.mlb.com/api/v1/stats"
_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
       "Accept": "application/json"}


def _season_games():
    """Games in a full season — honor the configured override if present."""
    try:
        import espn_agent
        return espn_agent.SEASON_GAMES
    except Exception:
        return 162

# MLB StatsAPI stat field -> the CSV column name projections.py expects.
_HITTER_FIELD_TO_COLUMN = {
    "totalBases": "TB", "baseOnBalls": "BB", "runs": "R", "rbi": "RBI",
    "stolenBases": "SB", "strikeOuts": "SO",
}
_PITCHER_FIELD_TO_COLUMN = {
    "inningsPitched": "IP", "hits": "H", "baseOnBalls": "BB",
    "earnedRuns": "ER", "strikeOuts": "SO", "wins": "W", "losses": "L",
    "saves": "SV", "holds": "HLD",
}


# ===========================================================================
# MLB STATSAPI FETCH
# ===========================================================================
def _ip_to_float(ip):
    """MLB innings pitched '93.1' means 93 + 1/3 (not 93.1). Convert properly."""
    try:
        whole, _, frac = str(ip).partition(".")
        return int(whole or 0) + (int(frac or 0) / 3.0)
    except (TypeError, ValueError):
        return 0.0


def fetch_season_stats(season, group, limit=2000, _http=None):
    """Return the StatsAPI season-stat splits for 'hitting' or 'pitching'.
    _http lets the self-test inject a fake without network."""
    if _http is not None:
        return _http(group)
    import requests
    r = requests.get(STATSAPI, params={
        "stats": "season", "group": group, "season": season,
        "gameType": "R", "sportId": 1, "limit": limit},
        headers=_UA, timeout=30)
    r.raise_for_status()
    blocks = r.json().get("stats", [])
    return blocks[0].get("splits", []) if blocks else []


def _estimate_team_games(hitting_splits):
    """Busiest hitter's gamesPlayed ~= how many games teams have played."""
    games = [int(s.get("stat", {}).get("gamesPlayed", 0) or 0)
             for s in hitting_splits]
    return max(games) if games else 0


# ===========================================================================
# ROS PROJECTION -> league-scored value rows
# ===========================================================================
def _project_rows(splits, field_map, ros_multiplier):
    """Turn StatsAPI splits into projections.py-style rows: each stat scaled to
    rest-of-season and keyed by the league CSV column name (uppercase), with a
    _name for matching."""
    rows = []
    for s in splits:
        stat = s.get("stat", {})
        name = s.get("player", {}).get("fullName")
        if not name:
            continue
        row = {"_name": name}
        for field, col in field_map.items():
            raw = (_ip_to_float(stat.get(field))
                   if field == "inningsPitched"
                   else proj._to_float(stat.get(field)))
            row[col.upper()] = raw * ros_multiplier
        rows.append(row)
    return rows


# ===========================================================================
# PIPELINE
# ===========================================================================
def build_overrides(client, season=None, _http=None):
    """Pull MLB season stats -> ROS projection -> league scoring -> ESPN ids.

    Returns ({espn_id: ros_points}, info) where info reports the source, match
    counts, the games-remaining basis, and a sample of unmatched names so you
    can see coverage at a glance.
    """
    import espn_agent as core
    season = season or core.SEASON

    hitting = fetch_season_stats(season, "hitting", _http=_http)
    pitching = fetch_season_stats(season, "pitching", _http=_http)

    team_games = _estimate_team_games(hitting)
    remaining = max(0, _season_games() - team_games)
    # if the season hasn't started (no games), fall back to full-season pace=1
    mult = (remaining / team_games) if team_games else 1.0

    scoring_map = proj.load_scoring_map(client)
    league = client.league()
    espn_players = [e["playerPoolEntry"]["player"]
                    for t in league["teams"]
                    for e in t.get("roster", {}).get("entries", [])]
    espn_players += list(client.free_agents())
    index = proj.build_espn_index(espn_players)

    overrides, unmatched_all = {}, []
    for splits, field_map, col_map in (
            (hitting, _HITTER_FIELD_TO_COLUMN, proj.HITTER_STATID_TO_COLUMN),
            (pitching, _PITCHER_FIELD_TO_COLUMN, proj.PITCHER_STATID_TO_COLUMN)):
        rows = _project_rows(splits, field_map, mult)
        matched, unmatched = proj.match_rows(rows, index)
        for pid, row in matched.items():
            overrides[pid] = round(
                overrides.get(pid, 0.0)
                + proj.score_row(row, scoring_map, col_map), 1)
        unmatched_all += unmatched

    info = {
        "source": "MLB StatsAPI (statsapi.mlb.com)",
        "season": season,
        "team_games_played_est": team_games,
        "games_remaining": remaining,
        "ros_multiplier": round(mult, 3),
        "hitters_seen": len(hitting),
        "pitchers_seen": len(pitching),
        "matched": len(overrides),
        "unmatched": len(unmatched_all),
        "unmatched_sample": unmatched_all[:15],
    }
    return overrides, info


# ===========================================================================
# OFFLINE SELF-TEST  (no network)
# ===========================================================================
def _selftest():
    # fake StatsAPI splits: one hitter, one pitcher, ~half a season played
    fake = {
        "hitting": [
            {"player": {"fullName": "José Ramírez"},
             "stat": {"gamesPlayed": 81, "totalBases": 140, "baseOnBalls": 30,
                      "runs": 48, "rbi": 50, "stolenBases": 20,
                      "strikeOuts": 40}},
            {"player": {"fullName": "Bench Guy"},
             "stat": {"gamesPlayed": 40, "totalBases": 30, "baseOnBalls": 8,
                      "runs": 12, "rbi": 14, "stolenBases": 1,
                      "strikeOuts": 35}},
        ],
        "pitching": [
            {"player": {"fullName": "Spencer Strider"},
             "stat": {"inningsPitched": "90.1", "hits": 70, "baseOnBalls": 22,
                      "earnedRuns": 30, "strikeOuts": 130, "wins": 8,
                      "losses": 4, "saves": 0, "holds": 0}},
        ],
    }
    http = lambda group: fake[group]

    # league scoring map + a fake client exposing what build_overrides needs
    scoring = {"8": 1.0, "10": 1.0, "20": 1.0, "21": 1.0, "23": 1.0, "27": -1.0,
               "34": 1.0, "37": -1.0, "39": -1.0, "45": -2.0, "48": 1.0,
               "53": 2.0, "54": -2.0, "57": 5.0, "60": 2.0}

    def _pp(pid, name):
        return {"playerPoolEntry": {"player": {"id": pid, "fullName": name}}}

    class FakeClient:
        def settings(self):
            return {"scoringSettings": {"scoringItems": [
                {"statId": int(k), "points": v} for k, v in scoring.items()]}}
        def league(self):
            return {"teams": [{"roster": {"entries": [
                _pp(101, "José Ramírez"), _pp(201, "Spencer Strider")]}}]}
        def free_agents(self, limit=200):
            return [{"id": 999, "fullName": "Bench Guy"}]

    import espn_agent as core
    core.SEASON = 2026
    overrides, info = build_overrides(FakeClient(), _http=http)

    # team games estimated from busiest hitter (81), remaining = 81, mult = 1.0
    assert info["team_games_played_est"] == 81, info
    assert info["games_remaining"] == 81, info
    assert abs(info["ros_multiplier"] - 1.0) < 1e-9, info
    # all three names matched to ESPN ids
    assert info["matched"] == 3 and info["unmatched"] == 0, info

    # José Ramírez ROS (mult 1.0): TB140+BB30+R48+RBI50+SB20-SO40 = 248
    assert overrides[101] == 248.0, overrides[101]
    # Strider ROS: IP90.33 -H70 -BB22 -2*ER30(60) +K130 +2*W8(16) -2*L4(8)
    #   = 90.33 - 70 - 22 - 60 + 130 + 16 - 8 = 76.33
    assert abs(overrides[201] - 76.3) < 0.2, overrides[201]
    # accent/diacritic matching worked (José -> jose)
    assert 101 in overrides

    print("scouting self-test OK")
    print(f"  source           : {info['source']}")
    print(f"  ROS multiplier   : {info['ros_multiplier']} "
          f"({info['games_remaining']}/{info['team_games_played_est']} games)")
    print(f"  matched/unmatched: {info['matched']}/{info['unmatched']}")
    print(f"  José Ramírez ROS : {overrides[101]}")
    print(f"  Strider ROS      : {overrides[201]}")


def _live_preview():
    import espn_agent as core
    client = core.EspnClient()
    overrides, info = build_overrides(client)
    print("Scouting source:", info["source"])
    for k, v in info.items():
        if k not in ("source", "unmatched_sample"):
            print(f"  {k}: {v}")
    if info["unmatched_sample"]:
        print("  unmatched sample:", info["unmatched_sample"])
    # show the top-valued players by the outside source
    idx = {}
    league = client.league()
    for t in league["teams"]:
        for e in t.get("roster", {}).get("entries", []):
            p = e["playerPoolEntry"]["player"]
            idx[p["id"]] = p.get("fullName")
    for f in client.free_agents():
        idx[f.get("id")] = f.get("fullName")
    top = sorted(overrides.items(), key=lambda kv: kv[1], reverse=True)[:20]
    print("\nTop 20 ROS values (MLB-StatsAPI based):")
    for pid, val in top:
        print(f"  {val:>7}  {idx.get(pid, pid)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        _live_preview()
