"""Offline smoke test. Feeds every tool a synthetic ESPN-shaped payload.

Configures three leagues so multi-league routing is exercised too. No network
access and no credentials required.
"""
import asyncio
import os

os.environ["ESPN_LEAGUES"] = "purdue=111111,fremd-50=222222,fremd-20=333333"
os.environ["ESPN_SEASON"] = "2026"
os.environ.pop("ESPN_LEAGUE_ID", None)

import server  # noqa: E402

WEEK = 3


def player(pid, name, pos, pro, proj, act, season=None, ros=None, injury="ACTIVE"):
    stats = [
        {"scoringPeriodId": WEEK, "statSourceId": 1, "appliedTotal": proj},
        {"scoringPeriodId": WEEK, "statSourceId": 0, "appliedTotal": act},
    ]
    if season is not None:
        stats.append({"statSourceId": 0, "statSplitTypeId": 0, "appliedTotal": season})
    if ros is not None:
        stats.append({"statSourceId": 1, "statSplitTypeId": 0, "appliedTotal": ros})
    return {
        "id": pid, "fullName": name, "defaultPositionId": pos, "proTeamId": pro,
        "injuryStatus": injury, "stats": stats,
        "ownership": {"percentOwned": 64.2},
    }


def entry(slot, p):
    return {"lineupSlotId": slot, "playerPoolEntry": {"player": p}}


ROSTER_1 = [
    entry(0, player(1, "Jalen Hurts", 1, 21, 22.4, 25.1, 71.2, 288.0)),
    entry(2, player(2, "Bijan Robinson", 2, 1, 18.0, 12.3, 48.9, 231.5)),
    entry(4, player(3, "Puka Nacua", 3, 14, 16.2, 21.8, 55.0, 240.1, "QUESTIONABLE")),
    entry(4, player(12, "Garrett Wilson", 3, 20, 14.0, 11.2, 44.0, 205.3)),
    entry(6, player(4, "Trey McBride", 4, 22, 11.5, 9.0, 33.1, 160.0)),
    entry(23, player(13, "Tony Pollard", 2, 10, 10.2, 8.8, 31.0, 148.0)),
    entry(20, player(5, "Jaylen Waddle", 3, 15, 12.1, 4.2, 29.4, 150.2)),
    entry(20, player(14, "Jordan Mason", 2, 25, 8.4, 6.1, 24.0, 120.4)),
    entry(20, player(15, "Zay Flowers", 3, 33, 11.0, 13.3, 38.2, 178.0)),
]
ROSTER_2 = [
    entry(0, player(6, "Josh Allen", 1, 2, 24.9, 30.2, 88.0, 330.0)),
    entry(2, player(7, "Saquon Barkley", 2, 21, 19.8, 17.4, 60.3, 262.0)),
    entry(2, player(16, "De'Von Achane", 2, 15, 17.1, 20.0, 57.5, 250.8)),
    entry(4, player(17, "Nico Collins", 3, 34, 15.3, 9.4, 41.0, 198.0)),
    entry(6, player(18, "Sam LaPorta", 4, 8, 12.2, 14.6, 40.1, 175.2)),
    entry(20, player(19, "Rachaad White", 2, 27, 9.0, 7.2, 27.3, 131.0)),
    entry(20, player(20, "Baker Mayfield", 1, 27, 18.0, 15.5, 52.0, 245.0)),
    entry(21, player(8, "Chris Godwin", 3, 27, 0.0, 0.0, 12.0, 30.0, "INJURY_RESERVE")),
]

TEAMS = [
    {"id": 1, "name": "Gridiron Gurus", "owners": ["{A}"],
     "record": {"overall": {"wins": 2, "losses": 1, "ties": 0, "pointsFor": 341.2, "pointsAgainst": 300.1}},
     "roster": {"entries": ROSTER_1}},
    {"id": 2, "name": "Waiver Wire Warriors", "owners": ["{B}"],
     "record": {"overall": {"wins": 3, "losses": 0, "ties": 0, "pointsFor": 380.4, "pointsAgainst": 290.0}},
     "roster": {"entries": ROSTER_2}},
]

LEAGUE = {
    "id": 111111,
    "scoringPeriodId": WEEK,
    "status": {"currentMatchupPeriod": WEEK, "latestScoringPeriod": WEEK},
    "members": [
        {"id": "{A}", "firstName": "Sai", "lastName": "K"},
        {"id": "{B}", "firstName": "Pat", "lastName": "M"},
    ],
    "settings": {
        "name": "Boilermaker Fantasy",
        "size": 2,
        "scoringSettings": {"scoringType": "H2H_POINTS", "scoringItems": [{"statId": 53, "points": 0.5}]},
        "scheduleSettings": {"matchupPeriodCount": 14, "playoffTeamCount": 6},
        "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 6, "21": 1}},
    },
    "teams": TEAMS,
    "schedule": [
        {"matchupPeriodId": 1, "winner": "HOME",
         "home": {"teamId": 1, "totalPoints": 120.5}, "away": {"teamId": 2, "totalPoints": 99.1}},
        {"matchupPeriodId": WEEK, "winner": "UNDECIDED",
         "home": {"teamId": 1, "totalPoints": 68.2, "rosterForCurrentScoringPeriod": {"entries": ROSTER_1}},
         "away": {"teamId": 2, "totalPoints": 47.6, "rosterForCurrentScoringPeriod": {"entries": ROSTER_2}}},
        {"matchupPeriodId": 4, "home": {"teamId": 2, "totalPoints": 0}, "away": {"teamId": 1, "totalPoints": 0}},
    ],
    "transactions": [
        {"id": "t1", "type": "WAIVER", "status": "EXECUTED", "teamId": 1, "proposedDate": 1757808000000,
         "items": [{"type": "ADD", "playerId": 9, "toTeamId": 1}, {"type": "DROP", "playerId": 5, "fromTeamId": 1}]},
        {"id": "t2", "type": "ROSTER", "status": "EXECUTED", "teamId": 2, "proposedDate": 1757708000000,
         "items": [{"type": "LINEUP", "playerId": 7}]},
        {"id": "t3", "type": "TRADE_ACCEPT", "status": "EXECUTED", "proposedDate": 1757608000000,
         "items": [{"type": "ADD", "playerId": 3, "toTeamId": 2, "fromTeamId": 1},
                   {"type": "ADD", "playerId": 7, "toTeamId": 1, "fromTeamId": 2}]},
    ],
    "draftDetail": {"picks": [
        {"playerId": 6, "teamId": 2, "roundId": 1, "roundPickNumber": 1, "overallPickNumber": 1, "bidAmount": 62},
        {"playerId": 2, "teamId": 1, "roundId": 1, "roundPickNumber": 2, "overallPickNumber": 2, "bidAmount": 55},
        {"playerId": 3, "teamId": 1, "roundId": 2, "roundPickNumber": 1, "overallPickNumber": 3, "bidAmount": 40},
        {"playerId": 7, "teamId": 2, "roundId": 2, "roundPickNumber": 2, "overallPickNumber": 4},
    ]},
}

POOL = {"players": [
    {"id": 9, "onTeamId": 0, "player": player(9, "Rome Odunze", 3, 3, 13.4, None, 21.0, 110.0)},
    {"id": 10, "onTeamId": 0, "player": player(10, "Tyjae Spears", 2, 10, 9.1, None, 18.2, 95.0)},
    {"id": 11, "onTeamId": 0, "player": player(11, "Cade Otton", 4, 27, 7.7, None, 15.5, 88.0)},
]}

PLAYER_INDEX = [
    {"id": 1, "fullName": "Jalen Hurts", "defaultPositionId": 1, "proTeamId": 21},
    {"id": 3, "fullName": "Puka Nacua", "defaultPositionId": 3, "proTeamId": 14},
    {"id": 5, "fullName": "Jaylen Waddle", "defaultPositionId": 3, "proTeamId": 15},
    {"id": 6, "fullName": "Josh Allen", "defaultPositionId": 1, "proTeamId": 2},
    {"id": 7, "fullName": "Saquon Barkley", "defaultPositionId": 2, "proTeamId": 21},
    {"id": 9, "fullName": "Rome Odunze", "defaultPositionId": 3, "proTeamId": 3},
    {"id": 2, "fullName": "Bijan Robinson", "defaultPositionId": 2, "proTeamId": 1},
]

SEEN_LEAGUE_IDS = set()


async def fake_fetch(league, url, params, fantasy_filter=None):
    SEEN_LEAGUE_IDS.add(league.league_id)
    views = params.get("view") or []
    if "/players" in url:
        return PLAYER_INDEX
    if "kona_player_info" in views:
        ids = ((fantasy_filter or {}).get("players", {}).get("filterIds") or {}).get("value")
        if ids:
            return {"players": [{"id": 3, "onTeamId": 1,
                                 "player": player(3, "Puka Nacua", 3, 14, 16.2, 21.8, 55.0, 240.1)}]}
        return POOL
    data = dict(LEAGUE)
    data["settings"] = dict(LEAGUE["settings"], name=f"{league.alias.title()} League")
    return data


server._fetch = fake_fetch


async def main():
    checks = [
        ("list_leagues", server.list_leagues()),
        ("get_league_info (alias 'purdue')", server.get_league_info(league="purdue")),
        ("get_standings (alias 'fremd-50')", server.get_standings(league="fremd-50")),
        ("get_team_roster", server.get_team_roster(1, league="purdue")),
        ("get_matchups", server.get_matchups(league="purdue")),
        ("get_box_score", server.get_box_score(WEEK, league="purdue")),
        ("get_free_agents", server.get_free_agents(position="WR", limit=5, league="purdue")),
        ("get_player", server.get_player("nacua", league="purdue")),
        ("compare_rosters", server.compare_rosters(1, 2, league="purdue")),
        ("find_trade_targets", server.find_trade_targets(1, "RB", league="purdue")),
        ("get_recent_activity", server.get_recent_activity(league="purdue")),
        ("get_draft_recap", server.get_draft_recap(league="purdue")),
        ("get_team_schedule", server.get_team_schedule(1, league="purdue")),
    ]
    for name, coro in checks:
        print("=" * 72)
        print(name)
        print("=" * 72)
        print(await coro)
        print()

    print("=" * 72)
    print("error paths")
    print("=" * 72)
    print("no league arg  ->", await server.get_standings())
    print("bad alias      ->", await server.get_standings(league="nope"))
    print("bad team id    ->", await server.get_team_roster(99, league="purdue"))
    print()
    print("league ids actually requested:", sorted(SEEN_LEAGUE_IDS))
    assert SEEN_LEAGUE_IDS == {"111111", "222222", "333333"}, "multi-league routing failed"
    print("multi-league routing OK")


asyncio.run(main())
