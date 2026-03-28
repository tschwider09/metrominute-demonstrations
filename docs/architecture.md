# Architecture

## Data Gatherer Flow
1. Pull GTFS-RT payloads from configured feed base URLs and paths.
2. Parse vehicle positions and trip updates.
3. Classify snapshot health:
- `ok`: healthy payload with vehicle coverage
- `degraded`: partial coverage or fallback usage
- `down`: hard failure / no usable realtime data
4. Persist SQL logs:
- `realtime_snapshot_log`
- `trip_update_prediction_log`
- `vehicle_stop_arrival_log`
- `trip_update_arrival_eval_log`

## Route Planner Flow
1. Load station graph and GTFS files.
2. Build node graph with `(station, line, direction)` granularity.
3. Run weighted shortest path with transfer penalties.
4. Return structured route summary, legs, and runtime cache status.

## Hosted UI Layer
1. Flask web app (`metrominute_rt.webapp`) exposes planner endpoints and runtime health.
2. Frontend UI provides tabbed route-planning, station lookup, and diagnostics views.
3. UI consumes planner API responses and renders route legs, summary metrics, and graph/runtime metadata.

## Why This Split
- Recorder can run as a scheduler/cron service.
- Recorder is Flask-compatible and can share the same `.env`/database config as the API deployment.
- Planner can run as API-facing or CLI-only service.
- Failure domains are separated: upstream feed instability does not block planner graph logic.
