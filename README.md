# ESPN Fantasy MCP Server

This is a project I made so Claude can look at my ESPN fantasy football leagues. Before this, if I asked Claude "who should I start at flex?" it would just guess, because it had no way to see my team. Now it can actually pull my roster, the waiver wire, and matchups from ESPN and give me a real answer.

It's an **MCP server**, written in Python.

## What is MCP?

MCP stands for Model Context Protocol. Basically it's a standard way to give an AI app (like Claude Desktop or Claude Code) extra "tools" it can call. You write a small program (the server), tell Claude where it is, and then Claude can use the functions inside it.

So in this project:

- **Claude** = the one asking questions
- **server.py** = the middleman that talks to ESPN
- **ESPN** = where the actual league data lives

```
You: "who's the best RB on waivers?"
  -> Claude calls get_free_agents(position="RB")
  -> server.py asks ESPN for the data
  -> server.py turns it into readable text
  -> Claude reads it and answers you
```

## What it can do

There are 13 tools. All of them only **read** data. None of them can change your lineup or make a trade. I did that on purpose, because if the AI messes up a trade you can't undo it.

| Tool | What it does |
| --- | --- |
| `list_leagues` | Shows all the leagues you set up |
| `get_league_info` | Scoring type, lineup slots, current week, and which team id is which team |
| `get_standings` | Wins, losses, points for, points against |
| `get_team_roster` | One team's players with projected and actual points |
| `get_matchups` | The scoreboard for a week |
| `get_box_score` | Player by player points for a week's games |
| `get_free_agents` | Best available players, you can filter by position |
| `get_player` | Look up a player by name (who has him, his points, etc) |
| `compare_rosters` | Puts two teams side by side by position so you can spot trades |
| `find_trade_targets` | Finds players at a position on other teams and shows if that owner has extras |
| `get_recent_activity` | Adds, drops, waiver claims, and trades |
| `get_draft_recap` | Who got picked in each round |
| `get_team_schedule` | A team's past results and upcoming opponents |

## Files in this repo

| File | What it is |
| --- | --- |
| `server.py` | The actual MCP server. All the tools are in here. |
| `test_offline.py` | A test that uses fake ESPN data so you can check it works without internet or an ESPN account |
| `requirements.txt` | The Python packages you need |

## How to set it up

### Step 1: Install the packages

You need Python 3.10 or newer.

```bash
git clone https://github.com/saik0v0ur/espn-fantasy-mcp.git
cd espn-fantasy-mcp
pip install -r requirements.txt
```

That installs two things: `mcp` (the library for making MCP servers) and `httpx` (for making web requests to ESPN).

### Step 2: Find your league id

Go to your league on espn.com and look at the URL. The number after `leagueId=` is your league id.

```
https://fantasy.espn.com/football/league?leagueId=123456789
                                                  ^^^^^^^^^ this part
```

### Step 3: Get your ESPN cookies (only if your league is private)

If your league is public you can skip this step.

Private leagues need you to be logged in, so the server needs two cookies from your browser:

1. Log in to espn.com in Chrome or Firefox.
2. Press F12 to open DevTools.
3. Go to **Application** > **Cookies** > `https://www.espn.com`.
4. Copy the value of `espn_s2` (it's really long) and `SWID` (it looks like `{ABCD-1234-...}`).

**Important:** the `espn_s2` cookie is basically your ESPN login. Treat it like a password and never commit it to GitHub. It expires about once a year, so if things stop working, grab a new one.

### Step 4: Connect it to Claude

**For Claude Desktop:**

Open Settings > Developer > Edit Config. That opens a file called `claude_desktop_config.json`. Add this to it:

```json
{
  "mcpServers": {
    "espn-fantasy": {
      "command": "/full/path/to/python3",
      "args": ["/full/path/to/espn-fantasy-mcp/server.py"],
      "env": {
        "ESPN_LEAGUE_ID": "123456789",
        "ESPN_S2": "your espn_s2 cookie here",
        "ESPN_SWID": "{your-SWID-here}"
      }
    }
  }
}
```

Then fully quit and reopen Claude Desktop.

A mistake I ran into: you have to use the **full path** for both python and the script. Claude Desktop doesn't load your normal terminal setup, so just writing `python3` might point to a different Python that doesn't have the packages installed. Running `which python3` in your terminal tells you the full path.

**For Claude Code:**

```bash
claude mcp add espn-fantasy \
  --env ESPN_LEAGUE_ID=123456789 \
  --env ESPN_S2='your espn_s2 cookie here' \
  --env ESPN_SWID='{your-SWID-here}' \
  -- python3 /full/path/to/espn-fantasy-mcp/server.py
```

Then run `claude mcp list` to check it's there, and `/mcp` inside a session to see the tools.

## Using more than one league

I'm in a few leagues, so I added support for that. Instead of `ESPN_LEAGUE_ID`, set `ESPN_LEAGUES` and give each league a nickname:

```
ESPN_LEAGUES=work=123456,friends=789012,family=345678
```

Now you can say stuff like "show me the standings in my friends league" and Claude passes `league="friends"` to the tool.

If your leagues are on different ESPN accounts or different seasons, you can use JSON instead:

```json
{
  "work": "123456",
  "friends": {"id": "789012", "season": 2026},
  "family": {"id": "345678", "s2": "OTHER_COOKIE", "swid": "{OTHER-SWID}"}
}
```

Anything you leave out just uses the normal `ESPN_SEASON`, `ESPN_S2`, and `ESPN_SWID` values.

## All the settings

| Variable | Do I need it? | Default | What it's for |
| --- | --- | --- | --- |
| `ESPN_LEAGUE_ID` | Yes, if you have one league | none | Your league id |
| `ESPN_LEAGUES` | Yes, if you have multiple leagues | none | Nicknames and ids (see above) |
| `ESPN_LEAGUE_ALIAS` | No | `default` | Nickname for your one league |
| `ESPN_SEASON` | No | This year | Set it to look at past seasons |
| `ESPN_S2` | Only for private leagues | none | The `espn_s2` cookie |
| `ESPN_SWID` | Only for private leagues | none | The `SWID` cookie |
| `ESPN_CACHE_TTL` | No | `60` | How many seconds to remember ESPN's answer before asking again |
| `ESPN_SPORT` | No | `ffl` | `ffl` is football. Other sports kind of work but the positions are football ones. |

## Testing it

You can test it without Claude and without an ESPN account:

```bash
python3 test_offline.py
```

This fakes ESPN's responses for three made up leagues and runs every tool against them. If it ends with `multi-league routing OK`, everything is working.

To test with your real league:

```bash
ESPN_LEAGUE_ID=123456789 python3 -c "
import asyncio, server
print(asyncio.run(server.list_leagues()))
"
```

## How the trade stuff works

The server doesn't decide trades for you. It just gives Claude the data, and Claude does the thinking. The two main tools for this are:

- `compare_rosters(team_a, team_b)` shows both teams by position, and marks where each team is deep or thin. A good trade is usually when one team has too many of something the other team needs.
- `find_trade_targets(team_id, position)` lists every player at a position on other teams, and tells you if that owner has extras. If someone starts 2 RBs but has 5, they'll probably trade one. If they only have 2, they probably won't.

What I usually ask Claude to do: check `get_standings` to see who's winning and losing, then `find_trade_targets` to make a shortlist, then `compare_rosters` on the best one.

Heads up, the depth check just counts players, so it doesn't know about bye weeks or if a backup is actually any good. Also ESPN's projections aren't great. So don't just copy whatever Claude suggests straight into the app.

## Things that can go wrong

ESPN doesn't have an official public fantasy API. This project uses the same hidden endpoints the ESPN website uses, which means ESPN could change them at any time and break things. I tried to write the code so it doesn't crash if a field is missing, but a big change would still break it.

Other known issues:

- `get_recent_activity` doesn't work for some leagues because ESPN doesn't give that data for every league. It'll tell you instead of crashing.
- `get_player` downloads ESPN's whole player list the first time you use it (a few thousand players), so the first call is slow. After that it's cached for 6 hours.

## Troubleshooting

| Problem | What's probably wrong |
| --- | --- |
| "ESPN returned 401/403" | Your cookies are missing or expired, or your account isn't in that league |
| "ESPN returned 404" | Wrong league id, or the league didn't exist that season |
| "the league argument is required" | You have multiple leagues and didn't say which one. Run `list_leagues` to see the nicknames. |
| Server says disconnected in Claude | Probably the wrong Python path in your config |
| No tools show up | The server crashed when starting. Run `python3 server.py` in your terminal to see the error. |

## What I learned

- How MCP servers work and how Claude decides which tool to call
- Working with an undocumented API by looking at the requests in DevTools
- Using `async` / `await` in Python with `httpx`
- Caching so I don't spam ESPN with the same request
- Writing tests with fake data so I don't need internet to test
