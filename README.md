# rail-disruption

A Claude Code skill for GB rail disruption: is a train or route cancelled, will it last the rest of the day, which train you're on and when it arrives, and the fastest working alternative (including getting across London).

The skill lives in `.claude/skills/train-disruption/`, so Claude Code loads it automatically when run from this repo. Everything goes through one stdlib-only Python script (3.10+):

```
python3 .claude/skills/train-disruption/scripts/trains.py <command> ...
```

Commands: `board`, `train`, `journey`, `direct-to`, `tfl`, `london-status`, `crs`. See `SKILL.md` for the workflow and how it decides between data sources.

## Data sources

- **National Rail Darwin**, via a Huxley 2 proxy: live cancellations with reasons, about four hours ahead.
- **Realtime Trains**: the whole day's timetable and per-train detail. Needs `RTT_API_TOKEN` (free personal tier at https://api-portal.rtt.io).
- **TfL journey planner and line status**: crossing London.

Optional: `HUXLEY_URL` and `DARWIN_ACCESS_TOKEN` to use a self-hosted Huxley 2 instead of the public demo.

## Tests

```
cd .claude/skills/train-disruption/scripts && python3 -m unittest test_trains
```
