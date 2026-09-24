"""ESPN Fantasy Football MCP server.

Read-only access to one or more ESPN fantasy football leagues over the Model
Context Protocol: standings, rosters, matchups, box scores, the waiver wire,
player lookups, transactions, draft results, and trade research.

ESPN has no public, documented fantasy API. This talks to the same private
endpoints the ESPN web app uses, so shapes can change without notice. Every
parser here is defensive for that reason.

Configuration comes from environment variables.

Single league:

    ESPN_LEAGUE_ID   the numeric league id from the ESPN URL
    ESPN_SEASON      optional, defaults to the current NFL season year
    ESPN_S2          required for private leagues, the espn_s2 cookie
    ESPN_SWID        required for private leagues, the SWID cookie

Multiple leagues, either as JSON:

    ESPN_LEAGUES='{"work": "123456", "friends": {"id": "789012", "season": 2026}}'

or as a shorthand list when every league shares one ESPN account:

    ESPN_LEAGUES='work=123456,friends=789012,family=345678'

Per-league entries may override "season", "sport", "s2", and "swid". Anything
they omit falls back to the single-league variables above.

    ESPN_CACHE_TTL   optional, seconds to cache responses (default 60)
"""

from __future__ import annotations

import datetime as _dt
import functools
import inspect
import json
import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import httpx

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Static ESPN maps
# ---------------------------------------------------------------------------

# player.defaultPositionId
POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "D/ST"]

# rosterEntry.lineupSlotId
SLOTS = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE",
    21: "IR", 23: "FLEX", 24: "ER",
}

STARTING_SLOTS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 23}

# Dedicated starting slot for each position, used to judge roster depth.
POSITION_SLOT = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "K": 17, "D/ST": 16}

# Position name -> lineupSlotIds used when filtering the player pool
SLOT_FILTERS = {
    "QB": [0], "RB": [2], "WR": [4], "TE": [6], "K": [17],
    "D/ST": [16], "DST": [16], "DEF": [16], "FLEX": [23],
}

PRO_TEAMS = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
    20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
    26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
    34: "HOU",
}

INJURY_SHORT = {
    "ACTIVE": "", "NORMAL": "", "QUESTIONABLE": "Q", "DOUBTFUL": "D",
    "OUT": "OUT", "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP",
    "DAY_TO_DAY": "DTD", "PROBABLE": "P",
}

TRANSACTION_LABELS = {
    "WAIVER": "Waiver claim", "FREEAGENT": "Free agent pickup",
    "TRADE_ACCEPT": "Trade", "TRADE_PROPOSAL": "Trade proposal",
    "ROSTER": "Lineup change", "DRAFT": "Draft pick",
}


class ESPNError(RuntimeError):
    """Anything that went wrong talking to ESPN, phrased for a human."""


# ---------------------------------------------------------------------------
# League configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class League:
    alias: str
    league_id: str
    season: int
    sport: str = "ffl"
    s2: str = ""
    swid: str = ""


def _default_season() -> int:
    """NFL seasons are labelled by the year they kick off in."""
    today = _dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def _normalize_swid(value: str) -> str:
    value = (value or "").strip()
    if value and not value.startswith("{"):
        value = "{" + value.strip("{}") + "}"
    return value


def _parse_leagues_env(raw: str) -> dict[str, Any]:
    """Accept either JSON or the 'alias=id,alias=id' shorthand."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ESPNError(f"ESPN_LEAGUES is not valid JSON: {exc}") from exc
    parsed: dict[str, Any] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        alias, _, league_id = chunk.partition("=")
        parsed[(alias if league_id else chunk).strip()] = (league_id or chunk).strip()
    return parsed


def _load_leagues() -> dict[str, League]:
    season = int(os.environ.get("ESPN_SEASON") or _default_season())
    sport = (os.environ.get("ESPN_SPORT") or "ffl").strip()
    s2 = os.environ.get("ESPN_S2", "").strip()
    swid = _normalize_swid(os.environ.get("ESPN_SWID", ""))

    leagues: dict[str, League] = {}
    for alias, value in _parse_leagues_env(os.environ.get("ESPN_LEAGUES", "")).items():
        if isinstance(value, (str, int)):
            value = {"id": value}
        if not isinstance(value, dict):
            continue
        key = str(alias).strip().lower()
        league_id = str(value.get("id") or value.get("league_id") or "").strip()
        if not key or not league_id:
            continue
        leagues[key] = League(
            alias=key,
            league_id=league_id,
            season=int(value.get("season") or season),
            sport=str(value.get("sport") or sport),
            s2=str(value.get("s2") or value.get("espn_s2") or s2).strip(),
            swid=_normalize_swid(str(value.get("swid") or value.get("SWID") or swid)),
        )

    single = os.environ.get("ESPN_LEAGUE_ID", "").strip()
    if single and not any(lg.league_id == single for lg in leagues.values()):
        alias = (os.environ.get("ESPN_LEAGUE_ALIAS") or "default").strip().lower()
        leagues[alias] = League(alias, single, season, sport, s2, swid)
    return leagues


LEAGUES = _load_leagues()
CACHE_TTL = float(os.environ.get("ESPN_CACHE_TTL") or 60)


def _resolve(alias: str | None) -> League:
    """Map a league argument to a configured league."""
    if not LEAGUES:
        raise ESPNError(
            "No leagues are configured. Set ESPN_LEAGUE_ID for one league, or "
            "ESPN_LEAGUES for several, then restart Claude."
        )
    names = ", ".join(sorted(LEAGUES))
    if alias:
        key = alias.strip().lower()
        if key in LEAGUES:
            return LEAGUES[key]
        for league in LEAGUES.values():
            if league.league_id == alias.strip():
                return league
        raise ESPNError(f"Unknown league '{alias}'. Configured leagues: {names}.")
    if len(LEAGUES) == 1:
        return next(iter(LEAGUES.values()))
    raise ESPNError(
        f"More than one league is configured, so the league argument is required. "
        f"Pass one of: {names}. Use list_leagues for details."
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

_HOST = "https://lm-api-reads.fantasy.espn.com/apis/v3/games"
_cache: dict[str, tuple[float, Any]] = {}


def _headers(league: League, fantasy_filter: dict | None = None) -> dict[str, str]:
    headers = {"User-Agent": "espn-fantasy-mcp/1.1", "Accept": "application/json"}
    cookies = []
    if league.s2:
        cookies.append(f"espn_s2={league.s2}")
    if league.swid:
        cookies.append(f"SWID={league.swid}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    if fantasy_filter:
        headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter)
    return headers


async def _fetch(league: League, url: str, params: dict,
                 fantasy_filter: dict | None = None) -> Any:
    key = json.dumps([league.alias, url, params, fantasy_filter], sort_keys=True, default=str)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        try:
            response = await client.get(url, params=params, headers=_headers(league, fantasy_filter))
        except httpx.RequestError as exc:
            raise ESPNError(f"Could not reach ESPN: {exc}") from exc

    if response.status_code in (401, 403):
        raise ESPNError(
            f"ESPN returned 401/403 for league '{league.alias}'. That league is private, "
            "so it needs valid espn_s2 and SWID cookies from an account that is a member. "
            "Cookies expire roughly yearly, so refresh them if they used to work."
        )
    if response.status_code == 404:
        raise ESPNError(
            f"ESPN returned 404 for league '{league.alias}' (id {league.league_id}, "
            f"season {league.season}). Check the id, and check the season if you are "
            "asking about a past year."
        )
    if response.status_code >= 400:
        raise ESPNError(f"ESPN returned HTTP {response.status_code}: {response.text[:300]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise ESPNError(
            "ESPN returned a non-JSON response, which usually means the login was rejected."
        ) from exc

    # Historical seasons come back wrapped in a single-element list.
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]

    _cache[key] = (time.monotonic(), data)
    return data


async def _league_data(league: League, views: str | list[str], params: dict | None = None,
                       fantasy_filter: dict | None = None) -> dict:
    """Call the league endpoint with one or more ESPN views."""
    query: dict[str, Any] = dict(params or {})
    query["view"] = views if isinstance(views, list) else [views]
    url = f"{_HOST}/{league.sport}/seasons/{league.season}/segments/0/leagues/{league.league_id}"
    try:
        return await _fetch(league, url, query, fantasy_filter)
    except ESPNError:
        # Older seasons live behind the leagueHistory endpoint.
        history_url = f"{_HOST}/{league.sport}/leagueHistory/{league.league_id}"
        query["seasonId"] = league.season
        return await _fetch(league, history_url, query, fantasy_filter)


async def _all_players(league: League) -> list[dict]:
    """Full player index for the season. Cached hard because it is large."""
    key = f"players::{league.sport}::{league.season}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 21600:
        return hit[1]
    url = f"{_HOST}/{league.sport}/seasons/{league.season}/players"
    data = await _fetch(league, url, {"view": "players_wl", "scoringPeriodId": 0})
    players = data if isinstance(data, list) else data.get("players", [])
    _cache[key] = (time.monotonic(), players)
    return players


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _team_name(team: dict) -> str:
    name = (team.get("name") or "").strip()
    if not name:
        name = f"{team.get('location', '')} {team.get('nickname', '')}".strip()
    return name or f"Team {team.get('id', '?')}"


def _team_index(data: dict) -> dict[int, dict]:
    return {t["id"]: t for t in data.get("teams", []) if "id" in t}


def _owner_names(team: dict, members: list[dict]) -> str:
    by_id = {m.get("id"): m for m in members}
    names = []
    for owner_id in team.get("owners") or []:
        member = by_id.get(owner_id) or {}
        label = f"{member.get('firstName', '')} {member.get('lastName', '')}".strip()
        label = label or member.get("displayName", "")
        if label:
            names.append(label)
    return ", ".join(names)


def _record(team: dict) -> tuple[int, int, int, float, float]:
    overall = (team.get("record") or {}).get("overall") or {}
    return (
        int(overall.get("wins") or 0),
        int(overall.get("losses") or 0),
        int(overall.get("ties") or 0),
        float(overall.get("pointsFor") or team.get("points") or 0.0),
        float(overall.get("pointsAgainst") or 0.0),
    )


def _player_points(player: dict, week: int | None) -> tuple[float | None, float | None]:
    """Return (actual, projected) fantasy points for a week."""
    actual = projected = None
    for stat in player.get("stats") or []:
        if week is not None and stat.get("scoringPeriodId") != week:
            continue
        if stat.get("statSourceId") == 0:
            actual = stat.get("appliedTotal")
        elif stat.get("statSourceId") == 1:
            projected = stat.get("appliedTotal")
    return actual, projected


def _season_stats(player: dict) -> tuple[float | None, float | None]:
    """Return (season points to date, full-season projection)."""
    actual = projected = None
    for stat in player.get("stats") or []:
        if stat.get("statSplitTypeId") != 0:
            continue
        if stat.get("statSourceId") == 0:
            actual = stat.get("appliedTotal")
        elif stat.get("statSourceId") == 1:
            projected = stat.get("appliedTotal")
    return actual, projected


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _player_label(player: dict) -> str:
    name = player.get("fullName") or "Unknown player"
    pos = POSITIONS.get(player.get("defaultPositionId"), "?")
    team = PRO_TEAMS.get(player.get("proTeamId"), "FA")
    tag = INJURY_SHORT.get(player.get("injuryStatus") or "", "")
    label = f"{name} ({pos}, {team})"
    return f"{label} [{tag}]" if tag else label


def _roster_entries(team: dict) -> list[dict]:
    for key in ("roster", "rosterForCurrentScoringPeriod", "rosterForMatchupPeriod"):
        block = team.get(key)
        if isinstance(block, dict) and block.get("entries"):
            return block["entries"]
    return []


def _format_roster(entries: list[dict], week: int | None) -> str:
    starters, bench = [], []
    for entry in entries:
        slot = entry.get("lineupSlotId")
        player = (entry.get("playerPoolEntry") or {}).get("player") or {}
        actual, projected = _player_points(player, week)
        line = (
            f"  {SLOTS.get(slot, str(slot)):<5} {_player_label(player):<34} "
            f"proj {_num(projected):>6}   act {_num(actual):>6}"
        )
        (starters if slot in STARTING_SLOTS else bench).append((slot, line))

    starters.sort(key=lambda row: row[0])
    out = ["Starters:"] + [row[1] for row in starters]
    if bench:
        out += ["Bench / IR:"] + [row[1] for row in bench]
    return "\n".join(out)


def _current_week(data: dict) -> int:
    status = data.get("status") or {}
    for key in ("currentMatchupPeriod", "latestScoringPeriod"):
        value = status.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return int(data.get("scoringPeriodId") or 1)


def _starter_counts(data: dict) -> dict[str, int]:
    """How many starters each position requires, ignoring flex."""
    counts = (data.get("settings", {}).get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    required = {}
    for position, slot in POSITION_SLOT.items():
        required[position] = int(counts.get(str(slot)) or counts.get(slot) or 0)
    return required


def _flex_count(data: dict) -> int:
    counts = (data.get("settings", {}).get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    return int(counts.get("23") or counts.get(23) or 0)


def _position_groups(entries: list[dict], week: int | None) -> dict[str, list[dict]]:
    """Group a roster by position with the numbers trade talk needs."""
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        player = (entry.get("playerPoolEntry") or {}).get("player") or {}
        position = POSITIONS.get(player.get("defaultPositionId"), "?")
        season_actual, season_projected = _season_stats(player)
        _, week_projected = _player_points(player, week)
        groups.setdefault(position, []).append({
            "player": player,
            "slot": entry.get("lineupSlotId"),
            "season": season_actual,
            "rest_of_season": season_projected,
            "week": week_projected,
        })
    for rows in groups.values():
        rows.sort(key=lambda r: r["season"] if r["season"] is not None else -1, reverse=True)
    return groups


def _depth_note(count: int, required: int) -> str:
    if required == 0:
        return ""
    extra = count - required
    if extra >= 2:
        return f"surplus of {extra}"
    if extra <= 0:
        return "thin, no backup"
    return "one spare"


# ---------------------------------------------------------------------------
# Server and tools
# ---------------------------------------------------------------------------

# Older mcp 1.x releases of FastMCP don't accept a version argument.
_server_kwargs: dict[str, Any] = {}
if "version" in inspect.signature(_Server.__init__).parameters:
    _server_kwargs["version"] = "1.1.0"

mcp = _Server(
    name="espn-fantasy",
    **_server_kwargs,
    instructions=(
        "Read-only access to the user's ESPN fantasy football leagues. Call list_leagues "
        "first when more than one league is configured, and pass the league alias to every "
        "other tool. Call get_league_info for the current week and the team-id map, since "
        "team ids are small integers rather than names. Weeks are NFL scoring periods "
        "starting at 1. For trade work, compare_rosters and find_trade_targets supply the "
        "raw data; the analysis and the proposal itself are yours to write."
    ),
)


def _friendly_errors(func):
    """Return ESPN failures as readable text rather than an opaque tool error."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ESPNError as exc:
            return f"ESPN request failed: {exc}"
    return wrapper


@mcp.tool()
@_friendly_errors
async def list_leagues() -> str:
    """Every configured league, with its alias, id, season, and name.

    Call this first in a multi-league setup. The alias is what every other tool
    takes as its league argument.
    """
    if not LEAGUES:
        raise ESPNError(
            "No leagues are configured. Set ESPN_LEAGUE_ID for one league, or "
            "ESPN_LEAGUES for several."
        )
    lines = [f"{len(LEAGUES)} league(s) configured", ""]
    for alias in sorted(LEAGUES):
        league = LEAGUES[alias]
        try:
            data = await _league_data(league, "mSettings")
            name = (data.get("settings") or {}).get("name", "unknown")
            week = _current_week(data)
            detail = f"{name}, week {week}"
        except ESPNError as exc:
            detail = f"unreachable ({exc})"
        auth = "private" if league.s2 else "public or no cookies set"
        lines.append(f"  {alias:<12} id {league.league_id}, season {league.season}, {auth}")
        lines.append(f"  {'':<12} {detail}")
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_league_info(league: str | None = None) -> str:
    """League name, size, scoring format, roster slots, and the current week.

    Start here when you need the current week number or the mapping from team
    names to the team ids other tools take.

    Args:
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mSettings", "mTeam"])
    settings = data.get("settings") or {}
    scoring = settings.get("scoringSettings") or {}
    schedule = settings.get("scheduleSettings") or {}
    roster = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    members = data.get("members") or []

    reception_points = 0.0
    for item in scoring.get("scoringItems") or []:
        if item.get("statId") == 53:
            reception_points = float(item.get("points") or 0.0)
    ppr = {1.0: "Full PPR", 0.5: "Half PPR"}.get(reception_points, "Standard (no PPR)")

    slots = ", ".join(
        f"{SLOTS.get(int(slot), slot)} x{count}"
        for slot, count in sorted(roster.items(), key=lambda kv: int(kv[0]))
        if count
    )

    lines = [
        f"League: {settings.get('name', 'Unknown')} "
        f"(alias '{lg.alias}', id {lg.league_id}, season {lg.season})",
        f"Teams: {settings.get('size', len(data.get('teams', [])))}",
        f"Scoring: {scoring.get('scoringType', 'unknown')}, {ppr}",
        f"Current week: {_current_week(data)}",
        f"Regular season weeks: {schedule.get('matchupPeriodCount', '?')}, "
        f"playoff teams: {schedule.get('playoffTeamCount', '?')}",
        f"Lineup: {slots or 'unknown'}",
        "",
        "Teams:",
    ]
    for team in sorted(data.get("teams", []), key=lambda t: t.get("id", 0)):
        owners = _owner_names(team, members)
        suffix = f"  [{owners}]" if owners else ""
        lines.append(f"  id {team.get('id'):<3} {_team_name(team)}{suffix}")
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_standings(league: str | None = None) -> str:
    """Current standings with records, points for, and points against.

    Args:
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mTeam", "mSettings"])
    ranked = sorted(
        data.get("teams", []),
        key=lambda t: (-(_record(t)[0]), _record(t)[1], -_record(t)[3]),
    )
    lines = [
        f"Standings for {(data.get('settings') or {}).get('name', lg.alias)}, "
        f"{lg.season} season through week {_current_week(data)}",
        "",
        f"{'#':<3} {'Team':<28} {'Rec':<9} {'PF':>8} {'PA':>8}",
    ]
    for rank, team in enumerate(ranked, start=1):
        wins, losses, ties, points_for, points_against = _record(team)
        record = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
        lines.append(
            f"{rank:<3} {_team_name(team)[:28]:<28} {record:<9} "
            f"{points_for:>8.1f} {points_against:>8.1f}"
        )
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_team_roster(team_id: int, week: int | None = None,
                          league: str | None = None) -> str:
    """Full roster for one team, with per-player projected and actual points.

    Args:
        team_id: Numeric team id from get_league_info.
        week: NFL week number. Defaults to the current week.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mRoster", "mTeam"], {"scoringPeriodId": week} if week else None)
    week = week or _current_week(data)
    team = _team_index(data).get(team_id)
    if not team:
        available = ", ".join(str(t) for t in sorted(_team_index(data)))
        raise ESPNError(f"No team with id {team_id}. Valid ids: {available}")

    wins, losses, ties, points_for, _ = _record(team)
    record = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
    header = f"{_team_name(team)} (id {team_id}), {record}, {points_for:.1f} PF, week {week}"
    return f"{header}\n\n{_format_roster(_roster_entries(team), week)}"


@mcp.tool()
@_friendly_errors
async def get_matchups(week: int | None = None, league: str | None = None) -> str:
    """Scoreboard for a week: every matchup with scores.

    Args:
        week: NFL week number. Defaults to the current week.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mMatchupScore", "mTeam"],
                              {"scoringPeriodId": week} if week else None)
    week = week or _current_week(data)
    teams = _team_index(data)

    lines = [f"Week {week} matchups", ""]
    found = False
    for matchup in data.get("schedule", []):
        if matchup.get("matchupPeriodId") != week:
            continue
        found = True
        home, away = matchup.get("home") or {}, matchup.get("away") or {}
        home_team = teams.get(home.get("teamId"), {})
        away_team = teams.get(away.get("teamId"), {})
        winner = matchup.get("winner", "UNDECIDED")

        if not away_team:
            lines.append(f"  {_team_name(home_team)} has a bye")
            continue

        def mark(side: str) -> str:
            return " W" if winner == side else ""

        lines.append(
            f"  {_team_name(away_team)[:24]:<24} {float(away.get('totalPoints') or 0):>7.1f}{mark('AWAY')}\n"
            f"  {_team_name(home_team)[:24]:<24} {float(home.get('totalPoints') or 0):>7.1f}{mark('HOME')}"
        )
        lines.append("")
    if not found:
        lines.append(f"  No matchups scheduled for week {week}.")
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_box_score(week: int, team_id: int | None = None,
                        league: str | None = None) -> str:
    """Player-by-player box score for a week, including who each team started.

    Args:
        week: NFL week number.
        team_id: Optional. Limit output to the matchup this team played in.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(
        lg, ["mMatchup", "mMatchupScore", "mBoxscore", "mTeam"], {"scoringPeriodId": week}
    )
    teams = _team_index(data)
    blocks: list[str] = []

    for matchup in data.get("schedule", []):
        if matchup.get("matchupPeriodId") != week:
            continue
        home, away = matchup.get("home") or {}, matchup.get("away") or {}
        ids = {home.get("teamId"), away.get("teamId")}
        if team_id is not None and team_id not in ids:
            continue

        parts = []
        for side in (away, home):
            if not side:
                continue
            team = teams.get(side.get("teamId"), {})
            entries = _roster_entries(side)
            title = (
                f"{_team_name(team)} (id {side.get('teamId')}) "
                f"{float(side.get('totalPoints') or 0):.1f}"
            )
            body = _format_roster(entries, week) if entries else "  (no roster detail returned)"
            parts.append(f"{title}\n{body}")
        blocks.append("\n\n".join(parts))

    if not blocks:
        return f"No box score found for week {week}" + (f", team {team_id}." if team_id else ".")
    return f"Week {week} box score\n\n" + "\n\n---\n\n".join(blocks)


@mcp.tool()
@_friendly_errors
async def get_free_agents(position: str | None = None, limit: int = 20,
                          week: int | None = None, league: str | None = None) -> str:
    """Best available free agents and waiver adds, sorted by weekly projection.

    Args:
        position: Optional filter, one of QB, RB, WR, TE, K, D/ST, FLEX.
        limit: How many players to return, max 50.
        week: NFL week to project for. Defaults to the current week.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    limit = max(1, min(int(limit), 50))
    meta = await _league_data(lg, "mSettings")
    week = week or _current_week(meta)

    players_filter: dict[str, Any] = {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "limit": 250,
        "offset": 0,
        "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
    }
    if position:
        key = position.upper().replace(" ", "")
        slot_ids = SLOT_FILTERS.get(key)
        if not slot_ids:
            raise ESPNError(
                f"Unknown position '{position}'. Use QB, RB, WR, TE, K, D/ST, or FLEX."
            )
        players_filter["filterSlotIds"] = {"value": slot_ids}

    data = await _league_data(
        lg, "kona_player_info", {"scoringPeriodId": week},
        fantasy_filter={"players": players_filter},
    )

    rows = []
    for entry in data.get("players", []):
        player = entry.get("player") or {}
        _, projected = _player_points(player, week)
        owned = (player.get("ownership") or {}).get("percentOwned")
        rows.append((projected if projected is not None else -1.0, player, projected, owned))

    rows.sort(key=lambda row: row[0], reverse=True)
    if not rows:
        return "ESPN returned no available players. The pool may be empty or the filter too narrow."

    label = f"{position.upper()} " if position else ""
    lines = [
        f"Top available {label}free agents, week {week} projections",
        "",
        f"{'Player':<34} {'Proj':>7} {'Own%':>7}",
    ]
    for _, player, projected, owned in rows[:limit]:
        owned_text = "-" if owned is None else f"{owned:.0f}%"
        lines.append(f"{_player_label(player)[:34]:<34} {_num(projected):>7} {owned_text:>7}")
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_player(name: str, week: int | None = None, league: str | None = None) -> str:
    """Look up a player by name: who rosters him, projections, and season points.

    Args:
        name: Full or partial player name, for example "Jefferson".
        week: NFL week for the projection. Defaults to the current week.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    index = await _all_players(lg)
    query = name.strip().lower()
    scored = []
    for player in index:
        full = (player.get("fullName") or "").lower()
        if not full:
            continue
        if query in full:
            score = 1.0 + len(query) / max(len(full), 1)
        else:
            score = SequenceMatcher(None, query, full).ratio()
            if score < 0.6:
                continue
        scored.append((score, player))

    if not scored:
        return f"No player matching '{name}' in the {lg.season} ESPN player index."

    scored.sort(key=lambda row: row[0], reverse=True)
    top = [player for _, player in scored[:5]]

    meta = await _league_data(lg, ["mSettings", "mTeam"])
    week = week or _current_week(meta)
    teams = _team_index(meta)

    detail = await _league_data(
        lg, "kona_player_info", {"scoringPeriodId": week},
        fantasy_filter={"players": {"filterIds": {"value": [p["id"] for p in top]}, "limit": 5}},
    )
    by_id = {e.get("id") or (e.get("player") or {}).get("id"): e for e in detail.get("players", [])}

    lines = []
    for stub in top:
        entry = by_id.get(stub["id"]) or {}
        player = entry.get("player") or stub
        actual, projected = _player_points(player, week)
        season, rest = _season_stats(player)
        on_team = entry.get("onTeamId") or 0
        owner = _team_name(teams[on_team]) + f" (id {on_team})" if on_team in teams else "Free agent"
        owned = (player.get("ownership") or {}).get("percentOwned")
        lines.append(
            f"{_player_label(player)}\n"
            f"  Rostered by: {owner}\n"
            f"  Week {week}: projected {_num(projected)}, actual {_num(actual)}\n"
            f"  Season: {_num(season)} points, full-season projection {_num(rest)}"
            + (f", owned in {owned:.0f}% of leagues" if owned is not None else "")
        )
        if len(lines) >= 3:
            break
    return "\n\n".join(lines)


@mcp.tool()
@_friendly_errors
async def compare_rosters(team_a: int, team_b: int, week: int | None = None,
                          league: str | None = None) -> str:
    """Side-by-side roster breakdown of two teams by position, for trade research.

    Shows every player each team holds at each position with season points, full
    season projection, and this week's projection, plus how many starters the
    position requires and whether each team is deep or thin there. Use this to
    reason about which positions each side can afford to trade from.

    Args:
        team_a: First team id, usually the user's team.
        team_b: Second team id, the potential trade partner.
        week: NFL week for the weekly projection. Defaults to the current week.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mRoster", "mTeam", "mSettings"])
    week = week or _current_week(data)
    teams = _team_index(data)
    for team_id in (team_a, team_b):
        if team_id not in teams:
            available = ", ".join(str(t) for t in sorted(teams))
            raise ESPNError(f"No team with id {team_id}. Valid ids: {available}")

    required = _starter_counts(data)
    flex = _flex_count(data)
    groups = {
        team_id: _position_groups(_roster_entries(teams[team_id]), week)
        for team_id in (team_a, team_b)
    }

    lines = [
        f"Roster comparison, week {week}",
        f"A: {_team_name(teams[team_a])} (id {team_a})   "
        f"B: {_team_name(teams[team_b])} (id {team_b})",
        f"Lineup requires {', '.join(f'{p} x{n}' for p, n in required.items() if n)}"
        + (f", plus {flex} FLEX" if flex else ""),
        "",
    ]

    for position in POSITION_ORDER:
        rows_a = groups[team_a].get(position, [])
        rows_b = groups[team_b].get(position, [])
        if not rows_a and not rows_b:
            continue
        need = required.get(position, 0)
        lines.append(f"{position}  (starts {need})")
        for label, team_id, rows in (("A", team_a, rows_a), ("B", team_b, rows_b)):
            note = _depth_note(len(rows), need)
            head = f"  {label} {_team_name(teams[team_id])[:22]:<22} {len(rows)} rostered"
            lines.append(f"{head}{'  <- ' + note if note else ''}")
            for row in rows:
                tag = " [IR]" if row["slot"] == 21 else ""
                lines.append(
                    f"      {_player_label(row['player'])[:32]:<32}"
                    f" season {_num(row['season']):>6}"
                    f"  ros proj {_num(row['rest_of_season']):>7}"
                    f"  wk {_num(row['week']):>5}{tag}"
                )
        lines.append("")

    lines.append(
        "Depth notes compare rostered bodies to required starters only, so treat "
        "them as a starting point rather than a verdict."
    )
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def find_trade_targets(team_id: int, position: str, limit: int = 15,
                             week: int | None = None, league: str | None = None) -> str:
    """Rostered players at a position on other teams, ranked, with each owner's depth.

    Answers "who could I realistically trade for at RB". Players held by an owner
    with a surplus at that position are the plausible targets; players held by a
    thin owner are not. Pair this with compare_rosters before proposing anything.

    Args:
        team_id: The team doing the asking, excluded from results.
        position: One of QB, RB, WR, TE, K, D/ST.
        limit: How many players to return, max 40.
        week: NFL week for the weekly projection. Defaults to the current week.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    position = position.upper().replace(" ", "")
    position = {"DST": "D/ST", "DEF": "D/ST"}.get(position, position)
    if position not in POSITION_ORDER:
        raise ESPNError(f"Unknown position '{position}'. Use QB, RB, WR, TE, K, or D/ST.")
    limit = max(1, min(int(limit), 40))

    data = await _league_data(lg, ["mRoster", "mTeam", "mSettings"])
    week = week or _current_week(data)
    teams = _team_index(data)
    if team_id not in teams:
        raise ESPNError(f"No team with id {team_id}. Valid ids: {', '.join(str(t) for t in sorted(teams))}")

    required = _starter_counts(data).get(position, 0)
    candidates = []
    for other_id, team in teams.items():
        if other_id == team_id:
            continue
        rows = _position_groups(_roster_entries(team), week).get(position, [])
        for rank, row in enumerate(rows, start=1):
            candidates.append((other_id, rank, len(rows), row))

    if not candidates:
        return f"No {position} found on other rosters in this league."

    candidates.sort(key=lambda c: c[3]["season"] if c[3]["season"] is not None else -1, reverse=True)

    lines = [
        f"{position} held by other teams, week {week}, ranked by season points",
        f"This league starts {required} {position}.",
        "",
    ]
    for other_id, rank, depth, row in candidates[:limit]:
        note = _depth_note(depth, required)
        role = "starter" if rank <= max(required, 1) else f"depth #{rank}"
        lines.append(
            f"{_player_label(row['player'])[:32]:<32}"
            f" season {_num(row['season']):>6}"
            f"  ros proj {_num(row['rest_of_season']):>7}"
            f"  wk {_num(row['week']):>5}"
        )
        lines.append(
            f"    owner {_team_name(teams[other_id])} (id {other_id}), "
            f"their {role} of {depth}{', ' + note if note else ''}"
        )
    lines.append("")
    lines.append(
        "Owners marked 'thin' are unlikely to deal at this position. Nothing here "
        "submits a trade; proposals still go through the ESPN app."
    )
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_recent_activity(limit: int = 20, include_lineup_moves: bool = False,
                              league: str | None = None) -> str:
    """Recent adds, drops, waiver claims, and trades in the league.

    Args:
        limit: How many transactions to return, max 50.
        include_lineup_moves: Include start/sit changes, which are noisy.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    limit = max(1, min(int(limit), 50))
    data = await _league_data(lg, ["mTransactions2", "mTeam"], fantasy_filter={
        "transactions": {"limit": 200, "sortDate": {"sortPriority": 1, "sortAsc": False}}
    })
    transactions = data.get("transactions")
    if not transactions:
        return (
            "ESPN returned no transaction data for this league. Some leagues do not "
            "expose the mTransactions2 view; the ESPN site's Recent Activity page is "
            "the fallback."
        )

    teams = _team_index(data)
    index = {p["id"]: p for p in await _all_players(lg) if "id" in p}

    def player_name(player_id: Any) -> str:
        player = index.get(player_id)
        return _player_label(player) if player else f"player {player_id}"

    def team_name(tid: Any) -> str:
        team = teams.get(tid)
        return _team_name(team) if team else f"team {tid}"

    transactions.sort(key=lambda t: t.get("proposedDate") or 0, reverse=True)
    lines = ["Recent league activity", ""]
    count = 0
    for txn in transactions:
        kind = txn.get("type", "")
        if kind == "ROSTER" and not include_lineup_moves:
            continue
        when = txn.get("proposedDate")
        stamp = (
            _dt.datetime.fromtimestamp(when / 1000).strftime("%b %d %I:%M%p")
            if isinstance(when, (int, float)) else "unknown date"
        )
        actions = []
        for item in txn.get("items") or []:
            verb = item.get("type", "")
            if verb == "ADD":
                actions.append(
                    f"{team_name(item.get('toTeamId') or txn.get('teamId'))} added "
                    f"{player_name(item.get('playerId'))}"
                )
            elif verb == "DROP":
                actions.append(
                    f"{team_name(item.get('fromTeamId') or txn.get('teamId'))} dropped "
                    f"{player_name(item.get('playerId'))}"
                )
            elif verb == "LINEUP" and include_lineup_moves:
                actions.append(
                    f"{team_name(txn.get('teamId'))} moved {player_name(item.get('playerId'))}"
                )
        if not actions:
            continue
        status = txn.get("status", "")
        suffix = f" [{status.lower()}]" if status and status != "EXECUTED" else ""
        lines.append(f"{stamp}  {TRANSACTION_LABELS.get(kind, kind)}{suffix}")
        for action in actions:
            lines.append(f"    {action}")
        count += 1
        if count >= limit:
            break

    if count == 0:
        return "No transactions matched. Try include_lineup_moves=true for a fuller log."
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_draft_recap(team_id: int | None = None, round_limit: int = 4,
                          league: str | None = None) -> str:
    """Draft results by round, with auction amounts when the league used one.

    Args:
        team_id: Optional. Show only this team's picks, all rounds.
        round_limit: When showing the whole draft, stop after this many rounds.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mDraftDetail", "mTeam"])
    picks = ((data.get("draftDetail") or {}).get("picks")) or []
    if not picks:
        return "No draft data available for this league and season."

    teams = _team_index(data)
    index = {p["id"]: p for p in await _all_players(lg) if "id" in p}
    picks.sort(key=lambda p: p.get("overallPickNumber") or 0)

    lines = []
    if team_id is not None:
        team = teams.get(team_id)
        if not team:
            raise ESPNError(f"No team with id {team_id}.")
        lines.append(f"{_team_name(team)} draft picks, {lg.season}")
        lines.append("")
        for pick in picks:
            if pick.get("teamId") != team_id:
                continue
            player = index.get(pick.get("playerId"))
            label = _player_label(player) if player else f"player {pick.get('playerId')}"
            cost = f"  ${pick['bidAmount']}" if pick.get("bidAmount") else ""
            keeper = "  (keeper)" if pick.get("keeper") else ""
            lines.append(
                f"  R{pick.get('roundId')}.{pick.get('roundPickNumber'):<3} "
                f"(#{pick.get('overallPickNumber')}) {label}{cost}{keeper}"
            )
        return "\n".join(lines)

    lines.append(f"{lg.season} draft, first {round_limit} rounds")
    for pick in picks:
        round_id = pick.get("roundId") or 0
        if round_id > round_limit:
            break
        if pick.get("roundPickNumber") == 1:
            lines.append(f"\nRound {round_id}")
        player = index.get(pick.get("playerId"))
        label = _player_label(player) if player else f"player {pick.get('playerId')}"
        cost = f"  ${pick['bidAmount']}" if pick.get("bidAmount") else ""
        team = teams.get(pick.get("teamId"))
        lines.append(
            f"  #{pick.get('overallPickNumber'):<3} {label:<34} "
            f"{_team_name(team) if team else '?'}{cost}"
        )
    return "\n".join(lines)


@mcp.tool()
@_friendly_errors
async def get_team_schedule(team_id: int, league: str | None = None) -> str:
    """Week-by-week results and upcoming opponents for one team.

    Args:
        team_id: Numeric team id from get_league_info.
        league: League alias. Required when more than one league is configured.
    """
    lg = _resolve(league)
    data = await _league_data(lg, ["mMatchupScore", "mTeam"])
    teams = _team_index(data)
    if team_id not in teams:
        raise ESPNError(f"No team with id {team_id}.")

    lines = [f"{_team_name(teams[team_id])} schedule, {lg.season}", ""]
    for matchup in sorted(data.get("schedule", []), key=lambda m: m.get("matchupPeriodId") or 0):
        home, away = matchup.get("home") or {}, matchup.get("away") or {}
        if team_id not in (home.get("teamId"), away.get("teamId")):
            continue
        is_home = home.get("teamId") == team_id
        mine, theirs = (home, away) if is_home else (away, home)
        opponent = teams.get(theirs.get("teamId"))
        my_points = float(mine.get("totalPoints") or 0)
        their_points = float(theirs.get("totalPoints") or 0)
        week = matchup.get("matchupPeriodId")

        if not opponent:
            lines.append(f"  Week {week:<3} bye")
            continue
        if my_points or their_points:
            if matchup.get("winner", "UNDECIDED") == "UNDECIDED":
                result = "live"
            else:
                result = "W" if my_points > their_points else "L" if my_points < their_points else "T"
            lines.append(
                f"  Week {week:<3} {result:<4} {my_points:>6.1f} - {their_points:<6.1f} "
                f"vs {_team_name(opponent)}"
            )
        else:
            lines.append(f"  Week {week:<3} upcoming vs {_team_name(opponent)}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
