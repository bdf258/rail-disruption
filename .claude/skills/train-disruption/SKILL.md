---
name: train-disruption
description: Work out whether a GB train or route is cancelled or delayed, whether the disruption will last the rest of the day, which train the user is on and when it arrives, and the fastest working alternative route (including getting across London). Use when the user asks "is my train cancelled", "are trains to X running", "will it be like this all day", "how else can I get to X", "which train are we on", or "when do we get to X".
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/trains.py *)
---

# Train disruption: cancellations and alternative routes

Answer the user's travel question fast and plainly: they are usually at a station or on a train. Lead with the answer (cancelled or not, arrival time, which route to take), then the evidence.

All live lookups go through one script:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/trains.py <command> ...
```

## Setup

| Variable | Needed for | Notes |
|---|---|---|
| `RTT_API_TOKEN` | `train`, and timetable rows beyond Darwin's ~4-hour horizon | Realtime Trains API token from https://api-portal.rtt.io (free personal tier). The portal issues a refresh token; the script swaps it for a short-lived access token on each run. |
| `HUXLEY_URL` | optional | Darwin JSON proxy. Defaults to the public Huxley 2 demo, which has no uptime guarantee. |
| `DARWIN_ACCESS_TOKEN` | optional | National Rail Darwin (OpenLDBWS) token, sent to the proxy as `accessToken`. Use with a self-hosted Huxley 2 when the demo is down. |

In a cloud session, environment variables are set in the environment's settings (cloud environment menu in the session title bar, then Edit), not pasted into chat. If `RTT_API_TOKEN` is missing, carry on with Darwin-only results and say what is missing.

## Which source to believe

This is the core judgement, learned the hard way on 24 October 2026:

- **Darwin** (National Rail's live feed) carries the operator's cancellations hours in advance, with reasons. It is authoritative but only sees about four hours ahead.
- **Realtime Trains** has the whole day's timetable and exact live positions, but its cancellations come from Network Rail's TRUST system and usually appear only close to departure. That morning RTT showed every Liverpool Street to Cambridge train as running while Darwin had them all cancelled for overhead-wire damage.

So the script uses Darwin as far as it can see and labels anything later `timetable`. **Never tell the user a later train is running on the strength of a `timetable` row during known disruption.** If the script prints a `WARNING: Darwin live data unavailable`, every row is timetable-only: say so, and confirm cancellations elsewhere (below) before advising.

## Workflow

### 1. Pin down the stations

Use three-letter station codes. Look up any you aren't sure of:

```
trains.py crs "cambridge"        # CBG Cambridge, CMB Cambridge North, CMS Cambridge South, ...
```

London termini that often serve as alternatives for each other: LST Liverpool Street, KGX King's Cross, STP St Pancras (Thameslink low-level platforms), FST Fenchurch Street, EUS Euston, MYB Marylebone, PAD Paddington, VIC Victoria, LBG London Bridge, WAT Waterloo, CHX Charing Cross.

### 2. Is this train or route affected?

```
trains.py board LST --to CBG                 # rest of today, every service calling at CBG
trains.py board LST --at 17:00 --hours 3     # a later window, all destinations
```

The board ends with a summary (cancelled count and reasons in the live window, next running service) and National Rail's disruption messages for the station. Quote the reason given (for example "damage to the overhead electric wires").

### 3. Will it last the rest of the day?

Darwin can't see past about four hours, so look for a stated end time:

1. The disruption messages printed by `board` / `journey`. They usually link a page like `https://www.nationalrail.co.uk/service-disruptions/<place>-<yyyymmdd>/`; fetch it.
2. Search the web for the operator's statement (`<operator> <place> disruption <date>`). Local news often reprints the operator's "disruption expected until ..." line and its full cancellation list.
3. The operator's JourneyCheck page (e.g. `https://www.journeycheck.com/greateranglia/`) when it renders.

Report the stated estimate and its source. If there is none, say the live data shows cancellations up to the horizon and that nothing official says when it ends. Don't extrapolate from the timetable.

### 4. Find alternatives

Find other stations with direct trains to the destination, and see which are working:

```
trains.py direct-to CBG --match London
```

Then compare whole routes. `+N` is the minutes needed to reach the first station; multi-station routes such as `LST,ELY,KLN` include a change (`--change` sets the minimum, default 5 minutes):

```
trains.py journey --route LST,CBG --route KGX,CBG+15 --route STP,CBG+15
trains.py journey --route LST,ELY,KLN --at 14:00 --hours 3 --change 8
```

Get `N` for crossing London from TfL, and check the Underground for closures (weekend part-closures are common):

```
trains.py tfl "Liverpool Street" "Kings Cross St Pancras"
trains.py london-status
```

Recommend the route with the earliest reliable arrival. Prefer live rows over `[timetable only]` ones, and mention a fallback.

### 5. Which train am I on, and when do we arrive?

```
trains.py train STP 10:40 --to CMS
```

The time can be the scheduled or the actual departure ("the 10:31" or "it left at 10:40"). Use `--to` with any later stop to pick the right train. It prints the operator, route and train number, then each stop's scheduled, actual and forecast times and platform. Needs `RTT_API_TOKEN`.

## Reporting

- First line: the direct answer, e.g. "Yes, the 10:18 is cancelled (overhead-wire damage at Stansted Mountfitchet). Go from King's Cross: the 11:24 gets you to Cambridge at 12:12."
- Then the evidence in a few bullets: live counts, stated end time and source, alternatives with times and platforms.
- Say which rows are live and which are timetable-only whenever it matters.
- Cite the National Rail or operator pages and news articles you used.

## Maintenance notes

- Darwin through Huxley returns at most 10 services per board when calling points are included (`journey`, `direct-to`). The script then treats Darwin as complete only up to the last row and fills in from RTT.
- If the public Huxley demo is down for long, self-host Huxley 2 (Docker image in the jpsingleton/Huxley2 repository) with a Darwin token from National Rail / the Rail Data Marketplace, and set `HUXLEY_URL` and `DARWIN_ACCESS_TOKEN`.
- API references: RTT https://realtimetrains.github.io/api-specification/ (OpenAPI at `specification/main.yml`), Huxley 2 https://huxley2.azurewebsites.net/, TfL https://api.tfl.gov.uk/.
- Tests: `python3 ${CLAUDE_SKILL_DIR}/scripts/test_trains.py`.
