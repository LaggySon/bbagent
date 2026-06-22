#!/usr/bin/env python3
"""
projections.py — turn external projections into league-specific point values
============================================================================

Feeds espn_agent.plug_projections() with {espn_player_id: ros_fantasy_points}
computed under YOUR league's actual scoring (decoded from mSettings).

Target league: 14-team H2H points, total-bases based (no HR/AVG scored).
Hitter and pitcher stats are scored on separate maps below — this avoids the
BB / SO column collision between hitting and pitching.

CONFIRM TWO before trusting valuation (check ESPN > Settings > Scoring):
  - statId 8  (+1): read as TB (total bases)
  - statId 27 (-1): read as SO (batter strikeouts) — could be CS

Pipeline (hitters + pitchers are usually separate CSV exports):
    from espn_agent import EspnClient, plug_projections
    import projections as proj
    client = EspnClient()
    overrides, unmatched = proj.build_overrides(
        client, hitters_csv="hitters.csv", pitchers_csv="pitchers.csv")
    plug_projections(overrides)
    print("fix these:", unmatched)

Offline check:  python projections.py selftest
"""

import csv
import sys
import unicodedata

# ---------------------------------------------------------------------------
# LEAGUE SCORING -> CSV COLUMN MAPS  (pre-filled for this league's rules)
# Keys = ESPN statId (string). Values = column header in your projection CSV.
# Hitters and pitchers are scored separately, so identical column names
# (BB, SO) on each side don't collide.
# ---------------------------------------------------------------------------
HITTER_STATID_TO_COLUMN = {
    "8":  "TB",    # +1  CONFIRM = total bases
    "10": "BB",    # +1  walks
    "20": "R",     # +1  runs
    "21": "RBI",   # +1  RBI
    "23": "SB",    # +1  stolen bases
    "27": "SO",    # -1  CONFIRM = batter strikeouts (vs caught stealing "CS")
}
PITCHER_STATID_TO_COLUMN = {
    "34": "IP",    # +1  innings pitched
    "37": "H",     # -1  hits allowed
    "39": "BB",    # -1  walks allowed
    "45": "ER",    # -2  earned runs
    "48": "SO",    # +1  strikeouts
    "53": "W",     # +2  wins
    "54": "L",     # -2  losses
    "57": "SV",    # +5  saves
    "60": "HLD",   # +2  holds
}

# Read-only helper for discover output.
_STATID_GUESS = {
    "8": "TB", "10": "BB(bat)", "20": "R", "21": "RBI", "23": "SB",
    "27": "SO(bat)/CS?", "34": "IP", "37": "H allowed", "39": "BB allowed",
    "45": "ER", "48": "K", "53": "W", "54": "L", "57": "SV", "60": "HLD",
}


# ===========================================================================
# NAME NORMALIZATION + MATCHING
# ===========================================================================
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def normalize_name(name):
    if not name:
        return ""
    if "," in name:                      # "Ramirez, Jose" -> "Jose Ramirez"
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    cleaned = []
    for tok in name.replace(".", " ").replace("'", "").split():
        if tok in _SUFFIXES:
            continue
        cleaned.append(tok)
    return " ".join(cleaned)


def build_espn_index(espn_players):
    idx = {}
    for p in espn_players:
        nm = normalize_name(p.get("fullName") or p.get("name"))
        if nm:
            idx[nm] = p["id"]
    return idx


def match_rows(rows, espn_index):
    matched, unmatched = {}, []
    for row in rows:
        pid = espn_index.get(normalize_name(row.get("_name")))
        if pid is not None:
            matched[pid] = row
        else:
            unmatched.append(row.get("_name"))
    return matched, unmatched


# ===========================================================================
# CSV LOADING
# ===========================================================================
def load_csv(path, name_column=None):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        upper = {h: h.strip().upper() for h in headers}
        if name_column is None:
            for cand in ("NAME", "PLAYER", "PLAYERNAME"):
                hit = next((h for h in headers if h.strip().upper() == cand), None)
                if hit:
                    name_column = hit
                    break
        if name_column is None:
            raise ValueError(f"No name column found in {path}; pass name_column=")
        for raw in reader:
            row = {upper[k]: (v.strip() if isinstance(v, str) else v)
                   for k, v in raw.items() if k in upper}
            row["_name"] = (raw.get(name_column) or "").strip()
            rows.append(row)
    return rows


# ===========================================================================
# SCORING
# ===========================================================================
def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def score_row(row, scoring_map, col_map):
    """Apply league scoring to one row using the given statId->column map.
    Negative point stats (ER, H, BB allowed, batter SO) work since points sum."""
    total = 0.0
    for stat_id, pts in scoring_map.items():
        col = col_map.get(str(stat_id))
        if not col:
            continue
        total += _to_float(row.get(col.upper())) * float(pts)
    return total


def load_scoring_map(client):
    s = client.settings()
    items = s.get("scoringSettings", {}).get("scoringItems", [])
    out = {}
    for it in items:
        if it.get("points") is not None:
            out[str(it.get("statId"))] = it["points"]
    return out


def discover_scoring(client):
    sm = load_scoring_map(client)
    if not sm:
        print("No scoringItems found.")
        return
    print("Scored stats (statId | points | guess):")
    for sid, pts in sorted(sm.items(), key=lambda x: int(x[0])):
        print(f"  {sid:>4} | {pts:>6} | {_STATID_GUESS.get(sid, '??? VERIFY')}")


# ===========================================================================
# PIPELINE
# ===========================================================================
def build_overrides(client, hitters_csv=None, pitchers_csv=None, name_column=None):
    """Load hitter/pitcher CSVs -> match to ESPN ids -> score per league rules.
    Returns ({espn_id: ros_points}, unmatched_names)."""
    scoring_map = load_scoring_map(client)
    league = client.league()
    espn_players = [e["playerPoolEntry"]["player"]
                    for t in league["teams"]
                    for e in t.get("roster", {}).get("entries", [])]
    espn_players += list(client.free_agents())
    index = build_espn_index(espn_players)

    overrides, unmatched_all = {}, []
    for path, col_map in ((hitters_csv, HITTER_STATID_TO_COLUMN),
                          (pitchers_csv, PITCHER_STATID_TO_COLUMN)):
        if not path:
            continue
        matched, unmatched = match_rows(load_csv(path, name_column), index)
        for pid, row in matched.items():
            overrides[pid] = round(
                overrides.get(pid, 0.0) + score_row(row, scoring_map, col_map), 1)
        unmatched_all += unmatched
    return overrides, unmatched_all


# ===========================================================================
# OFFLINE SELF-TEST
# ===========================================================================
def selftest():
    assert normalize_name("José Ramírez") == "jose ramirez"
    assert normalize_name("Ramirez, Jose") == "jose ramirez"
    assert normalize_name("Ronald Acuña Jr.") == "ronald acuna"

    espn = [{"id": 101, "fullName": "José Ramírez"},
            {"id": 201, "fullName": "Spencer Strider"}]
    idx = build_espn_index(espn)

    # league scoring map (this league)
    scoring = {"8": 1.0, "10": 1.0, "20": 1.0, "21": 1.0, "23": 1.0, "27": -1.0,
               "34": 1.0, "37": -1.0, "39": -1.0, "45": -2.0, "48": 1.0,
               "53": 2.0, "54": -2.0, "57": 5.0, "60": 2.0}

    # hitter: TB 280, BB 60, R 95, RBI 100, SB 20, SO 120
    hrow = {"TB": "280", "BB": "60", "R": "95", "RBI": "100", "SB": "20",
            "SO": "120", "_name": "Ramirez, Jose"}
    hpts = score_row(hrow, scoring, HITTER_STATID_TO_COLUMN)
    assert hpts == 280 + 60 + 95 + 100 + 20 - 120, hpts          # 435
    # pitcher SHARES column names BB/SO but must NOT pick up hitter scoring:
    # IP 180, H 150, BB 45, ER 65, SO 220, W 15, L 8, SV 0, HLD 0
    prow = {"IP": "180", "H": "150", "BB": "45", "ER": "65", "SO": "220",
            "W": "15", "L": "8", "SV": "0", "HLD": "0", "_name": "Spencer Strider"}
    ppts = score_row(prow, scoring, PITCHER_STATID_TO_COLUMN)
    expected = 180 - 150 - 45 + (-2 * 65) + 220 + (2 * 15) + (-2 * 8)
    assert ppts == expected, (ppts, expected)                    # 89
    # collision guard: pitcher BB(45) scored as -1 (allowed), not +1 (drawn)
    assert ppts < 180 + 220, "pitcher BB/SO must use pitcher map only"

    print(f"projections self-test OK — hitter {hpts:.0f}, pitcher {ppts:.0f}, "
          "no hitter/pitcher column collision.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        print("usage: python projections.py selftest")
