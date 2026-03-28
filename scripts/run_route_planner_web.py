#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from metrominute_rt.webapp import create_web_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the MetroMinute route planner web UI on localhost only."
    )
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080).")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode.")

    parser.add_argument("--station-graph-path", default="", help="Path to station_details.json.")
    parser.add_argument(
        "--gtfs-dir",
        dest="gtfs_dirs",
        action="append",
        default=[],
        help="Path to GTFS static directory. Repeat for multiple candidates.",
    )
    parser.add_argument("--matrix-path", default="", help="Optional non-weighted planning matrix JSON path.")
    parser.add_argument("--cache-seconds", type=int, default=900, help="Planner cache seconds.")
    parser.add_argument("--line-switch-penalty", type=float, default=0.5, help="Default line switch penalty.")
    parser.add_argument("--station-transfer-penalty", type=float, default=0.0, help="Default station transfer penalty.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = create_web_app(
        {
            "station_graph_path": args.station_graph_path,
            "gtfs_dirs": args.gtfs_dirs,
            "matrix_path": args.matrix_path,
            "cache_seconds": args.cache_seconds,
            "line_switch_penalty": args.line_switch_penalty,
            "station_transfer_penalty": args.station_transfer_penalty,
        }
    )
    host = "127.0.0.1"
    print(f"Starting localhost planner UI at http://{host}:{args.port}/")
    app.run(host=host, port=args.port, debug=bool(args.debug))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
