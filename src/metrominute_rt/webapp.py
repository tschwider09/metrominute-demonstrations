from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from .route_planner import RoutePlanner


def _split_csv(raw_value: str) -> list[str]:
    return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]


def _parse_bool(raw_value: str, default: bool = False) -> bool:
    value = str(raw_value if raw_value is not None else str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _query_float(name: str, default: float | None, minimum: float | None = None, maximum: float | None = None):
    raw_value = request.args.get(name, None)
    if raw_value is None or str(raw_value).strip() == "":
        return default, None
    try:
        value = float(raw_value)
    except ValueError:
        return None, f"Query param '{name}' must be a number."
    if minimum is not None and value < minimum:
        return None, f"Query param '{name}' must be >= {minimum}."
    if maximum is not None and value > maximum:
        return None, f"Query param '{name}' must be <= {maximum}."
    return value, None


def _query_int(name: str, default: int | None, minimum: int | None = None, maximum: int | None = None):
    raw_value = request.args.get(name, None)
    if raw_value is None or str(raw_value).strip() == "":
        return default, None
    try:
        value = int(raw_value)
    except ValueError:
        return None, f"Query param '{name}' must be an integer."
    if minimum is not None and value < minimum:
        return None, f"Query param '{name}' must be >= {minimum}."
    if maximum is not None and value > maximum:
        return None, f"Query param '{name}' must be <= {maximum}."
    return value, None


def _query_bool(name: str, default: bool | None):
    raw_value = request.args.get(name, None)
    if raw_value is None or str(raw_value).strip() == "":
        return default, None
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True, None
    if normalized in {"0", "false", "no", "off"}:
        return False, None
    return None, f"Query param '{name}' must be one of true/false/1/0."


def _query_limit(default_limit: int = 200):
    limit, limit_error = _query_int("limit", default_limit, 1, 1000)
    if limit_error:
        return None, limit_error
    return int(limit), None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_existing_path(candidates: list[str]) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.exists():
            return str(path)
    return ""


def _resolve_station_graph_path(explicit_path: str) -> str:
    if explicit_path:
        return _find_existing_path([explicit_path])
    repo_root = _repo_root()
    return _find_existing_path(
        [
            os.getenv("MMRT_STATION_GRAPH_PATH", ""),
            str(repo_root / "data" / "station_details.json"),
            str(repo_root.parent / "MetroMinuteNYC" / "app" / "static" / "data" / "station_details.json"),
            str(repo_root.parent / "app" / "static" / "data" / "station_details.json"),
        ]
    )


def _resolve_gtfs_dirs(explicit_dirs: list[str]) -> list[str]:
    if explicit_dirs:
        return [str(Path(path).expanduser().resolve()) for path in explicit_dirs if Path(path).expanduser().exists()]
    repo_root = _repo_root()
    env_dirs = _split_csv(os.getenv("MMRT_GTFS_DIRS", ""))
    candidates = env_dirs or [
        str(repo_root / "data" / "gtfs_static"),
        str(repo_root.parent / "MetroMinuteNYC" / "app" / "data" / "gtfs_static"),
        str(repo_root.parent / "app" / "data" / "gtfs_static"),
    ]
    resolved: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser().resolve()
        if path.exists() and path.is_dir():
            resolved.append(str(path))
    return resolved


def _resolve_matrix_path(explicit_path: str) -> str:
    if explicit_path:
        return _find_existing_path([explicit_path])
    repo_root = _repo_root()
    return _find_existing_path(
        [
            os.getenv("MMRT_MATRIX_PATH", ""),
            str(repo_root / "data" / "non_weighted_planning_graph_matrix.json"),
            str(repo_root.parent / "MetroMinuteNYC" / "app" / "data" / "non_weighted_planning_graph_matrix.json"),
            str(repo_root.parent / "app" / "data" / "non_weighted_planning_graph_matrix.json"),
        ]
    )


def _planner_station_catalog(planner: RoutePlanner, force_refresh: bool = False):
    cache = planner._ensure_loaded(force_refresh=force_refresh)
    if not cache.get("available"):
        return cache, []

    stations = cache.get("stations") or {}
    station_lines = cache.get("station_lines") or {}
    station_direction_map = cache.get("station_line_directions") or {}
    rows: list[dict[str, Any]] = []
    for station_id, station in stations.items():
        name = str((station or {}).get("name") or station_id)
        borough = str((station or {}).get("borough") or "")
        lines = sorted(station_lines.get(station_id) or [])
        directions = station_direction_map.get(station_id) or {}
        rows.append(
            {
                "station_id": str(station_id),
                "name": name,
                "borough": borough,
                "latitude": float((station or {}).get("latitude") or 0.0),
                "longitude": float((station or {}).get("longitude") or 0.0),
                "lines": lines,
                "line_directions": directions,
                "label": f"{name} ({station_id})",
            }
        )
    rows.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("station_id") or "")))
    return cache, rows


def create_web_app(overrides: dict[str, Any] | None = None) -> Flask:
    load_dotenv()
    overrides = overrides or {}

    web_root = Path(__file__).resolve().parent / "web"
    app = Flask(
        __name__,
        template_folder=str(web_root / "templates"),
        static_folder=str(web_root / "static"),
        static_url_path="/static",
    )

    station_graph_path = _resolve_station_graph_path(str(overrides.get("station_graph_path") or ""))
    gtfs_dirs = _resolve_gtfs_dirs(list(overrides.get("gtfs_dirs") or []))
    matrix_path = _resolve_matrix_path(str(overrides.get("matrix_path") or ""))

    cache_seconds = int(overrides.get("cache_seconds") or os.getenv("MMRT_PLANNER_CACHE_SECONDS", "900"))
    line_switch_penalty = float(
        overrides.get("line_switch_penalty") or os.getenv("MMRT_LINE_SWITCH_PENALTY_MINUTES", "0.5")
    )
    station_transfer_penalty = float(
        overrides.get("station_transfer_penalty") or os.getenv("MMRT_STATION_TRANSFER_PENALTY_MINUTES", "0.0")
    )

    planner: RoutePlanner | None = None
    planner_boot_error = ""
    if not station_graph_path:
        planner_boot_error = (
            "Planner not configured: station graph path is missing. "
            "Set MMRT_STATION_GRAPH_PATH or pass --station-graph-path."
        )
    elif not gtfs_dirs:
        planner_boot_error = (
            "Planner not configured: GTFS directory list is missing. "
            "Set MMRT_GTFS_DIRS or pass one or more --gtfs-dir values."
        )
    else:
        try:
            planner = RoutePlanner(
                station_graph_path=station_graph_path,
                gtfs_dirs=gtfs_dirs,
                non_weighted_matrix_path=matrix_path,
                cache_seconds=cache_seconds,
                line_switch_penalty_minutes=line_switch_penalty,
                station_transfer_penalty_minutes=station_transfer_penalty,
            )
        except Exception as exc:
            planner_boot_error = f"RoutePlanner initialization failed: {exc}"

    app.extensions["route_planner"] = planner
    app.extensions["route_planner_boot_error"] = planner_boot_error
    app.extensions["route_planner_paths"] = {
        "station_graph_path": station_graph_path,
        "gtfs_dirs": gtfs_dirs,
        "matrix_path": matrix_path,
    }

    def planner_or_response():
        active_planner = app.extensions.get("route_planner")
        if active_planner:
            return active_planner, None
        error_message = str(app.extensions.get("route_planner_boot_error") or "Route planner is not configured.")
        return None, (jsonify({"available": False, "error": error_message}), 503)

    @app.get("/")
    def index():
        active_planner = app.extensions.get("route_planner")
        boot_error = str(app.extensions.get("route_planner_boot_error") or "")
        station_rows: list[dict[str, Any]] = []
        cache_snapshot: dict[str, Any] = {}
        runtime_snapshot: dict[str, Any] = {}

        if active_planner:
            cache_snapshot, station_rows = _planner_station_catalog(active_planner, force_refresh=False)
            if not cache_snapshot.get("available"):
                boot_error = str(cache_snapshot.get("error") or "Planner graph unavailable.")
            runtime_snapshot = active_planner.runtime_status()

        bootstrap_data = {
            "planner_available": bool(active_planner and cache_snapshot.get("available", True)),
            "planner_error": boot_error,
            "station_count": len(station_rows),
            "stations": station_rows[:1500],
            "runtime": runtime_snapshot,
            "graph": {
                "node_count": int(cache_snapshot.get("node_count") or 0),
                "edge_count": int(cache_snapshot.get("edge_count") or 0),
                "source": cache_snapshot.get("source") or app.extensions.get("route_planner_paths") or {},
            },
        }
        return render_template("index.html", bootstrap_data=bootstrap_data)

    @app.get("/api/health")
    def health():
        active_planner = app.extensions.get("route_planner")
        return jsonify(
            {
                "ok": True,
                "planner_configured": bool(active_planner),
                "planner_boot_error": str(app.extensions.get("route_planner_boot_error") or ""),
            }
        )

    @app.get("/api/stations")
    def stations():
        active_planner, error_response = planner_or_response()
        if error_response:
            return error_response

        q = str(request.args.get("q", "") or "").strip().lower()
        limit, limit_error = _query_limit(default_limit=200)
        if limit_error:
            return jsonify({"error": limit_error}), 400

        force_refresh = _parse_bool(request.args.get("refresh", "0"), default=False)
        cache_snapshot, station_rows = _planner_station_catalog(active_planner, force_refresh=force_refresh)
        if not cache_snapshot.get("available"):
            return jsonify({"available": False, "error": cache_snapshot.get("error") or "Planner unavailable."}), 503

        if q:
            filtered_rows = [
                row
                for row in station_rows
                if q in str(row.get("name") or "").lower()
                or q in str(row.get("station_id") or "").lower()
                or any(q == str(line).lower() for line in (row.get("lines") or []))
            ]
        else:
            filtered_rows = station_rows

        return jsonify(
            {
                "available": True,
                "count": len(filtered_rows[:limit]),
                "total_count": len(filtered_rows),
                "stations": filtered_rows[:limit],
            }
        )

    @app.get("/api/runtime")
    def runtime():
        active_planner, error_response = planner_or_response()
        if error_response:
            return error_response

        force_refresh = _parse_bool(request.args.get("refresh", "0"), default=False)
        cache_snapshot = active_planner._ensure_loaded(force_refresh=force_refresh)
        runtime_snapshot = active_planner.runtime_status()
        return jsonify(
            {
                "available": bool(cache_snapshot.get("available")),
                "error": str(cache_snapshot.get("error") or ""),
                "runtime": runtime_snapshot,
                "graph": {
                    "node_count": int(cache_snapshot.get("node_count") or 0),
                    "edge_count": int(cache_snapshot.get("edge_count") or 0),
                    "source": cache_snapshot.get("source") or {},
                },
            }
        )

    @app.get("/api/routes/plan")
    def routes_plan():
        active_planner, error_response = planner_or_response()
        if error_response:
            return error_response

        origin = str(request.args.get("from", "") or "").strip()
        destination = str(request.args.get("to", "") or "").strip()
        if not origin or not destination:
            return jsonify({"error": "Query params 'from' and 'to' are required."}), 400

        force_refresh = _parse_bool(request.args.get("refresh", "0"), default=False)

        ride_multiplier, ride_error = _query_float("ride_multiplier", None, minimum=0.01, maximum=10.0)
        if ride_error:
            return jsonify({"error": ride_error}), 400

        direction_switch_penalty, direction_error = _query_float(
            "direction_switch_penalty", None, minimum=0.0, maximum=60.0
        )
        if direction_error:
            return jsonify({"error": direction_error}), 400

        line_switch_penalty, line_error = _query_float("line_switch_penalty", None, minimum=0.0, maximum=60.0)
        if line_error:
            return jsonify({"error": line_error}), 400

        station_transfer_penalty, transfer_error = _query_float(
            "station_transfer_penalty", None, minimum=0.0, maximum=60.0
        )
        if transfer_error:
            return jsonify({"error": transfer_error}), 400

        transfer_time_multiplier, transfer_mult_error = _query_float(
            "transfer_time_multiplier", None, minimum=0.0, maximum=10.0
        )
        if transfer_mult_error:
            return jsonify({"error": transfer_mult_error}), 400

        dynamic_wait_enabled, wait_error = _query_bool("dynamic_wait_enabled", None)
        if wait_error:
            return jsonify({"error": wait_error}), 400

        default_wait_minutes, wait_minutes_error = _query_float(
            "default_wait_minutes", None, minimum=0.0, maximum=60.0
        )
        if wait_minutes_error:
            return jsonify({"error": wait_minutes_error}), 400

        arrival_limit, arrival_error = _query_int("arrival_limit", None, minimum=1, maximum=10)
        if arrival_error:
            return jsonify({"error": arrival_error}), 400

        weights: dict[str, Any] = {}
        if ride_multiplier is not None:
            weights["ride_multiplier"] = ride_multiplier
        if direction_switch_penalty is not None:
            weights["direction_switch_penalty"] = direction_switch_penalty
        if line_switch_penalty is not None:
            weights["line_switch_penalty"] = line_switch_penalty
        if station_transfer_penalty is not None:
            weights["station_transfer_penalty"] = station_transfer_penalty
        if transfer_time_multiplier is not None:
            weights["transfer_time_multiplier"] = transfer_time_multiplier
        if dynamic_wait_enabled is not None:
            weights["dynamic_wait_enabled"] = dynamic_wait_enabled
        if default_wait_minutes is not None:
            weights["default_wait_minutes"] = default_wait_minutes
        if arrival_limit is not None:
            weights["arrival_limit"] = arrival_limit

        result = active_planner.plan(
            origin=origin,
            destination=destination,
            weights=weights or None,
            force_refresh=force_refresh,
        )
        status_code = int(result.get("status_code") or 200)
        payload = dict(result)
        payload.pop("status_code", None)
        return jsonify(payload), status_code

    return app


def main() -> int:
    app = create_web_app()
    app.run(
        host=os.getenv("MMRT_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("MMRT_WEB_PORT", "8080")),
        debug=_parse_bool(os.getenv("MMRT_WEB_DEBUG", "1"), default=True),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
