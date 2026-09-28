# ESPN Fantasy MCP Server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude query your ESPN fantasy football leagues. Once it's connected, you can ask things like "who should I start at flex this week?" or "find me a realistic RB trade in my friends league," and Claude answers from live league data instead of guessing.

The project is small (one Python file, about 1,100 lines), but it touches a lot of ideas worth understanding: client/server protocols, reverse-engineering an undocumented API, async I/O, caching, defensive parsing, and error design. This README walks through the setup first, then spends most of its time on how the thing works and why it's built the way it is.

## Contents

1. [Background: what MCP actually does](#background-what-mcp-actually-does)
2. [Tools](#tools)
3. [Setup](#setup)
4. [Configuring multiple leagues](#configuring-multiple-leagues)
5. [Architecture](#architecture)
6. [The ESPN API](#the-espn-api)
7. [Caching](#caching)
8. [Error handling](#error-handling)
9. [Trade research: splitting data from judgment](#trade-research-splitting-data-from-judgment)
10. [Testing](#testing)
11. [Known limitations](#known-limitations)
12. [Troubleshooting](#troubleshooting)
13. [Extending the server](#extending-the-server)

## Background: what MCP actually does

A language model on its own can only work with text that's already in its context. It has no way to go look up your roster. MCP solves that with a simple contract between two programs:

- The **client** (Claude Desktop or Claude Code) starts the server as a child process and talks to it over stdin/stdout using JSON-RPC.
- The **server** (this project) advertises a list of tools. Each tool has a name, a description, and a typed parameter list.
- When the model decides a tool would help, the client sends a `tools/call` request, the server runs the function, and the returned text goes back into the model's context.

Here's the full round trip for one question:

```
"Who's the best RB on waivers in my friends league?"
        │
        ▼
Claude (the model) picks a tool:  get_free_agents(position="RB", league="friends")
        │   JSON-RPC over stdio
        ▼
server.py
  ├─ _resolve("friends")         -> League(alias, id, season, cookies)
  ├─ _league_data(...)           -> builds URL + query + X-Fantasy-Filter header
  ├─ _fetch(...)                 -> cache check, then HTTP GET to ESPN
  └─ format rows as plain text
        │
        ▼
Claude reads the text and writes the answer
```

Notice that the server never returns raw JSON to the model. ESPN's payloads are large, full of numeric IDs, and mostly irrelevant to any one question. Turning them into short, labeled text before returning them keeps the model's context small and makes its answers more reliable.

## Tools

The server exposes 13 tools. **All of them are read-only.** None of them set a lineup, place a waiver claim, or propose a trade. That's deliberate: ESPN's write endpoints use a different auth flow, and a model misfiring on a write costs you a real transaction you can't take back. Keeping the server read-only means the worst case is a wrong answer, never a wrong action.

| Tool | What it returns | ESPN views used |
| --- | --- | --- |
| `list_leagues` | Every configured league with its alias, id, season, and name | `mSettings` |
| `get_league_info` | Scoring format, lineup slots, current week, and the team-id map | `mSettings`, `mTeam` |
| `get_standings` | Records, points for, points against | `mTeam`, `mSettings` |
| `get_team_roster` | One team's roster with projected and actual points | `mRoster`, `mTeam` |
| `get_matchups` | The scoreboard for a week | `mMatchupScore`, `mTeam` |
| `get_box_score` | Player-by-player scoring for a week's matchups | `mMatchup`, `mMatchupScore`, `mBoxscore`, `mTeam` |
| `get_free_agents` | Available players sorted by weekly projection, filterable by position | `kona_player_info` + filter header |
| `get_player` | Fuzzy name lookup: who rosters him, projection, season points, ownership | season player index |
| `compare_rosters` | Two teams side by side by position, with depth and surplus flags | `mRoster`, `mTeam`, `mSettings` |
| `find_trade_targets` | Players at a position on other rosters, with each owner's depth there | `mRoster`, `mTeam`, `mSettings` |
| `get_recent_activity` | Adds, drops, waiver claims, trades | `mTransactions2`, `mTeam` |
| `get_draft_recap` | Draft results by round, with auction amounts | `mDraftDetail`, `mTeam` |
| `get_team_schedule` | Week-by-week results and upcoming opponents | `mMatchupScore`, `mTeam` |

Tools that return lists are capped (for example, `get_free_agents` at 50 and `find_trade_targets` at 40). A tool response lands directly in the model's context window, so an unbounded list is a real cost, not just a cosmetic one.

## Setup

### 1. Install dependencies

You'll need Python 3.10 or newer.

```bash
git clone https://github.com/saik0v0ur/espn-fantasy-mcp.git
cd espn-fantasy-mcp
pip install -r requirements.txt
```

There are only two dependencies: `mcp` (the official Python SDK) and `httpx` (an HTTP client with async support). The server works with both the 1.x and 2.x lines of `mcp`; see [Architecture](#architecture) for how.

### 2. Find your league id

It's in the URL of your league page on espn.com:

```
https://fantasy.espn.com/football/league?leagueId=123456789
```

### 3. Get your cookies (private leagues only)

Public leagues need nothing else. Private leagues authenticate with two browser cookies:

1. Log in at espn.com in Chrome or Firefox.
2. Open DevTools (F12) and go to **Application > Storage > Cookies > `https://www.espn.com`**.
3. Copy the values of `espn_s2` (a long URL-encoded string) and `SWID` (a GUID in curly braces).

One cookie pair covers every league on that ESPN account. Keep in mind that `espn_s2` is a session credential: anyone holding it can read your account's leagues. Treat it like a password, keep it in environment variables, and never commit it. It expires roughly once a year.

### 4. Connect it to Claude

**Claude Desktop.** Go to Settings > Developer > Edit Config to open `claude_desktop_config.json`, then add:

```json
{
  "mcpServers": {
    "espn-fantasy": {
      "command": "/absolute/path/to/python3",
      "args": ["/absolute/path/to/espn-fantasy-mcp/server.py"],
      "env": {
        "ESPN_LEAGUE_ID": "123456789",
        "ESPN_S2": "your espn_s2 value",
        "ESPN_SWID": "{your-SWID}"
      }
    }
  }
}
```

Restart Claude Desktop afterward. Use absolute paths for both the interpreter and the script. Claude Desktop launches servers with a minimal environment, not your shell profile, so a bare `python3` can resolve to a different interpreter than the one you installed packages into. Run `which python3` in the environment where you ran `pip install` to get the right path.

**Claude Code:**

```bash
claude mcp add espn-fantasy \
  --env ESPN_LEAGUE_ID=123456789 \
  --env ESPN_S2='your espn_s2 value' \
  --env ESPN_SWID='{your-SWID}' \
  -- python3 /absolute/path/to/espn-fantasy-mcp/server.py
```

Flags go before the server name, and `--` separates Claude's flags from the command it runs. Verify with `claude mcp list`, then run `/mcp` inside a session to see the tools.

## Configuring multiple leagues

Set `ESPN_LEAGUES` instead of `ESPN_LEAGUE_ID` and give each league an alias. Every tool then takes a `league` argument, and Claude passes the alias through based on how you phrase the question.

The shorthand form works when every league is on one ESPN account:

```
ESPN_LEAGUES=work=123456,friends=789012,family=345678
```

The JSON form handles leagues that need different seasons or different accounts:

```json
{
  "work": "123456",
  "friends": {"id": "789012", "season": 2026},
  "family": {"id": "345678", "s2": "OTHER_ACCOUNT_COOKIE", "swid": "{OTHER-SWID}"}
}
```

Resolution works in layers. Any field a league entry leaves out falls back to the global `ESPN_SEASON`, `ESPN_SPORT`, `ESPN_S2`, and `ESPN_SWID`. When the model passes a league argument, `_resolve()` tries it as an alias first (case-insensitive), then as a raw league id. When there's only one league, the argument becomes optional. When there are several and the model omits it, the server returns an error that lists the valid aliases, which is usually enough for the model to correct itself on the next call.

### All environment variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `ESPN_LEAGUES` | for multiple leagues | | JSON or `alias=id,alias=id` |
| `ESPN_LEAGUE_ID` | for a single league | | Numeric id from the league URL |
| `ESPN_LEAGUE_ALIAS` | no | `default` | Alias for the single-league form |
| `ESPN_SEASON` | no | current NFL season | Year the season kicked off; set it to query past years |
| `ESPN_S2` | private leagues | | `espn_s2` cookie |
| `ESPN_SWID` | private leagues | | `SWID` cookie; braces are added if missing |
| `ESPN_CACHE_TTL` | no | `60` | Seconds to cache league responses |
| `ESPN_SPORT` | no | `ffl` | `fba`, `flb`, `fhl` partially work, but position and slot maps are NFL-specific |

A small detail worth pointing out: the default season comes from `_default_season()`, which returns the current year from July onward and the previous year before that. NFL seasons are labeled by the year they start, so a January playoff game belongs to last year's season.

## Architecture

The whole server lives in `server.py`, organized top to bottom in layers. Each layer only calls the one below it.

```
┌──────────────────────────────────────────────┐
│ Tools (@mcp.tool)                            │  13 async functions, one per capability
│   wrapped by @_friendly_errors               │  turns ESPNError into readable text
├──────────────────────────────────────────────┤
│ Formatting helpers                           │  _format_roster, _player_label,
│                                              │  _position_groups, _depth_note, ...
├──────────────────────────────────────────────┤
│ Data access                                  │  _league_data, _all_players
├──────────────────────────────────────────────┤
│ HTTP + cache                                 │  _fetch, _headers, _cache
├──────────────────────────────────────────────┤
│ Configuration                                │  League dataclass, _load_leagues,
│                                              │  _resolve
├──────────────────────────────────────────────┤
│ Static maps                                  │  POSITIONS, SLOTS, PRO_TEAMS,
│                                              │  INJURY_SHORT, ...
└──────────────────────────────────────────────┘
```

A few design choices worth calling out:

**Configuration is loaded once and frozen.** `_load_leagues()` runs at import time and builds a `dict[str, League]`, where `League` is a `@dataclass(frozen=True)`. Nothing mutates config after startup, which removes a whole category of bugs. The cost is that config changes need a restart, which is normal for MCP servers anyway.

**Static lookup tables replace magic numbers.** ESPN encodes nearly everything as integers: position `2` means RB, lineup slot `23` means FLEX, pro team `12` means KC. Those mappings live in dictionaries at the top of the file, so the parsing code reads `SLOTS.get(slot_id)` instead of a chain of `if` statements, and when ESPN adds a slot it's a one-line change.

**Every tool is `async`.** Network calls dominate the runtime, and `httpx.AsyncClient` lets the server wait on ESPN without blocking the MCP event loop. It doesn't parallelize much today, but it's the right shape if you want tools that fan out across several leagues at once.

**The SDK version is detected, not assumed.** In `mcp` 2.x the server class is `mcp.server.mcpserver.MCPServer`; in 1.x it's `mcp.server.fastmcp.FastMCP`. The import is a `try/except ImportError`. Older 1.x releases also reject a `version` keyword, so the constructor inspects the class signature with `inspect.signature` and only passes `version` when it's accepted. Checking for the capability you need, instead of hard-coding version numbers, tends to survive future releases better.

**The server gives the model instructions.** The `instructions` string passed to the server tells Claude to call `list_leagues` first when there are several leagues, to look up team ids with `get_league_info` (ids are small integers, not names), and that trade analysis is its job, not the server's. Good tool descriptions and instructions matter as much as the code: they're the only documentation the model reads.

## The ESPN API

ESPN doesn't publish a fantasy API. This server calls the same private endpoints the ESPN web app uses, which you can find yourself by opening DevTools on your league page and watching the Network tab. Everything below came from doing exactly that.

**Base URL:**

```
https://lm-api-reads.fantasy.espn.com/apis/v3/games/{sport}/seasons/{season}/segments/0/leagues/{leagueId}
```

**Views.** Rather than separate endpoints per resource, ESPN uses one league endpoint and a repeatable `view` query parameter to choose which sections come back: `mTeam` for teams and records, `mRoster` for rosters, `mSettings` for scoring and lineup rules, `mMatchupScore` for the schedule and scores, `mTransactions2` for activity, `mDraftDetail` for the draft. Requesting several views in one call (`?view=mTeam&view=mRoster`) returns them merged into one JSON object, which is how most tools get everything they need in a single round trip.

**The `X-Fantasy-Filter` header.** Some queries are too complex for URL parameters, so ESPN accepts a JSON document in a custom request header. The free-agent search, for example, sends something like this:

```json
{
  "players": {
    "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
    "filterSlotIds": {"value": [2]},
    "limit": 250,
    "sortPercOwned": {"sortAsc": false, "sortPriority": 1}
  }
}
```

That's "unrostered or on waivers, eligible at RB, top 250 by ownership." The server then re-sorts those 250 by this week's projection and returns the top N. Pulling a wide pool by ownership and ranking it locally keeps the sorting logic in code you control, and it means the ranking uses the exact projection the tool displays.

**Authentication** is just the `espn_s2` and `SWID` cookies sent in a `Cookie` header. There's no token exchange or OAuth step.

**Historical seasons** live at a different endpoint, `/leagueHistory/{leagueId}?seasonId=YYYY`, and come back wrapped in a one-element list. `_league_data()` tries the normal endpoint first and falls back to the history endpoint on failure, and `_fetch()` unwraps the list, so tool code never has to know which one answered.

**Player lookup** uses a separate endpoint, `/seasons/{season}/players?view=players_wl`, that returns the full player index (several thousand entries). `get_player` fuzzy-matches your query against it with `difflib.SequenceMatcher`, so "nacua" or "puka" both find the right player. It then fetches league-specific details for the closest matches with a `filterIds` filter, which is how it knows who rosters each one.

## Caching

There are two caches, both plain in-memory dictionaries keyed by strings.

**League responses** are cached for `ESPN_CACHE_TTL` seconds (default 60). The cache key is the JSON serialization of `[alias, url, params, fantasy_filter]` with sorted keys, so two logically identical requests always hit the same entry no matter what order their parameters were built in. This matters more than you'd think: a single question like "compare my team to team 4" can trigger three or four tool calls that all need `mTeam`, and without the cache every one would hit ESPN.

**The player index** is cached for six hours per sport and season. It's large and changes rarely, so a long TTL is the right trade.

Timestamps come from `time.monotonic()` rather than `time.time()`. The monotonic clock can't jump backward when the system clock syncs, so a cache entry can never appear to be from the future.

There's no eviction beyond expiry and no size bound. For a server that talks to a handful of leagues for one user, that's a reasonable simplification. A multi-user deployment would want an LRU cache instead.

## Error handling

Errors are designed for the model as the reader, not a developer.

All ESPN-related failures raise a single custom exception, `ESPNError`, with a message written in plain language. `_fetch()` translates HTTP status codes into specific explanations:

| Status | Message tells the model |
| --- | --- |
| 401 / 403 | The league is private and the cookies are missing, expired, or from a non-member account |
| 404 | The league id is wrong, or the league didn't exist in that season |
| Non-JSON body | The login was probably rejected (ESPN returns an HTML page instead of JSON) |
| Network failure | ESPN couldn't be reached at all |

Every tool is wrapped in a `@_friendly_errors` decorator that catches `ESPNError` and returns the message as normal tool output instead of raising. That choice is intentional. If a tool raises, the model just sees an opaque failure. If it returns "ESPN returned 404 for league 'friends' (id 789012, season 2025). Check the id...", the model can explain the problem to you or retry with a corrected argument. Input validation follows the same idea: an invalid team id returns the list of valid ids.

Parsing is defensive throughout. Since the API is undocumented, the code uses `.get()` with defaults instead of indexing, and helpers like `_team_name()` fall back through several fields (`name`, then `location + nickname`, then `Team {id}`). A missing field degrades one line of output instead of crashing the tool.

## Trade research: splitting data from judgment

The server supplies facts. Claude supplies the reasoning. Trade value depends on scoring settings, schedule, injury risk, and who you're negotiating with, none of which reduce to a number an API can return. Two tools do most of the work:

`compare_rosters(team_a, team_b)` groups both rosters by position with season points, full-season projection, and this week's projection. It also reports how many starters each position requires (read from the league's lineup settings, with FLEX slots counted separately) and labels each side's depth.

`find_trade_targets(team_id, position)` lists every player at a position on other rosters, ranked, and annotates each with his owner's depth there.

The depth label comes from `_depth_note(count, required)`, which compares rostered players at a position against required starters:

| Extra players beyond starters | Label |
| --- | --- |
| 0 or fewer | `thin, no backup` |
| 1 | `one spare` |
| 2 or more | `surplus of N` |

An owner who starts two RBs and rosters five is a live trade partner. An owner with exactly two isn't. Complementary imbalances (you're deep at WR and thin at RB, they're the reverse) are what workable trades are built on.

A typical flow: `get_league_info` for team ids, `get_standings` to see who's buying and who's selling, `find_trade_targets` to shortlist, then `compare_rosters` against the best match to build the package.

The depth heuristic is intentionally simple, and you should know its blind spots. It counts bodies. It doesn't know about bye weeks, injuries short of an IR designation, or whether a backup is actually startable. ESPN's projections are also mediocre, so a deal that looks balanced by projection can still be lopsided. Push back on Claude's suggestions instead of pasting them into the app.

## Testing

```bash
python3 test_offline.py
```

This test runs without a network connection or an ESPN account. It sets `ESPN_LEAGUES` to three fake leagues, then replaces the server's `_fetch` function with a stub that returns a synthetic ESPN payload and records which league ids were requested. Every tool is called at least once, including the error paths (missing league argument, unknown alias, invalid team id). A successful run ends with:

```
league ids actually requested: ['111111', '222222', '333333']
multi-league routing OK
```

That last assertion checks that the `league` argument actually routes to the right league id, which is the easiest thing to break when changing configuration code.

Swapping out the network layer is a standard technique. Because every tool funnels through `_fetch()`, replacing one function isolates the entire server from the outside world. That's one practical payoff of the layered structure described above.

To check a real connection:

```bash
ESPN_LEAGUE_ID=123456789 python3 -c "
import asyncio, server
print(asyncio.run(server.list_leagues()))
"
```

## Known limitations

- **The API can change without notice.** Defensive parsing handles missing fields, but a structural change on ESPN's side will still break things.
- **`get_recent_activity` isn't available for every league.** It depends on the `mTransactions2` view, which some leagues don't expose. The tool says so instead of failing.
- **The first `get_player` call is slow.** It downloads the full player index. Later calls hit the six-hour cache.
- **Other sports only partially work.** `ESPN_SPORT` accepts `fba`, `flb`, and `fhl`, but the position, slot, and team maps are NFL-specific, so labels will be wrong.
- **The cache is per-process.** Restarting Claude restarts the server and clears it.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| "ESPN returned 401/403" | Missing or expired cookies, or an account that isn't a member of that league |
| "ESPN returned 404" | Wrong league id, or a season the league didn't exist in |
| "the league argument is required" | Several leagues configured and none named; `list_leagues` shows the aliases |
| Server shows as disconnected | Usually the wrong interpreter path in the config |
| Tool list is empty | The server crashed on import; run `python3 server.py` in a terminal to see the traceback |

## Extending the server

Adding a tool takes three steps:

1. Write an `async def` in `server.py` with type-hinted parameters and a docstring. The SDK builds the tool's JSON schema from the type hints, and the model reads the docstring to decide when to call it, so write it for that audience.
2. Decorate it with `@mcp.tool()` and then `@_friendly_errors` (in that order, outermost first, matching the existing tools).
3. Fetch data with `_league_data(lg, [views...])` and return formatted text, not raw JSON.

Some ideas if you want practice:

- **Parallel league queries.** A "standings across all my leagues" tool is a natural fit for `asyncio.gather()` over `_league_data` calls.
- **A bounded cache.** Replace the dictionary with an LRU structure and add a size limit.
- **Bye-week awareness.** Pull the NFL schedule and have `_depth_note` discount players on bye.
- **Non-football sports.** Move the static maps into per-sport tables selected by `ESPN_SPORT`.
