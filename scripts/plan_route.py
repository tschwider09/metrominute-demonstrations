#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from metrominute_rt.route_planner import RoutePlanner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single route-planner query from the command line.")
    parser.add_argument("--from", dest="origin", required=True, help="Origin station id/name (e.g. R16).")
    parser.add_argument("--to", dest="destination", required=True, help="Destination station id/name (e.g. A27).")
    parser.add_argument(
        "--station-graph-path",
        required=True,
        help="Path to station_details.json.",
    )
    parser.add_argument(
        "--gtfs-dir",
        dest="gtfs_dirs",
        action="append",
        required=True,
        help="Path to a GTFS directory (repeat this flag for multiple directories).",
    )
    parser.add_argument(
        "--matrix-path",
        default="",
        help="Optional non-weighted planning matrix json path.",
    )
    parser.add_argument("--refresh", action="store_true", help="Force planner graph refresh before query.")
    parser.add_argument("--ride-multiplier", type=float, default=1.0, help="Ride-edge multiplier.")
    parser.add_argument("--line-switch-penalty", type=float, default=0.5, help="Line switch penalty in minutes.")
    parser.add_argument(
        "--station-transfer-penalty",
        type=float,
        default=0.0,
        help="Station transfer penalty in minutes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    planner = RoutePlanner(
        station_graph_path=args.station_graph_path,
        gtfs_dirs=args.gtfs_dirs,
        non_weighted_matrix_path=args.matrix_path,
        line_switch_penalty_minutes=args.line_switch_penalty,
        station_transfer_penalty_minutes=args.station_transfer_penalty,
    )

    result = planner.plan(
        origin=args.origin,
        destination=args.destination,
        weights={
            "ride_multiplier": args.ride_multiplier,
            "line_switch_penalty": args.line_switch_penalty,
            "station_transfer_penalty": args.station_transfer_penalty,
        },
        force_refresh=args.refresh,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if int(result.get("status_code") or 200) == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())

