# Lean Canvas (WIP) — MetroMinute

This is a working draft canvas for communicating the broader MetroMinute product scope while this repository remains focused on data gatherer + planner infrastructure.

## Problem
- Riders struggle to trust ETA and routing decisions when service conditions shift quickly.
- Static routing alone misses realtime variation in arrivals and ride durations.
- Existing transit predictions are useful but need validation and integration into routing decisions.

## Customer Segments
- Daily NYC subway riders making time-sensitive commutes.
- Riders making unfamiliar or transfer-heavy trips.
- Product and operations teams improving transit experience quality.

## Unique Value Proposition
- Route planning that combines graph topology with realtime and historically-informed prediction layers.
- A measurable loop: ingest data, validate prediction quality, and feed insights back into planner behavior.
- A practical bridge between transit data science work and rider-facing product decisions.

## Solution
- Pull GTFS-RT feeds and persist prediction-vs-observation logs.
- Build planner topology on `station|line|direction` nodes for transfer-aware routing.
- Apply a layered runtime signal stack:
  - MTA predictions
  - recent average calculations
  - GNN/historical ML outputs
- Expose planner through localhost/API/frontend surfaces for iteration.

## Channels
- Web and mobile planning interfaces.
- API endpoints for planner and diagnostics.
- Portfolio/demo materials (README, slide deck, pitch video).

## Revenue Streams (WIP)
- B2C subscription for premium routing insights (future hypothesis).
- B2B/API partnerships for route quality intelligence (future hypothesis).

## Cost Structure
- Infrastructure for feed ingestion, storage, and API serving.
- Model development and validation workflows.
- Engineering time for frontend/product iteration.

## Key Metrics
- Prediction accuracy deltas (predicted vs observed arrivals).
- Route ETA error and transfer recommendation quality.
- Planner response health and runtime-cache freshness.
- User-facing reliability outcomes (time savings, fewer missed transfers).

## Unfair Advantage
- Integrated pipeline from realtime capture to validation to route-planner weight updates.
- Direction-aware, line-aware graph formulation paired with ML-informed routing signals.
- Fast experiment loop that connects transit ML analysis directly to planner behavior.

## Validation Status Snapshot
- In place: realtime ingestion, SQL logging, weighted planner, localhost UI/API.
- In progress: broader model-serving automation and richer rider-facing product packaging.
