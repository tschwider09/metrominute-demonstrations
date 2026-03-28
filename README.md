# MetroMinute RT Data Gatherer + Route Planner

Portfolio repository focused on two production-relevant transit AI/data components from MetroMinute:
- Real-time GTFS-RT data gathering + SQL logging
- Weighted subway route planning

## Quick Gloss
- Real-time collector ingests MTA GTFS-RT feeds and stores prediction-vs-actual evaluation data.
- Route planner computes transfer-aware paths using line and direction-aware graph nodes.
- Error handling is tuned to reduce false alarm noise:
  - partial feed issues are reported as degraded warnings
  - hard errors are reserved for true outages

## Project Components

### 1) RT Data Gatherer
File:
- `src/metrominute_rt/realtime_recorder.py`

What it does:
- Polls multiple MTA GTFS-RT feeds
- Persists snapshots, trip update predictions, inferred arrivals, and match/eval rows
- Uses cache fallback for temporary upstream issues
- Runs cleanly as a Flask-side background service (cron/worker) with shared env config

Key reliability improvements:
- Introduces snapshot `status` (`ok`, `degraded`, `down`)
- Uses `warning` for partial degradation (for example, trip updates available but vehicle positions missing)
- Keeps `error` for hard-failure conditions only

Run:
```bash
python scripts/run_recorder.py --interval-seconds 20 --prediction-field arrival --run-label local-test
```

### 2) Route Planner
File:
- `src/metrominute_rt/route_planner.py`

What it does:
- Builds a weighted transit graph from station + GTFS data
- Plans routes with transfer and line-switch penalties
- Supports dynamic runtime indices from realtime trip updates

Run one query:
```bash
python scripts/plan_route.py \
  --from R16 \
  --to A27 \
  --station-graph-path /absolute/path/to/station_details.json \
  --gtfs-dir /absolute/path/to/gtfs_static \
  --matrix-path /absolute/path/to/non_weighted_planning_graph_matrix.json
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Set `.env` values:
- `DATABASE_URL` required for recorder
- `MTA_API_KEY` optional but recommended for full realtime coverage

Flask deployment note:
- This recorder is intended to run beside Flask (same runtime env, same DB URL), not inside request handlers.
- Preferred production mode is scheduler/cron invocation of `scripts/run_recorder.py`.

## Repository Layout

```text
src/metrominute_rt/
  realtime_recorder.py
  route_planner.py
scripts/
  run_recorder.py
  plan_route.py
docs/
  architecture.md
```

## Notes
- GTFS-RT upstream behavior can vary by feed and auth mode.
- In degraded states, this repo reports warnings instead of escalating every partial issue as a hard error.
- This repo intentionally separates data gathering and planning concerns for easier deployment.
