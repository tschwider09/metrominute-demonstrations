import csv
import datetime as dt
import heapq
import json
import os
import threading
import time
from bisect import bisect_left
from typing import Any


def _normalize_line(raw_value: str) -> str:
    value = str(raw_value or "").strip().upper()
    if not value:
        return ""
    if value.endswith("X") and len(value) > 1:
        value = value[:-1]
    if value == "SIR":
        return "SI"
    return value


def _normalize_direction(raw_value: str) -> str:
    value = str(raw_value or "").strip().lower()
    if not value:
        return ""
    if value in {"n", "north", "0"}:
        return "north"
    if value in {"s", "south", "1"}:
        return "south"
    return value


def _direction_from_stop_id(stop_id: str) -> str:
    value = str(stop_id or "").strip().upper()
    if value.endswith("N"):
        return "north"
    if value.endswith("S"):
        return "south"
    return ""


def _opposite_direction(direction: str) -> str:
    normalized = _normalize_direction(direction)
    if normalized == "north":
        return "south"
    if normalized == "south":
        return "north"
    return ""


def _direction_between_adjacent_stations(
    from_station: str,
    to_station: str,
    station_direction_neighbors: dict[str, dict[str, set[str]]],
) -> str:
    if to_station in (station_direction_neighbors.get(from_station) or {}).get("north", set()):
        return "north"
    if to_station in (station_direction_neighbors.get(from_station) or {}).get("south", set()):
        return "south"
    return ""


def _node_key(station_id: str, line_id: str, direction: str = "") -> str:
    normalized_direction = _normalize_direction(direction)
    if normalized_direction:
        return f"{station_id}|{line_id}|{normalized_direction}"
    return f"{station_id}|{line_id}"


def _split_node_key(node_key: str) -> tuple[str, str, str]:
    parts = str(node_key or "").split("|")
    if len(parts) == 3:
        return parts[0], parts[1], _normalize_direction(parts[2])
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return "", "", ""


def _parse_gtfs_time_to_seconds(raw_value: str) -> int | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
    except ValueError:
        return None
    if minutes < 0 or minutes > 59 or seconds < 0 or seconds > 59 or hours < 0:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _base_stop_id(stop_id: str) -> str:
    value = str(stop_id or "").strip().upper()
    if value.endswith(("N", "S")) and len(value) > 1:
        return value[:-1]
    return value


def _to_float(raw_value: Any, default_value: float) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return float(default_value)
    if not (value == value):  # NaN check
        return float(default_value)
    return value


def _to_bool(raw_value: Any, default_value: bool = False) -> bool:
    if raw_value is None:
        return bool(default_value)
    value = str(raw_value).strip().lower()
    if not value:
        return bool(default_value)
    return value in {"1", "true", "yes", "on"}


def _haversine_miles(from_coords: tuple[float, float], to_coords: tuple[float, float]) -> float:
    from_lat, from_lon = from_coords
    to_lat, to_lon = to_coords
    import math

    lon1, lat1, lon2, lat2 = map(math.radians, [from_lon, from_lat, to_lon, to_lat])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return 3958.7613 * c


def _estimate_neighbor_ride_minutes(
    from_coords: tuple[float, float],
    to_coords: tuple[float, float],
) -> float:
    """
    Fallback for missing GTFS segment timing.
    Uses a conservative in-tunnel speed + small dwell buffer.
    """
    distance_miles = _haversine_miles(from_coords, to_coords)
    if distance_miles <= 0:
        return 1.5
    cruise_minutes = (distance_miles / 21.0) * 60.0
    return max(0.9, min(cruise_minutes + 0.8, 10.0))


class RideTimeModel:
    """
    Skeleton interface for ride-edge prediction models.
    Implementors should return predicted ride minutes or None to fall back.

    Primary feature inputs:
      - from_station_id
      - to_station_id
      - direction
      - query_epoch_seconds / minute_of_day_local
      - day_of_week_local
      - recent_rides
    """

    def predict_ride_minutes(
        self,
        edge: dict[str, Any],
        current_cost_minutes: float,
        from_station_id: str = "",
        to_station_id: str = "",
        line_id: str = "",
        direction: str = "",
        query_epoch_seconds: float | None = None,
        minute_of_day_local: int | None = None,
        day_of_week_local: str = "",
        recent_rides: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> float | None:
        return None


class ArrivalModel:
    """
    Skeleton interface for next-arrival prediction models.
    Expected return: absolute minutes-from-route-start for the next arrivals, sorted ascending.
    Example: [3.0, 7.0, 12.0]
    """

    def predict_next_arrivals(
        self,
        station_id: str,
        line_id: str,
        direction: str,
        at_minute: float,
        limit: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[float]:
        return []


class RoutePlanner:
    """
    Weighted subway route planner using (station, line, direction) nodes and Dijkstra.

    Nodes:
      - (station_id, line_id, direction)
    Edge kinds:
      - ride: move between stations on same line and direction
      - direction_switch: same station and line, switch platform/direction
      - line_switch: same station, switch line and optionally direction
      - station_transfer: linked stations, then board another line/direction

    Topology source:
      - Prefers precomputed non-weighted matrix when available.
      - Supports direction-aware (`station|line|direction`) matrix, line-aware
        (`station|line`) matrix, or station-only matrix.
      - Falls back to station graph neighbor links when matrix is unavailable.
    """

    REQUIRED_GTFS_FILES = ("stops.txt", "trips.txt", "stop_times.txt")

    def __init__(
        self,
        station_graph_path: str,
        gtfs_dirs: list[str],
        non_weighted_matrix_path: str = "",
        cache_seconds: int = 900,
        line_switch_penalty_minutes: float = 0.5,
        station_transfer_penalty_minutes: float = 0.0,
        ride_time_model: RideTimeModel | None = None,
        arrival_model: ArrivalModel | None = None,
    ):
        self.station_graph_path = os.path.abspath(station_graph_path)
        self.gtfs_dirs = [os.path.abspath(str(path)) for path in gtfs_dirs if str(path).strip()]
        self.non_weighted_matrix_path = (
            os.path.abspath(str(non_weighted_matrix_path))
            if str(non_weighted_matrix_path).strip()
            else ""
        )
        self.cache_seconds = max(60, int(cache_seconds))
        self.line_switch_penalty_minutes = max(0.0, float(line_switch_penalty_minutes))
        self.station_transfer_penalty_minutes = max(0.0, float(station_transfer_penalty_minutes))
        self.ride_time_model = ride_time_model or RideTimeModel()
        self.arrival_model = arrival_model or ArrivalModel()

        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {
            "loaded_at": 0,
            "available": False,
            "error": "Route planner graph not loaded.",
            "source": {
                "station_graph_path": self.station_graph_path,
                "non_weighted_matrix_path": self.non_weighted_matrix_path,
                "topology_source": "",
                "matrix_status": "not_loaded",
                "matrix_node_mode": "none",
                "gtfs_dir": "",
            },
            "stations": {},
            "stations_by_name": {},
            "station_lines": {},
            "station_line_directions": {},
            "station_neighbors": {},
            "station_direction_neighbors": {},
            "stop_to_station": {},
            "adjacency": {},
            "node_count": 0,
            "edge_count": 0,
        }
        self._runtime_lock = threading.Lock()
        self._runtime_cache: dict[str, Any] = {
            "version": 0,
            "built_at": 0.0,
            "source_timestamp": None,
            "arrival_index": {},
            "ride_index": {},
            "error": "",
            "arrival_refreshed_at": 0.0,
            "ride_refreshed_at": 0.0,
        }

    @staticmethod
    def _normalize_day_of_week(raw_value: Any) -> str:
        value = str(raw_value or "").strip().lower()
        if not value:
            return ""
        normalized_lookup = {
            "0": "MON",
            "1": "TUE",
            "2": "WED",
            "3": "THU",
            "4": "FRI",
            "5": "SAT",
            "6": "SUN",
            "mon": "MON",
            "monday": "MON",
            "tue": "TUE",
            "tues": "TUE",
            "tuesday": "TUE",
            "wed": "WED",
            "wednesday": "WED",
            "thu": "THU",
            "thur": "THU",
            "thurs": "THU",
            "thursday": "THU",
            "fri": "FRI",
            "friday": "FRI",
            "sat": "SAT",
            "saturday": "SAT",
            "sun": "SUN",
            "sunday": "SUN",
        }
        return normalized_lookup.get(value, "")

    @staticmethod
    def _normalize_recent_rides(raw_value: Any) -> list[dict[str, Any]]:
        if raw_value is None:
            return []
        if isinstance(raw_value, list):
            return [item for item in raw_value if isinstance(item, dict)]
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if not value:
                return []
            try:
                parsed = json.loads(value)
            except Exception:
                return []
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
            return []
        return []

    def _pick_gtfs_dir(self) -> str:
        for directory in self.gtfs_dirs:
            if not directory or not os.path.isdir(directory):
                continue
            if all(os.path.exists(os.path.join(directory, filename)) for filename in self.REQUIRED_GTFS_FILES):
                return directory
        return ""

    def _load_station_graph(self) -> dict[str, dict]:
        if not os.path.exists(self.station_graph_path):
            raise FileNotFoundError(f"Station graph JSON not found: {self.station_graph_path}")
        with open(self.station_graph_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("Station graph JSON must be an object keyed by station ID.")
        return raw

    @staticmethod
    def _build_station_topology_from_station_graph(
        station_graph: dict[str, dict[str, Any]],
        station_ids: set[str],
    ) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
        station_neighbors: dict[str, set[str]] = {station_id: set() for station_id in station_ids}
        station_direction_neighbors: dict[str, dict[str, set[str]]] = {
            station_id: {"north": set(), "south": set()}
            for station_id in station_ids
        }
        for station_id, details in station_graph.items():
            if station_id not in station_ids or not isinstance(details, dict):
                continue
            for direction_key in ("north", "south"):
                direction_blob = details.get(direction_key) or {}
                if not isinstance(direction_blob, dict):
                    continue
                for raw_neighbor in direction_blob.keys():
                    neighbor_id = str(raw_neighbor or "").strip()
                    if not neighbor_id or neighbor_id not in station_ids or neighbor_id == station_id:
                        continue
                    station_neighbors[station_id].add(neighbor_id)
                    station_neighbors[neighbor_id].add(station_id)
                    station_direction_neighbors[station_id][direction_key].add(neighbor_id)
        return station_neighbors, station_direction_neighbors

    @staticmethod
    def _is_reachable_in_direction(
        from_station: str,
        to_station: str,
        direction: str,
        station_direction_neighbors: dict[str, dict[str, set[str]]],
    ) -> bool:
        normalized_direction = _normalize_direction(direction)
        if not from_station or not to_station or not normalized_direction or from_station == to_station:
            return False

        visited = {from_station}
        queue = [from_station]
        while queue:
            current_station = queue.pop(0)
            neighbors = (station_direction_neighbors.get(current_station) or {}).get(normalized_direction, set())
            for neighbor in neighbors:
                if neighbor == to_station:
                    return True
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        return False

    def _resolve_ride_direction(
        self,
        from_station: str,
        to_station: str,
        direction_hint: str,
        station_direction_neighbors: dict[str, dict[str, set[str]]],
        relation_cache: dict[tuple[str, str, str], str],
    ) -> str:
        cache_key = (from_station, to_station, _normalize_direction(direction_hint))
        cached = relation_cache.get(cache_key)
        if cached is not None:
            return cached

        direct_relation = _direction_between_adjacent_stations(
            from_station=from_station,
            to_station=to_station,
            station_direction_neighbors=station_direction_neighbors,
        )
        if direct_relation:
            relation_cache[cache_key] = direct_relation
            return direct_relation

        normalized_hint = _normalize_direction(direction_hint)
        if normalized_hint and self._is_reachable_in_direction(
            from_station=from_station,
            to_station=to_station,
            direction=normalized_hint,
            station_direction_neighbors=station_direction_neighbors,
        ):
            relation_cache[cache_key] = normalized_hint
            return normalized_hint

        north_reachable = self._is_reachable_in_direction(
            from_station=from_station,
            to_station=to_station,
            direction="north",
            station_direction_neighbors=station_direction_neighbors,
        )
        south_reachable = self._is_reachable_in_direction(
            from_station=from_station,
            to_station=to_station,
            direction="south",
            station_direction_neighbors=station_direction_neighbors,
        )
        if north_reachable and not south_reachable:
            relation_cache[cache_key] = "north"
            return "north"
        if south_reachable and not north_reachable:
            relation_cache[cache_key] = "south"
            return "south"

        relation_cache[cache_key] = ""
        return ""

    def _load_non_weighted_matrix_neighbors(
        self,
        allowed_station_ids: set[str],
    ) -> tuple[dict[str, set[str]], set[str], str, str]:
        if not self.non_weighted_matrix_path:
            return {}, set(), "disabled", "none"
        if not os.path.exists(self.non_weighted_matrix_path):
            return {}, set(), "missing", "none"

        try:
            with open(self.non_weighted_matrix_path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            return {}, set(), f"invalid_json:{exc}", "none"

        node_order_raw = raw.get("node_order") or []
        matrix_rows = raw.get("matrix") or []
        if not isinstance(node_order_raw, list) or not isinstance(matrix_rows, list):
            return {}, set(), "invalid_shape", "none"
        if len(node_order_raw) != len(matrix_rows):
            return {}, set(), "invalid_dimensions", "none"

        node_order = [str(station_id or "").strip() for station_id in node_order_raw]
        node_count = len(node_order)
        separator_counts = {node.count("|") for node in node_order if node}
        if 2 in separator_counts:
            matrix_node_mode = "direction_line_nodes"
        elif 1 in separator_counts:
            matrix_node_mode = "line_nodes"
        else:
            matrix_node_mode = "station_nodes"
        matrix_node_ids: set[str] = set()
        for node_id in node_order:
            if not node_id:
                continue
            if "|" in node_id:
                station_id, line_id, direction = _split_node_key(node_id)
                if station_id in allowed_station_ids and line_id:
                    if matrix_node_mode != "direction_line_nodes" or direction:
                        matrix_node_ids.add(node_id)
            elif node_id in allowed_station_ids:
                matrix_node_ids.add(node_id)

        if not matrix_node_ids:
            return {}, set(), "no_overlap", matrix_node_mode

        neighbors: dict[str, set[str]] = {node_id: set() for node_id in matrix_node_ids}

        directed_arc_count = 0
        for row_idx, from_node_id in enumerate(node_order):
            if from_node_id not in matrix_node_ids:
                continue
            row = matrix_rows[row_idx]
            if not isinstance(row, str) or len(row) != node_count:
                return {}, set(), "invalid_row_encoding", matrix_node_mode
            from_neighbors = neighbors[from_node_id]
            for col_idx, raw_value in enumerate(row):
                if raw_value != "1":
                    continue
                to_node_id = node_order[col_idx]
                if to_node_id not in matrix_node_ids or to_node_id == from_node_id:
                    continue
                if to_node_id not in from_neighbors:
                    directed_arc_count += 1
                from_neighbors.add(to_node_id)

        return (
            neighbors,
            matrix_node_ids,
            f"loaded nodes={len(matrix_node_ids)} arcs={directed_arc_count}",
            matrix_node_mode,
        )

    @staticmethod
    def _synthesize_matrix_edge(
        from_node: str,
        to_node: str,
        station_meta: dict[str, dict[str, Any]],
        transfer_minutes: dict[tuple[str, str], float],
        ride_minutes: dict[tuple[str, str, str, str], float],
    ) -> dict[str, Any] | None:
        from_station, from_line, from_direction = _split_node_key(from_node)
        to_station, to_line, to_direction = _split_node_key(to_node)
        if not from_station or not from_line or not to_station or not to_line:
            return None
        if from_node == to_node:
            return None

        if from_station == to_station and from_line == to_line and from_direction != to_direction:
            return {
                "to": to_node,
                "kind": "direction_switch",
                "minutes": 0.0,
                "from_station": from_station,
                "to_station": to_station,
                "from_line": from_line,
                "to_line": to_line,
                "from_direction": from_direction,
                "to_direction": to_direction,
                "direction": to_direction,
            }

        if from_station == to_station and from_line != to_line:
            return {
                "to": to_node,
                "kind": "line_switch",
                "minutes": 0.0,
                "from_station": from_station,
                "to_station": to_station,
                "from_line": from_line,
                "to_line": to_line,
                "from_direction": from_direction,
                "to_direction": to_direction,
                "direction": to_direction,
            }

        if from_line == to_line and from_station != to_station and from_direction == to_direction:
            minutes = ride_minutes.get((from_station, to_station, from_line, from_direction))
            if minutes is None:
                reverse_direction = _opposite_direction(from_direction)
                if reverse_direction:
                    minutes = ride_minutes.get((to_station, from_station, from_line, reverse_direction))
            if minutes is None:
                from_meta = station_meta.get(from_station) or {}
                to_meta = station_meta.get(to_station) or {}
                from_coords = (
                    float(from_meta.get("latitude", 0.0)),
                    float(from_meta.get("longitude", 0.0)),
                )
                to_coords = (
                    float(to_meta.get("latitude", 0.0)),
                    float(to_meta.get("longitude", 0.0)),
                )
                minutes = _estimate_neighbor_ride_minutes(from_coords, to_coords)
            return {
                "to": to_node,
                "kind": "ride",
                "minutes": float(minutes),
                "line": from_line,
                "direction": from_direction,
                "from_direction": from_direction,
                "to_direction": to_direction,
                "from_station": from_station,
                "to_station": to_station,
            }

        if from_station != to_station:
            walk_minutes = transfer_minutes.get((from_station, to_station))
            if walk_minutes is None:
                from_meta = station_meta.get(from_station) or {}
                to_meta = station_meta.get(to_station) or {}
                distance_miles = _haversine_miles(
                    (
                        float(from_meta.get("latitude", 0.0)),
                        float(from_meta.get("longitude", 0.0)),
                    ),
                    (
                        float(to_meta.get("latitude", 0.0)),
                        float(to_meta.get("longitude", 0.0)),
                    ),
                )
                walk_minutes = max(5.0, min((distance_miles / 2.5) * 60.0, 10.0))
            return {
                "to": to_node,
                "kind": "station_transfer",
                "minutes": float(walk_minutes),
                "from_station": from_station,
                "to_station": to_station,
                "from_line": from_line,
                "to_line": to_line,
                "from_direction": from_direction,
                "to_direction": to_direction,
                "direction": to_direction,
            }

        return None

    def _apply_directional_matrix_topology(
        self,
        base_adjacency: dict[str, list[dict[str, Any]]],
        matrix_neighbors: dict[str, set[str]],
        station_meta: dict[str, dict[str, Any]],
        transfer_minutes: dict[tuple[str, str], float],
        ride_minutes: dict[tuple[str, str, str, str], float],
    ) -> tuple[dict[str, list[dict[str, Any]]], str]:
        matrix_direction_nodes = {
            node_id
            for node_id in matrix_neighbors.keys()
            if str(node_id or "").count("|") == 2
        }
        if not matrix_direction_nodes:
            return base_adjacency, "no_direction_nodes"

        output: dict[str, list[dict[str, Any]]] = {}
        synthesized_count = 0
        mapped_count = 0
        covered_base_nodes = 0

        for from_node, base_edges in base_adjacency.items():
            if from_node not in matrix_direction_nodes:
                output[from_node] = list(base_edges)
                continue

            covered_base_nodes += 1
            base_by_to = {
                str(edge.get("to") or ""): edge
                for edge in base_edges
                if str(edge.get("to") or "")
            }
            mapped_edges: list[dict[str, Any]] = []
            desired_neighbors = sorted(
                str(to_node or "")
                for to_node in matrix_neighbors.get(from_node, set())
                if str(to_node or "").count("|") == 2
            )
            for to_node in desired_neighbors:
                edge = base_by_to.get(to_node)
                if edge is not None:
                    mapped_edges.append(edge)
                    mapped_count += 1
                    continue
                synthesized = self._synthesize_matrix_edge(
                    from_node=from_node,
                    to_node=to_node,
                    station_meta=station_meta,
                    transfer_minutes=transfer_minutes,
                    ride_minutes=ride_minutes,
                )
                if synthesized is not None:
                    mapped_edges.append(synthesized)
                    synthesized_count += 1

            output[from_node] = mapped_edges

        for from_node in sorted(matrix_direction_nodes):
            if from_node in output:
                continue
            mapped_edges: list[dict[str, Any]] = []
            desired_neighbors = sorted(
                str(to_node or "")
                for to_node in matrix_neighbors.get(from_node, set())
                if str(to_node or "").count("|") == 2
            )
            for to_node in desired_neighbors:
                synthesized = self._synthesize_matrix_edge(
                    from_node=from_node,
                    to_node=to_node,
                    station_meta=station_meta,
                    transfer_minutes=transfer_minutes,
                    ride_minutes=ride_minutes,
                )
                if synthesized is not None:
                    mapped_edges.append(synthesized)
                    synthesized_count += 1
            output[from_node] = mapped_edges

        coverage = float(covered_base_nodes) / float(max(1, len(base_adjacency)))
        summary = (
            f"direction_matrix_applied coverage={coverage:.2f} "
            f"mapped={mapped_count} synthesized={synthesized_count}"
        )
        return output, summary

    def _load_graph(self) -> dict[str, Any]:
        loaded_at = int(time.time())
        try:
            station_graph = self._load_station_graph()
            gtfs_dir = self._pick_gtfs_dir()
            if not gtfs_dir:
                return {
                    "loaded_at": loaded_at,
                    "available": False,
                    "error": "No GTFS directory found with stops.txt, trips.txt, and stop_times.txt.",
                    "source": {
                        "station_graph_path": self.station_graph_path,
                        "non_weighted_matrix_path": self.non_weighted_matrix_path,
                        "topology_source": "",
                        "matrix_status": "not_loaded",
                        "matrix_node_mode": "none",
                        "gtfs_dir": "",
                    },
                    "stations": {},
                    "stations_by_name": {},
                    "station_lines": {},
                    "station_line_directions": {},
                    "station_neighbors": {},
                    "station_direction_neighbors": {},
                    "stop_to_station": {},
                    "adjacency": {},
                    "node_count": 0,
                    "edge_count": 0,
                }

            station_ids = set(station_graph.keys())
            station_meta = {
                station_id: {
                    "station_id": station_id,
                    "name": str((station_graph.get(station_id) or {}).get("name") or station_id),
                    "borough": str((station_graph.get(station_id) or {}).get("borough") or ""),
                    "latitude": _to_float((station_graph.get(station_id) or {}).get("latitude"), 0.0),
                    "longitude": _to_float((station_graph.get(station_id) or {}).get("longitude"), 0.0),
                }
                for station_id in station_ids
            }

            stations_by_name: dict[str, list[str]] = {}
            for station_id, meta in station_meta.items():
                normalized_name = str(meta["name"]).strip().lower()
                if not normalized_name:
                    continue
                stations_by_name.setdefault(normalized_name, []).append(station_id)

            station_neighbors, station_direction_neighbors = self._build_station_topology_from_station_graph(
                station_graph=station_graph,
                station_ids=station_ids,
            )
            (
                matrix_neighbors,
                matrix_node_ids,
                matrix_status,
                matrix_node_mode,
            ) = self._load_non_weighted_matrix_neighbors(
                allowed_station_ids=station_ids,
            )
            matrix_station_ids = {
                node_id
                for node_id in matrix_node_ids
                if "|" not in str(node_id or "")
            }
            matrix_direction_node_ids = {
                node_id
                for node_id in matrix_node_ids
                if str(node_id or "").count("|") == 2
            }
            matrix_line_node_ids = {
                node_id
                for node_id in matrix_node_ids
                if str(node_id or "").count("|") == 1
            }
            if matrix_station_ids:
                for station_id in matrix_station_ids:
                    station_neighbors[station_id] = set()
                for from_station in matrix_station_ids:
                    for to_station in matrix_neighbors.get(from_station, set()):
                        if to_station not in matrix_station_ids or to_station == from_station:
                            continue
                        station_neighbors[from_station].add(to_station)
                        station_neighbors[to_station].add(from_station)

            if matrix_direction_node_ids:
                topology_source = "direction_line_matrix_pending"
            elif matrix_line_node_ids:
                topology_source = "legacy_line_node_matrix_ignored"
            elif matrix_station_ids and len(matrix_station_ids) < len(station_ids):
                topology_source = "station_matrix+station_graph_fallback"
            elif matrix_station_ids:
                topology_source = "station_matrix"
            else:
                topology_source = "station_graph"

            stop_to_station: dict[str, str] = {station_id: station_id for station_id in station_ids}
            stops_path = os.path.join(gtfs_dir, "stops.txt")
            with open(stops_path, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    stop_id = str(row.get("stop_id", "") or "").strip()
                    if not stop_id:
                        continue
                    parent_station = str(row.get("parent_station", "") or "").strip()

                    if parent_station and parent_station in station_ids:
                        canonical = parent_station
                    elif stop_id in station_ids:
                        canonical = stop_id
                    elif stop_id[-1:] in {"N", "S"} and stop_id[:-1] in station_ids:
                        canonical = stop_id[:-1]
                    else:
                        continue

                    stop_to_station[stop_id] = canonical
                    stop_to_station[canonical] = canonical

            route_lookup: dict[str, str] = {}
            routes_path = os.path.join(gtfs_dir, "routes.txt")
            if os.path.exists(routes_path):
                with open(routes_path, newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        route_id = str(row.get("route_id", "") or "").strip()
                        line = _normalize_line(row.get("route_short_name", "") or route_id)
                        if route_id and line:
                            route_lookup[route_id] = line

            trip_to_line: dict[str, str] = {}
            trip_direction_hint: dict[str, str] = {}
            trips_path = os.path.join(gtfs_dir, "trips.txt")
            with open(trips_path, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    trip_id = str(row.get("trip_id", "") or "").strip()
                    route_id = str(row.get("route_id", "") or "").strip()
                    line = route_lookup.get(route_id) or _normalize_line(route_id)
                    if trip_id and line:
                        trip_to_line[trip_id] = line
                        direction_id = _normalize_direction(row.get("direction_id", "") or "")
                        if direction_id:
                            trip_direction_hint[trip_id] = direction_id

            station_line_directions: dict[str, dict[str, set[str]]] = {
                station_id: {}
                for station_id in station_ids
            }
            travel_stats: dict[tuple[str, str, str, str], tuple[int, float]] = {}
            prev_by_trip: dict[str, tuple[int, str, int, str]] = {}
            ride_direction_relation_cache: dict[tuple[str, str, str], str] = {}

            stop_times_path = os.path.join(gtfs_dir, "stop_times.txt")
            with open(stop_times_path, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    trip_id = str(row.get("trip_id", "") or "").strip()
                    line = trip_to_line.get(trip_id)
                    if not line:
                        continue

                    stop_id = str(row.get("stop_id", "") or "").strip()
                    station_id = stop_to_station.get(stop_id)
                    if not station_id and stop_id[-1:] in {"N", "S"}:
                        station_id = stop_to_station.get(stop_id[:-1])
                    if not station_id or station_id not in station_ids:
                        continue

                    stop_direction = _direction_from_stop_id(stop_id)
                    station_line_directions.setdefault(station_id, {}).setdefault(line, set())
                    if stop_direction:
                        station_line_directions[station_id][line].add(stop_direction)

                    stop_sequence_raw = str(row.get("stop_sequence", "") or "").strip()
                    departure_raw = row.get("departure_time", "") or row.get("arrival_time", "")
                    timestamp = _parse_gtfs_time_to_seconds(departure_raw)
                    if not stop_sequence_raw or timestamp is None:
                        continue
                    try:
                        stop_sequence = int(stop_sequence_raw)
                    except ValueError:
                        continue

                    previous = prev_by_trip.get(trip_id)
                    if previous and stop_sequence > previous[0]:
                        prev_station = previous[1]
                        prev_time = previous[2]
                        prev_direction = previous[3]
                        edge_direction = self._resolve_ride_direction(
                            from_station=prev_station,
                            to_station=station_id,
                            direction_hint=stop_direction or prev_direction or trip_direction_hint.get(trip_id, ""),
                            station_direction_neighbors=station_direction_neighbors,
                            relation_cache=ride_direction_relation_cache,
                        )
                        if prev_station != station_id and edge_direction:
                            station_line_directions.setdefault(prev_station, {}).setdefault(line, set()).add(edge_direction)
                            station_line_directions.setdefault(station_id, {}).setdefault(line, set()).add(edge_direction)
                            delta = timestamp - prev_time
                            if delta <= 0:
                                delta += 86400
                            if 0 < delta <= 7200:
                                minutes = max(0.25, min(float(delta) / 60.0, 120.0))
                                edge_key = (prev_station, station_id, line, edge_direction)
                                count, total = travel_stats.get(edge_key, (0, 0.0))
                                travel_stats[edge_key] = (count + 1, total + minutes)

                    prev_by_trip[trip_id] = (stop_sequence, station_id, timestamp, stop_direction)

            station_lines: dict[str, set[str]] = {
                station_id: set(line_map.keys())
                for station_id, line_map in station_line_directions.items()
            }
            for from_station, direction_neighbors in station_direction_neighbors.items():
                from_lines = station_lines.get(from_station) or set()
                if not from_lines:
                    continue
                for direction, neighbors in direction_neighbors.items():
                    for to_station in neighbors:
                        to_lines = station_lines.get(to_station) or set()
                        shared_lines = from_lines & to_lines
                        for line in shared_lines:
                            station_line_directions.setdefault(from_station, {}).setdefault(line, set()).add(direction)
                            station_line_directions.setdefault(to_station, {}).setdefault(line, set()).add(direction)

            transfer_minutes: dict[tuple[str, str], float] = {}
            transfers_path = os.path.join(gtfs_dir, "transfers.txt")
            if os.path.exists(transfers_path):
                with open(transfers_path, newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    for row in reader:
                        from_stop = str(row.get("from_stop_id", "") or "").strip()
                        to_stop = str(row.get("to_stop_id", "") or "").strip()
                        from_station = stop_to_station.get(from_stop) or stop_to_station.get(from_stop[:-1])
                        to_station = stop_to_station.get(to_stop) or stop_to_station.get(to_stop[:-1])
                        if (
                            not from_station
                            or not to_station
                            or from_station not in station_ids
                            or to_station not in station_ids
                            or from_station == to_station
                        ):
                            continue

                        raw_transfer_seconds = row.get("min_transfer_time", "")
                        transfer_seconds = _to_float(raw_transfer_seconds, 0.0)
                        walk_minutes = max(5.0, min(transfer_seconds / 60.0, 30.0))

                        key_ab = (from_station, to_station)
                        key_ba = (to_station, from_station)
                        current_ab = transfer_minutes.get(key_ab)
                        current_ba = transfer_minutes.get(key_ba)
                        transfer_minutes[key_ab] = (
                            walk_minutes if current_ab is None else min(current_ab, walk_minutes)
                        )
                        transfer_minutes[key_ba] = (
                            walk_minutes if current_ba is None else min(current_ba, walk_minutes)
                        )

            # Fallback transfer links for station complexes that share names.
            for station_group in stations_by_name.values():
                if len(station_group) < 2:
                    continue
                for index, from_station in enumerate(station_group):
                    for to_station in station_group[index + 1 :]:
                        from_meta = station_meta.get(from_station) or {}
                        to_meta = station_meta.get(to_station) or {}
                        distance_miles = _haversine_miles(
                            (float(from_meta.get("latitude", 0.0)), float(from_meta.get("longitude", 0.0))),
                            (float(to_meta.get("latitude", 0.0)), float(to_meta.get("longitude", 0.0))),
                        )
                        if distance_miles > 0.35:
                            continue
                        walk_minutes = max(5.0, min((distance_miles / 2.5) * 60.0, 10.0))
                        key_ab = (from_station, to_station)
                        key_ba = (to_station, from_station)
                        current_ab = transfer_minutes.get(key_ab)
                        current_ba = transfer_minutes.get(key_ba)
                        transfer_minutes[key_ab] = (
                            walk_minutes if current_ab is None else min(current_ab, walk_minutes)
                        )
                        transfer_minutes[key_ba] = (
                            walk_minutes if current_ba is None else min(current_ba, walk_minutes)
                        )

            ride_minutes: dict[tuple[str, str, str, str], float] = {}
            for edge_key, stats in travel_stats.items():
                count, total = stats
                if count <= 0:
                    continue
                ride_minutes[edge_key] = max(0.25, min(total / float(count), 120.0))

            # Fill any missing neighboring station links from the station graph itself.
            for from_station, direction_neighbors in station_direction_neighbors.items():
                from_meta = station_meta.get(from_station) or {}
                from_coords = (
                    float(from_meta.get("latitude", 0.0)),
                    float(from_meta.get("longitude", 0.0)),
                )
                for direction, neighbors in direction_neighbors.items():
                    from_line_map = station_line_directions.get(from_station) or {}
                    for to_station in neighbors:
                        to_line_map = station_line_directions.get(to_station) or {}
                        shared_lines = sorted(set(from_line_map.keys()) & set(to_line_map.keys()))
                        if not shared_lines:
                            continue
                        to_meta = station_meta.get(to_station) or {}
                        to_coords = (
                            float(to_meta.get("latitude", 0.0)),
                            float(to_meta.get("longitude", 0.0)),
                        )
                        for line in shared_lines:
                            if (
                                direction not in (from_line_map.get(line) or set())
                                or direction not in (to_line_map.get(line) or set())
                            ):
                                continue
                            forward_key = (from_station, to_station, line, direction)
                            if forward_key in ride_minutes:
                                continue
                            reverse_direction = _opposite_direction(direction)
                            reverse_key = (to_station, from_station, line, reverse_direction)
                            if reverse_direction and reverse_key in ride_minutes:
                                ride_minutes[forward_key] = ride_minutes[reverse_key]
                            else:
                                ride_minutes[forward_key] = _estimate_neighbor_ride_minutes(from_coords, to_coords)

            adjacency: dict[str, list[dict[str, Any]]] = {}
            for station_id, line_map in station_line_directions.items():
                for line, directions in line_map.items():
                    for direction in sorted(directions):
                        adjacency.setdefault(_node_key(station_id, line, direction), [])

            for (from_station, to_station, line, direction), minutes in ride_minutes.items():
                from_node = _node_key(from_station, line, direction)
                to_node = _node_key(to_station, line, direction)
                if from_node not in adjacency or to_node not in adjacency:
                    continue
                adjacency[from_node].append(
                    {
                        "to": to_node,
                        "kind": "ride",
                        "minutes": float(minutes),
                        "line": line,
                        "direction": direction,
                        "from_direction": direction,
                        "to_direction": direction,
                        "from_station": from_station,
                        "to_station": to_station,
                    }
                )

            for station_id, line_map in station_line_directions.items():
                for line, directions in line_map.items():
                    sorted_directions = sorted(directions)
                    if len(sorted_directions) < 2:
                        continue
                    for from_direction in sorted_directions:
                        from_node = _node_key(station_id, line, from_direction)
                        if from_node not in adjacency:
                            continue
                        for to_direction in sorted_directions:
                            if from_direction == to_direction:
                                continue
                            to_node = _node_key(station_id, line, to_direction)
                            if to_node not in adjacency:
                                continue
                            adjacency[from_node].append(
                                {
                                    "to": to_node,
                                    "kind": "direction_switch",
                                    "minutes": 0.0,
                                    "from_station": station_id,
                                    "to_station": station_id,
                                    "from_line": line,
                                    "to_line": line,
                                    "from_direction": from_direction,
                                    "to_direction": to_direction,
                                    "direction": to_direction,
                                }
                            )

            for station_id, line_map in station_line_directions.items():
                sorted_lines = sorted(line_map.keys())
                if len(sorted_lines) < 2:
                    continue
                for from_line in sorted_lines:
                    for from_direction in sorted(line_map.get(from_line) or []):
                        from_node = _node_key(station_id, from_line, from_direction)
                        if from_node not in adjacency:
                            continue
                        for to_line in sorted_lines:
                            if from_line == to_line:
                                continue
                            for to_direction in sorted(line_map.get(to_line) or []):
                                to_node = _node_key(station_id, to_line, to_direction)
                                if to_node not in adjacency:
                                    continue
                                adjacency[from_node].append(
                                    {
                                        "to": to_node,
                                        "kind": "line_switch",
                                        "minutes": 0.0,
                                        "from_station": station_id,
                                        "to_station": station_id,
                                        "from_line": from_line,
                                        "to_line": to_line,
                                        "from_direction": from_direction,
                                        "to_direction": to_direction,
                                        "direction": to_direction,
                                    }
                                )

            for (from_station, to_station), walk_minutes in transfer_minutes.items():
                from_line_map = station_line_directions.get(from_station) or {}
                to_line_map = station_line_directions.get(to_station) or {}
                if not from_line_map or not to_line_map:
                    continue
                for from_line, from_directions in from_line_map.items():
                    for from_direction in sorted(from_directions):
                        from_node = _node_key(from_station, from_line, from_direction)
                        if from_node not in adjacency:
                            continue
                        for to_line, to_directions in to_line_map.items():
                            for to_direction in sorted(to_directions):
                                to_node = _node_key(to_station, to_line, to_direction)
                                if to_node not in adjacency:
                                    continue
                                adjacency[from_node].append(
                                    {
                                        "to": to_node,
                                        "kind": "station_transfer",
                                        "minutes": float(walk_minutes),
                                        "from_station": from_station,
                                        "to_station": to_station,
                                        "from_line": from_line,
                                        "to_line": to_line,
                                        "from_direction": from_direction,
                                        "to_direction": to_direction,
                                        "direction": to_direction,
                                    }
                                )

            if matrix_direction_node_ids:
                adjacency, matrix_apply_status = self._apply_directional_matrix_topology(
                    base_adjacency=adjacency,
                    matrix_neighbors=matrix_neighbors,
                    station_meta=station_meta,
                    transfer_minutes=transfer_minutes,
                    ride_minutes=ride_minutes,
                )
                if len(matrix_direction_node_ids) < len(adjacency):
                    topology_source = "direction_line_matrix+adjacency_fallback"
                else:
                    topology_source = "direction_line_matrix"
                matrix_status = f"{matrix_status}; {matrix_apply_status}"
            elif matrix_line_node_ids:
                matrix_status = f"{matrix_status}; legacy_line_nodes_ignored"

            node_count = len(adjacency)
            edge_count = sum(len(edges) for edges in adjacency.values())

            return {
                "loaded_at": loaded_at,
                "available": True,
                "error": "",
                "source": {
                    "station_graph_path": self.station_graph_path,
                    "non_weighted_matrix_path": self.non_weighted_matrix_path,
                    "topology_source": topology_source,
                    "matrix_status": matrix_status,
                    "matrix_node_mode": matrix_node_mode,
                    "gtfs_dir": gtfs_dir,
                },
                "stations": station_meta,
                "stations_by_name": stations_by_name,
                "station_lines": {
                    station_id: sorted(line_map.keys())
                    for station_id, line_map in station_line_directions.items()
                    if line_map
                },
                "station_line_directions": {
                    station_id: {
                        line: sorted(directions)
                        for line, directions in line_map.items()
                        if directions
                    }
                    for station_id, line_map in station_line_directions.items()
                    if line_map
                },
                "station_neighbors": {
                    station_id: sorted(neighbors)
                    for station_id, neighbors in station_neighbors.items()
                    if neighbors
                },
                "station_direction_neighbors": {
                    station_id: {
                        direction: sorted(neighbors)
                        for direction, neighbors in direction_map.items()
                        if neighbors
                    }
                    for station_id, direction_map in station_direction_neighbors.items()
                    if any(direction_map.values())
                },
                "stop_to_station": {
                    str(stop_id): str(station_id)
                    for stop_id, station_id in stop_to_station.items()
                    if str(stop_id).strip() and str(station_id).strip()
                },
                "adjacency": adjacency,
                "node_count": node_count,
                "edge_count": edge_count,
            }
        except Exception as exc:
            return {
                "loaded_at": loaded_at,
                "available": False,
                "error": f"Route planner graph build failed: {exc}",
                "source": {
                    "station_graph_path": self.station_graph_path,
                    "non_weighted_matrix_path": self.non_weighted_matrix_path,
                    "topology_source": "",
                    "matrix_status": "error",
                    "matrix_node_mode": "none",
                    "gtfs_dir": "",
                },
                "stations": {},
                "stations_by_name": {},
                "station_lines": {},
                "station_line_directions": {},
                "station_neighbors": {},
                "station_direction_neighbors": {},
                "stop_to_station": {},
                "adjacency": {},
                "node_count": 0,
                "edge_count": 0,
            }

    def _ensure_loaded(self, force_refresh: bool = False) -> dict[str, Any]:
        now = int(time.time())
        with self._lock:
            loaded_at = int(self._cache.get("loaded_at") or 0)
            cache_age = now - loaded_at
            if not force_refresh and loaded_at > 0 and cache_age <= self.cache_seconds:
                return dict(self._cache)

        snapshot = self._load_graph()
        with self._lock:
            self._cache = snapshot
            return dict(self._cache)

    def _runtime_snapshot(self) -> dict[str, Any]:
        with self._runtime_lock:
            snapshot = self._runtime_cache or {}
            return {
                "version": int(snapshot.get("version") or 0),
                "built_at": float(snapshot.get("built_at") or 0.0),
                "source_timestamp": snapshot.get("source_timestamp"),
                "arrival_index": snapshot.get("arrival_index") or {},
                "ride_index": snapshot.get("ride_index") or {},
                "error": str(snapshot.get("error") or ""),
                "arrival_refreshed_at": float(snapshot.get("arrival_refreshed_at") or 0.0),
                "ride_refreshed_at": float(snapshot.get("ride_refreshed_at") or 0.0),
            }

    def refresh_runtime_indices(
        self,
        realtime_snapshot: dict[str, Any] | None,
        refresh_arrivals: bool = True,
        refresh_rides: bool = True,
    ) -> dict[str, Any]:
        graph_cache = self._ensure_loaded(force_refresh=False)
        stop_to_station = graph_cache.get("stop_to_station") or {}
        if not isinstance(stop_to_station, dict) or not stop_to_station:
            return self._runtime_snapshot()

        snapshot = realtime_snapshot if isinstance(realtime_snapshot, dict) else {}
        trip_updates = snapshot.get("trip_updates") or []
        if not isinstance(trip_updates, list):
            trip_updates = []

        now = float(time.time())
        route_start_epoch = now
        next_arrival_index: dict[tuple[str, str, str], list[float]] = {}
        next_ride_index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        ride_samples: dict[tuple[str, str, str, str], list[float]] = {}

        for trip_update in trip_updates:
            if not isinstance(trip_update, dict):
                continue
            line_id = _normalize_line(trip_update.get("route_id") or "")
            if not line_id:
                continue
            trip_direction = _normalize_direction(trip_update.get("direction") or "")
            updates = trip_update.get("stop_time_updates") or []
            if not isinstance(updates, list) or not updates:
                continue

            normalized_updates: list[dict[str, Any]] = []
            for stop_update in updates:
                if not isinstance(stop_update, dict):
                    continue
                raw_stop_id = str(stop_update.get("stop_id") or "").strip().upper()
                if not raw_stop_id:
                    continue
                station_id = (
                    stop_to_station.get(raw_stop_id)
                    or stop_to_station.get(_base_stop_id(raw_stop_id))
                    or _base_stop_id(raw_stop_id)
                )
                if not station_id:
                    continue
                event_epoch = _to_float(stop_update.get("arrival_time") or stop_update.get("departure_time"), -1.0)
                if event_epoch <= 0.0:
                    continue
                direction = _normalize_direction(trip_direction or _direction_from_stop_id(raw_stop_id))
                normalized_updates.append(
                    {
                        "stop_id": raw_stop_id,
                        "station_id": str(station_id).upper(),
                        "event_epoch": float(event_epoch),
                        "direction": direction,
                        "arrival_epoch": _to_float(stop_update.get("arrival_time"), -1.0),
                        "departure_epoch": _to_float(stop_update.get("departure_time"), -1.0),
                    }
                )

                arrival_key = (str(station_id).upper(), line_id, direction)
                next_arrival_index.setdefault(arrival_key, []).append(float(event_epoch))

            for index in range(len(normalized_updates) - 1):
                from_update = normalized_updates[index]
                to_update = normalized_updates[index + 1]
                if from_update.get("station_id") == to_update.get("station_id"):
                    continue
                direction = _normalize_direction(
                    from_update.get("direction")
                    or to_update.get("direction")
                    or trip_direction
                    or _direction_from_stop_id(str(from_update.get("stop_id") or ""))
                )
                if not direction:
                    continue

                from_epoch = _to_float(
                    from_update.get("departure_epoch")
                    if _to_float(from_update.get("departure_epoch"), -1.0) > 0.0
                    else from_update.get("arrival_epoch"),
                    -1.0,
                )
                to_epoch = _to_float(
                    to_update.get("arrival_epoch")
                    if _to_float(to_update.get("arrival_epoch"), -1.0) > 0.0
                    else to_update.get("departure_epoch"),
                    -1.0,
                )
                if from_epoch <= 0.0 or to_epoch <= from_epoch:
                    continue
                minutes = (float(to_epoch) - float(from_epoch)) / 60.0
                if minutes < 0.05 or minutes > 120.0:
                    continue

                ride_key = (
                    str(from_update.get("station_id") or "").upper(),
                    str(to_update.get("station_id") or "").upper(),
                    line_id,
                    direction,
                )
                ride_samples.setdefault(ride_key, []).append(float(minutes))

        for arrival_key, values in next_arrival_index.items():
            deduped_sorted = sorted(
                {
                    round(float(value), 3)
                    for value in values
                    if float(value) >= route_start_epoch - 120.0
                }
            )
            next_arrival_index[arrival_key] = deduped_sorted

        for ride_key, values in ride_samples.items():
            if not values:
                continue
            sorted_values = sorted(float(value) for value in values if value > 0.0)
            if not sorted_values:
                continue
            median_index = len(sorted_values) // 2
            if len(sorted_values) % 2 == 1:
                median_minutes = sorted_values[median_index]
            else:
                median_minutes = (sorted_values[median_index - 1] + sorted_values[median_index]) / 2.0
            next_ride_index[ride_key] = {
                "median_minutes": float(median_minutes),
                "sample_count": int(len(sorted_values)),
            }

        with self._runtime_lock:
            current = self._runtime_cache or {}
            current_version = int(current.get("version") or 0)
            arrival_index = current.get("arrival_index") or {}
            ride_index = current.get("ride_index") or {}
            arrival_refreshed_at = float(current.get("arrival_refreshed_at") or 0.0)
            ride_refreshed_at = float(current.get("ride_refreshed_at") or 0.0)

            if refresh_arrivals:
                arrival_index = next_arrival_index
                arrival_refreshed_at = now
            if refresh_rides:
                ride_index = next_ride_index
                ride_refreshed_at = now

            self._runtime_cache = {
                "version": current_version + 1,
                "built_at": now,
                "source_timestamp": snapshot.get("source_timestamp"),
                "arrival_index": arrival_index,
                "ride_index": ride_index,
                "error": str(snapshot.get("error") or ""),
                "arrival_refreshed_at": arrival_refreshed_at,
                "ride_refreshed_at": ride_refreshed_at,
            }
            return {
                "version": int(current_version + 1),
                "built_at": float(now),
                "source_timestamp": snapshot.get("source_timestamp"),
                "arrival_index": arrival_index,
                "ride_index": ride_index,
                "error": str(snapshot.get("error") or ""),
                "arrival_refreshed_at": float(arrival_refreshed_at),
                "ride_refreshed_at": float(ride_refreshed_at),
            }

    def runtime_status(self) -> dict[str, Any]:
        snapshot = self._runtime_snapshot()
        return {
            "version": int(snapshot.get("version") or 0),
            "built_at": float(snapshot.get("built_at") or 0.0),
            "source_timestamp": snapshot.get("source_timestamp"),
            "error": str(snapshot.get("error") or ""),
            "arrival_refreshed_at": float(snapshot.get("arrival_refreshed_at") or 0.0),
            "ride_refreshed_at": float(snapshot.get("ride_refreshed_at") or 0.0),
            "arrival_keys": len(snapshot.get("arrival_index") or {}),
            "ride_keys": len(snapshot.get("ride_index") or {}),
        }

    @staticmethod
    def _normalize_station_name(value: str) -> str:
        return str(value or "").strip().lower()

    def _resolve_station_id(self, cache: dict[str, Any], raw_station: str) -> str:
        value = str(raw_station or "").strip()
        if not value:
            return ""
        upper_value = value.upper()
        stations = cache.get("stations") or {}
        if upper_value in stations:
            return upper_value
        by_name = cache.get("stations_by_name") or {}
        direct_name_matches = by_name.get(self._normalize_station_name(value), [])
        if direct_name_matches:
            return sorted(direct_name_matches)[0]
        normalized_query = self._normalize_station_name(value)
        contains_matches = sorted(
            station_id
            for station_id, station in stations.items()
            if normalized_query and normalized_query in self._normalize_station_name((station or {}).get("name", ""))
        )
        return contains_matches[0] if contains_matches else ""

    def _resolve_station_complex_ids(self, cache: dict[str, Any], station_id: str) -> list[str]:
        stations = cache.get("stations") or {}
        anchor = stations.get(station_id) or {}
        if not anchor:
            return [station_id] if station_id else []

        anchor_name = self._normalize_station_name(anchor.get("name", ""))
        if not anchor_name:
            return [station_id]

        anchor_coords = (
            float(anchor.get("latitude", 0.0)),
            float(anchor.get("longitude", 0.0)),
        )
        same_name_distance_miles = 0.08
        candidates = []
        for candidate_id in cache.get("stations_by_name", {}).get(anchor_name, []):
            candidate = stations.get(candidate_id) or {}
            candidate_coords = (
                float(candidate.get("latitude", 0.0)),
                float(candidate.get("longitude", 0.0)),
            )
            if _haversine_miles(anchor_coords, candidate_coords) <= same_name_distance_miles:
                candidates.append(candidate_id)

        if station_id not in candidates:
            candidates.append(station_id)
        return sorted(set(candidates))

    def _predict_ride_edge_minutes(
        self,
        edge: dict[str, Any],
        current_cost_minutes: float,
        weights: dict[str, Any],
    ) -> float:
        base_minutes = max(0.0, float(edge.get("minutes", 0.0)))
        predicted_minutes: float | None = None

        route_start_epoch = _to_float(weights.get("model_time_epoch"), float(time.time()))
        query_epoch_seconds = route_start_epoch + (float(current_cost_minutes) * 60.0)
        try:
            query_dt_local = dt.datetime.fromtimestamp(query_epoch_seconds)
        except Exception:
            query_dt_local = dt.datetime.fromtimestamp(float(time.time()))
            query_epoch_seconds = float(query_dt_local.timestamp())

        inferred_day = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][query_dt_local.weekday()]
        day_of_week_local = self._normalize_day_of_week(weights.get("model_day_of_week")) or inferred_day
        minute_of_day_local = int(query_dt_local.hour * 60 + query_dt_local.minute)
        recent_rides = self._normalize_recent_rides(weights.get("model_recent_rides"))
        from_station_id = str(edge.get("from_station") or "")
        to_station_id = str(edge.get("to_station") or "")
        line_id = str(edge.get("line") or edge.get("from_line") or edge.get("to_line") or "")
        direction = _normalize_direction(
            edge.get("direction") or edge.get("to_direction") or edge.get("from_direction") or ""
        )

        model_context = {
            "weights": weights,
            "edge_kind": "ride",
            "query_epoch_seconds": float(query_epoch_seconds),
            "minute_of_day_local": minute_of_day_local,
            "day_of_week_local": day_of_week_local,
            "recent_rides_count": len(recent_rides),
            "direction": direction,
        }

        runtime = self._runtime_snapshot()
        ride_index = runtime.get("ride_index") or {}
        runtime_ride_key = (
            str(from_station_id or "").upper(),
            str(to_station_id or "").upper(),
            _normalize_line(line_id),
            _normalize_direction(direction),
        )
        runtime_ride_entry = ride_index.get(runtime_ride_key)
        if isinstance(runtime_ride_entry, dict):
            runtime_median = _to_float(runtime_ride_entry.get("median_minutes"), -1.0)
            runtime_count = max(0.0, _to_float(runtime_ride_entry.get("sample_count"), 0.0))
            if runtime_median > 0.0 and runtime_count > 0.0:
                if runtime_count >= 4:
                    realtime_weight = 0.75
                elif runtime_count >= 2:
                    realtime_weight = 0.6
                else:
                    realtime_weight = 0.45
                blended_minutes = (base_minutes * (1.0 - realtime_weight)) + (runtime_median * realtime_weight)
                return max(0.05, min(float(blended_minutes), 120.0))

        try:
            raw_prediction = self.ride_time_model.predict_ride_minutes(
                from_station_id=from_station_id,
                to_station_id=to_station_id,
                line_id=line_id,
                direction=direction,
                query_epoch_seconds=float(query_epoch_seconds),
                minute_of_day_local=minute_of_day_local,
                day_of_week_local=day_of_week_local,
                recent_rides=recent_rides,
                edge=edge,
                current_cost_minutes=float(current_cost_minutes),
                context=model_context,
            )
            if raw_prediction is not None:
                predicted_minutes = float(raw_prediction)
        except TypeError:
            # Backward compatibility: allow models that still implement the
            # previous signature (edge, current_cost_minutes, context).
            try:
                raw_prediction = self.ride_time_model.predict_ride_minutes(
                    edge=edge,
                    current_cost_minutes=float(current_cost_minutes),
                    context=model_context,
                )
                if raw_prediction is not None:
                    predicted_minutes = float(raw_prediction)
            except Exception:
                predicted_minutes = None
        except Exception:
            predicted_minutes = None

        if predicted_minutes is None:
            predicted_minutes = base_minutes
        return max(0.05, predicted_minutes)

    def _predict_arrival_candidates(
        self,
        station_id: str,
        line_id: str,
        direction: str,
        at_minute: float,
        weights: dict[str, Any],
    ) -> list[float]:
        if not bool(weights.get("dynamic_wait_enabled")):
            return []

        if not station_id or not line_id:
            return []

        runtime = self._runtime_snapshot()
        arrival_index = runtime.get("arrival_index") or {}
        arrival_key = (str(station_id).upper(), _normalize_line(line_id), _normalize_direction(direction))
        precomputed_epochs = arrival_index.get(arrival_key) or []
        if precomputed_epochs:
            route_start_epoch = _to_float(weights.get("model_time_epoch"), float(time.time()))
            query_epoch = float(route_start_epoch) + (float(at_minute) * 60.0)
            sorted_epochs = [float(value) for value in precomputed_epochs if _to_float(value, -1.0) > 0.0]
            if sorted_epochs:
                start_index = bisect_left(sorted_epochs, query_epoch)
                selected_epochs = sorted_epochs[start_index : start_index + int(weights.get("arrival_limit", 5) or 5)]
                if selected_epochs:
                    return [max(0.0, (value - float(route_start_epoch)) / 60.0) for value in selected_epochs]

        try:
            arrivals = self.arrival_model.predict_next_arrivals(
                station_id=station_id,
                line_id=line_id,
                direction=direction,
                at_minute=float(at_minute),
                limit=int(weights.get("arrival_limit", 5) or 5),
                context={
                    "weights": weights,
                    "query_minute": float(at_minute),
                },
            )
        except Exception:
            arrivals = []

        candidates: list[float] = []
        for value in arrivals or []:
            candidate = _to_float(value, -1.0)
            if candidate >= 0.0:
                candidates.append(candidate)
        candidates.sort()
        return candidates

    def _predict_wait_details(
        self,
        station_id: str,
        line_id: str,
        direction: str,
        at_minute: float,
        weights: dict[str, Any],
    ) -> dict[str, Any]:
        default_wait = max(0.0, _to_float(weights.get("default_wait_minutes"), 0.0))
        candidates = self._predict_arrival_candidates(
            station_id=station_id,
            line_id=line_id,
            direction=direction,
            at_minute=at_minute,
            weights=weights,
        )
        next_arrivals = [candidate for candidate in candidates if candidate >= float(at_minute)][:4]
        if next_arrivals:
            wait_minutes = max(0.0, float(next_arrivals[0]) - float(at_minute))
            return {
                "wait_minutes": float(wait_minutes),
                "uses_live_arrivals": True,
                "next_arrival_minutes": [round(float(value), 2) for value in next_arrivals],
                "live_wait_minutes": round(float(wait_minutes), 2),
                "fallback_minutes": float(default_wait),
            }

        return {
            "wait_minutes": float(default_wait),
            "uses_live_arrivals": False,
            "next_arrival_minutes": [],
            "live_wait_minutes": None,
            "fallback_minutes": float(default_wait),
        }

    def _predict_wait_minutes(
        self,
        station_id: str,
        line_id: str,
        direction: str,
        at_minute: float,
        weights: dict[str, Any],
    ) -> float:
        return float(
            self._predict_wait_details(
                station_id=station_id,
                line_id=line_id,
                direction=direction,
                at_minute=at_minute,
                weights=weights,
            ).get("wait_minutes")
            or 0.0
        )

    def _transfer_runtime(
        self,
        edge: dict[str, Any],
        weights: dict[str, Any],
        current_cost_minutes: float,
    ) -> dict[str, Any]:
        kind = str(edge.get("kind") or "")
        station_id = str(edge.get("to_station") or edge.get("from_station") or "")
        line_id = str(edge.get("to_line") or edge.get("from_line") or "")
        direction = _normalize_direction(edge.get("to_direction") or edge.get("direction") or "")
        walk_minutes = max(0.0, float(edge.get("minutes", 0.0)))

        if kind == "direction_switch":
            penalty_minutes = max(0.0, float(weights["direction_switch_penalty"]))
            transfer_ready_minute = float(current_cost_minutes)
        elif kind == "line_switch":
            penalty_minutes = max(0.0, float(weights["line_switch_penalty"]))
            transfer_ready_minute = float(current_cost_minutes)
        else:
            penalty_minutes = max(0.0, float(weights["station_transfer_penalty"]))
            walk_minutes = max(5.0, walk_minutes * float(weights["transfer_time_multiplier"]))
            transfer_ready_minute = float(current_cost_minutes) + walk_minutes

        wait_details = self._predict_wait_details(
            station_id=station_id,
            line_id=line_id,
            direction=direction,
            at_minute=transfer_ready_minute,
            weights=weights,
        )
        wait_minutes = float(wait_details.get("wait_minutes") or 0.0)
        applied_minutes = penalty_minutes + (walk_minutes if kind == "station_transfer" else 0.0) + wait_minutes
        return {
            "applied_minutes": float(applied_minutes),
            "uses_live_arrivals": bool(wait_details.get("uses_live_arrivals")),
            "transfer_ready_minute": float(transfer_ready_minute),
            "next_arrival_minutes": wait_details.get("next_arrival_minutes") or [],
            "live_wait_minutes": wait_details.get("live_wait_minutes"),
            "fallback_minutes": float(wait_details.get("fallback_minutes") or 0.0),
            "wait_minutes": float(wait_minutes),
            "penalty_minutes": float(penalty_minutes),
            "walk_minutes": float(walk_minutes),
        }

    def _origin_boarding_runtime(
        self,
        station_id: str,
        line_id: str,
        direction: str,
        weights: dict[str, Any],
    ) -> dict[str, Any]:
        wait_details = self._predict_wait_details(
            station_id=station_id,
            line_id=line_id,
            direction=direction,
            at_minute=0.0,
            weights=weights,
        )
        return {
            "applied_minutes": float(wait_details.get("wait_minutes") or 0.0),
            "uses_live_arrivals": bool(wait_details.get("uses_live_arrivals")),
            "boarding_ready_minute": 0.0,
            "next_arrival_minutes": wait_details.get("next_arrival_minutes") or [],
            "live_wait_minutes": wait_details.get("live_wait_minutes"),
            "fallback_minutes": float(wait_details.get("fallback_minutes") or 0.0),
        }

    def _edge_cost(
        self,
        edge: dict[str, Any],
        weights: dict[str, Any],
        current_cost_minutes: float,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> float:
        kind = str(edge.get("kind") or "")
        base_minutes = max(0.0, float(edge.get("minutes", 0.0)))

        if kind == "ride":
            predicted_ride = self._predict_ride_edge_minutes(edge, current_cost_minutes, weights)
            return predicted_ride * weights["ride_multiplier"]

        if kind == "direction_switch":
            if runtime_metadata and runtime_metadata.get("applied_minutes") is not None:
                return float(runtime_metadata.get("applied_minutes") or 0.0)
            return float(self._transfer_runtime(edge, weights, current_cost_minutes)["applied_minutes"])

        if kind == "line_switch":
            if runtime_metadata and runtime_metadata.get("applied_minutes") is not None:
                return float(runtime_metadata.get("applied_minutes") or 0.0)
            return float(self._transfer_runtime(edge, weights, current_cost_minutes)["applied_minutes"])

        if kind == "station_transfer":
            if runtime_metadata and runtime_metadata.get("applied_minutes") is not None:
                return float(runtime_metadata.get("applied_minutes") or 0.0)
            return float(self._transfer_runtime(edge, weights, current_cost_minutes)["applied_minutes"])

        return base_minutes

    def _build_legs(
        self,
        path_edges: list[dict[str, Any]],
        stations: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        legs: list[dict[str, Any]] = []
        current_ride: dict[str, Any] | None = None

        def flush_ride():
            nonlocal current_ride
            if current_ride is not None:
                current_ride["stop_count"] = max(0, len(current_ride.get("stops", [])) - 1)
                current_ride["depart_cumulative_minutes"] = round(
                    float(current_ride.get("depart_cumulative_minutes") or 0.0),
                    2,
                )
                current_ride["arrive_cumulative_minutes"] = round(
                    float(current_ride.get("arrive_cumulative_minutes") or 0.0),
                    2,
                )
                normalized_stop_times: list[dict[str, Any]] = []
                for entry in current_ride.get("stop_times_minutes") or []:
                    if not isinstance(entry, dict):
                        continue
                    station_id = str(entry.get("station_id") or "").strip().upper()
                    if not station_id:
                        continue
                    try:
                        cumulative_minutes = float(entry.get("cumulative_minutes"))
                    except (TypeError, ValueError):
                        continue
                    rounded_minutes = round(cumulative_minutes, 2)
                    if normalized_stop_times and normalized_stop_times[-1].get("station_id") == station_id:
                        normalized_stop_times[-1]["cumulative_minutes"] = rounded_minutes
                    else:
                        normalized_stop_times.append(
                            {
                                "station_id": station_id,
                                "cumulative_minutes": rounded_minutes,
                            }
                        )
                current_ride["stop_times_minutes"] = normalized_stop_times
                legs.append(current_ride)
                current_ride = None

        for edge in path_edges:
            kind = edge.get("kind")
            if kind == "ride":
                line = str(edge.get("line") or "")
                direction = _normalize_direction(edge.get("direction") or edge.get("to_direction") or "")
                from_station = str(edge.get("from_station") or "")
                to_station = str(edge.get("to_station") or "")
                minutes = float(edge.get("minutes") or 0.0)
                edge_depart = float(edge.get("depart_cumulative_minutes") or 0.0)
                edge_arrive = float(
                    edge.get("arrive_cumulative_minutes")
                    or (edge_depart + float(edge.get("applied_minutes") or minutes))
                    or 0.0
                )

                if (
                    current_ride
                    and current_ride.get("line") == line
                    and current_ride.get("direction") == direction
                    and current_ride.get("to_station") == from_station
                ):
                    current_ride["to_station"] = to_station
                    current_ride["to_name"] = (stations.get(to_station) or {}).get("name", to_station)
                    current_ride["base_minutes"] += minutes
                    current_ride["minutes"] += float(edge.get("applied_minutes") or minutes)
                    current_ride["arrive_cumulative_minutes"] = edge_arrive
                    current_ride["stops"].append(to_station)
                    stop_times = current_ride.setdefault("stop_times_minutes", [])
                    if stop_times and stop_times[-1].get("station_id") == to_station:
                        stop_times[-1]["cumulative_minutes"] = edge_arrive
                    else:
                        stop_times.append(
                            {
                                "station_id": to_station,
                                "cumulative_minutes": edge_arrive,
                            }
                        )
                else:
                    flush_ride()
                    current_ride = {
                        "kind": "ride",
                        "line": line,
                        "direction": direction,
                        "from_station": from_station,
                        "from_name": (stations.get(from_station) or {}).get("name", from_station),
                        "to_station": to_station,
                        "to_name": (stations.get(to_station) or {}).get("name", to_station),
                        "stops": [from_station, to_station],
                        "base_minutes": minutes,
                        "minutes": float(edge.get("applied_minutes") or minutes),
                        "depart_cumulative_minutes": edge_depart,
                        "arrive_cumulative_minutes": edge_arrive,
                        "stop_times_minutes": [
                            {
                                "station_id": from_station,
                                "cumulative_minutes": edge_depart,
                            },
                            {
                                "station_id": to_station,
                                "cumulative_minutes": edge_arrive,
                            },
                        ],
                    }
                continue

            flush_ride()
            transfer_leg = {
                "kind": str(kind or "transfer"),
                "from_station": str(edge.get("from_station") or ""),
                "from_name": (stations.get(str(edge.get("from_station") or "")) or {}).get(
                    "name",
                    str(edge.get("from_station") or ""),
                ),
                "to_station": str(edge.get("to_station") or ""),
                "to_name": (stations.get(str(edge.get("to_station") or "")) or {}).get(
                    "name",
                    str(edge.get("to_station") or ""),
                ),
                "from_line": str(edge.get("from_line") or ""),
                "to_line": str(edge.get("to_line") or ""),
                "from_direction": _normalize_direction(edge.get("from_direction") or ""),
                "to_direction": _normalize_direction(edge.get("to_direction") or edge.get("direction") or ""),
                "minutes": float(edge.get("applied_minutes") or edge.get("minutes") or 0.0),
                "walk_minutes": float(edge.get("minutes") or 0.0),
                "depart_cumulative_minutes": round(float(edge.get("depart_cumulative_minutes") or 0.0), 2),
                "arrive_cumulative_minutes": round(float(edge.get("arrive_cumulative_minutes") or 0.0), 2),
                "transfer_ready_minute": round(float(edge.get("transfer_ready_minute") or 0.0), 2),
                "next_arrival_minutes": [
                    round(float(value), 2)
                    for value in (edge.get("next_arrival_minutes") or [])
                    if _to_float(value, -1.0) >= 0.0
                ],
                "live_wait_minutes": (
                    round(float(edge.get("live_wait_minutes")), 2)
                    if edge.get("live_wait_minutes") is not None
                    else None
                ),
                "uses_live_arrivals": bool(edge.get("uses_live_arrivals")),
                "fallback_minutes": round(float(edge.get("fallback_minutes") or 0.0), 2),
            }
            legs.append(transfer_leg)

        flush_ride()
        return legs

    def plan(
        self,
        origin: str,
        destination: str,
        weights: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        cache = self._ensure_loaded(force_refresh=force_refresh)
        if not cache.get("available"):
            return {
                "available": False,
                "error": cache.get("error") or "Planner graph unavailable.",
                "status_code": 503,
                "graph": {
                    "loaded_at": cache.get("loaded_at"),
                    "node_count": cache.get("node_count", 0),
                    "edge_count": cache.get("edge_count", 0),
                    "source": cache.get("source", {}),
                },
            }

        resolved_origin = self._resolve_station_id(cache, origin)
        resolved_destination = self._resolve_station_id(cache, destination)
        if not resolved_origin or not resolved_destination:
            return {
                "available": True,
                "error": "Unknown origin or destination station.",
                "status_code": 404,
                "graph": {
                    "loaded_at": cache.get("loaded_at"),
                    "node_count": cache.get("node_count", 0),
                    "edge_count": cache.get("edge_count", 0),
                    "source": cache.get("source", {}),
                },
            }

        station_lines = cache.get("station_lines") or {}
        station_line_directions = cache.get("station_line_directions") or {}
        adjacency = cache.get("adjacency") or {}
        stations = cache.get("stations") or {}
        origin_station_ids = self._resolve_station_complex_ids(cache, resolved_origin)
        origin_lines = sorted(
            {
                line
                for station_id in origin_station_ids
                for line in (station_lines.get(station_id) or [])
            }
        )
        destination_station_ids = self._resolve_station_complex_ids(cache, resolved_destination)
        destination_lines = sorted(
            {
                line
                for station_id in destination_station_ids
                for line in (station_lines.get(station_id) or [])
            }
        )
        if not origin_lines or not destination_lines:
            return {
                "available": True,
                "error": "No line data available for origin or destination station.",
                "status_code": 422,
                "graph": {
                    "loaded_at": cache.get("loaded_at"),
                    "node_count": cache.get("node_count", 0),
                    "edge_count": cache.get("edge_count", 0),
                    "source": cache.get("source", {}),
                },
            }

        normalized_weights = {
            "ride_multiplier": max(0.01, _to_float((weights or {}).get("ride_multiplier"), 1.0)),
            "direction_switch_penalty": max(
                0.0,
                _to_float((weights or {}).get("direction_switch_penalty"), self.line_switch_penalty_minutes),
            ),
            "line_switch_penalty": max(
                0.0,
                _to_float((weights or {}).get("line_switch_penalty"), self.line_switch_penalty_minutes),
            ),
            "station_transfer_penalty": max(
                0.0,
                _to_float((weights or {}).get("station_transfer_penalty"), self.station_transfer_penalty_minutes),
            ),
            "transfer_time_multiplier": max(
                0.0,
                _to_float((weights or {}).get("transfer_time_multiplier"), 1.0),
            ),
            "dynamic_wait_enabled": _to_bool(
                (weights or {}).get("dynamic_wait_enabled"),
                default_value=True,
            ),
            "default_wait_minutes": max(
                0.0,
                _to_float((weights or {}).get("default_wait_minutes"), 6.0),
            ),
            "arrival_limit": max(
                1,
                min(
                    int(_to_float((weights or {}).get("arrival_limit"), 5.0)),
                    10,
                ),
            ),
            "model_time_epoch": _to_float((weights or {}).get("model_time_epoch"), float(time.time())),
            "model_day_of_week": self._normalize_day_of_week((weights or {}).get("model_day_of_week")),
            "model_recent_rides": self._normalize_recent_rides((weights or {}).get("model_recent_rides")),
        }

        start_nodes = [
            _node_key(station_id, line, direction)
            for station_id in origin_station_ids
            for line in (station_lines.get(station_id) or [])
            for direction in (station_line_directions.get(station_id, {}).get(line) or [])
            if _node_key(station_id, line, direction) in adjacency
        ]
        goal_nodes = {
            _node_key(station_id, line, direction)
            for station_id in destination_station_ids
            for line in (station_lines.get(station_id) or [])
            for direction in (station_line_directions.get(station_id, {}).get(line) or [])
            if _node_key(station_id, line, direction) in adjacency
        }
        if not start_nodes or not goal_nodes:
            return {
                "available": True,
                "error": "No route nodes available for origin or destination.",
                "status_code": 422,
                "graph": {
                    "loaded_at": cache.get("loaded_at"),
                    "node_count": cache.get("node_count", 0),
                    "edge_count": cache.get("edge_count", 0),
                    "source": cache.get("source", {}),
                },
            }

        distances: dict[str, float] = {}
        previous: dict[str, tuple[str, dict[str, Any]]] = {}
        heap: list[tuple[float, str]] = []
        start_boarding_runtime: dict[str, dict[str, Any]] = {}
        for node in start_nodes:
            station_id, line_id, direction = _split_node_key(node)
            boarding_runtime = self._origin_boarding_runtime(
                station_id=station_id,
                line_id=line_id,
                direction=direction,
                weights=normalized_weights,
            )
            initial_cost = max(0.0, float(boarding_runtime.get("applied_minutes") or 0.0))
            existing_cost = distances.get(node)
            if existing_cost is None or initial_cost < existing_cost:
                distances[node] = initial_cost
                start_boarding_runtime[node] = boarding_runtime
                heapq.heappush(heap, (initial_cost, node))

        visited: set[str] = set()
        best_goal = ""
        while heap:
            current_cost, node = heapq.heappop(heap)
            known_cost = distances.get(node)
            if known_cost is None or current_cost > known_cost:
                continue
            if node in visited:
                continue
            visited.add(node)

            if node in goal_nodes:
                best_goal = node
                break

            for edge in adjacency.get(node, []):
                to_node = str(edge.get("to") or "")
                if not to_node:
                    continue
                runtime_metadata: dict[str, Any] = {}
                if str(edge.get("kind") or "") in {"direction_switch", "line_switch", "station_transfer"}:
                    runtime_metadata = self._transfer_runtime(edge, normalized_weights, current_cost)
                edge_weight = self._edge_cost(
                    edge,
                    normalized_weights,
                    current_cost,
                    runtime_metadata=runtime_metadata,
                )
                if edge_weight < 0:
                    continue
                candidate_cost = current_cost + edge_weight
                if candidate_cost < distances.get(to_node, float("inf")):
                    distances[to_node] = candidate_cost
                    previous[to_node] = (
                        node,
                        {
                            **edge,
                            **runtime_metadata,
                            "applied_minutes": float(edge_weight),
                            "depart_cumulative_minutes": float(current_cost),
                            "arrive_cumulative_minutes": float(candidate_cost),
                        },
                    )
                    heapq.heappush(heap, (candidate_cost, to_node))

        if not best_goal:
            return {
                "available": True,
                "error": "No path found between origin and destination.",
                "status_code": 404,
                "graph": {
                    "loaded_at": cache.get("loaded_at"),
                    "node_count": cache.get("node_count", 0),
                    "edge_count": cache.get("edge_count", 0),
                    "source": cache.get("source", {}),
                },
            }

        path_nodes: list[str] = [best_goal]
        path_edges: list[dict[str, Any]] = []
        cursor = best_goal
        while cursor in previous:
            prev_node, edge = previous[cursor]
            path_edges.append(edge)
            path_nodes.append(prev_node)
            cursor = prev_node
        path_nodes.reverse()
        path_edges.reverse()

        legs = self._build_legs(path_edges, stations)
        best_start_runtime = start_boarding_runtime.get(path_nodes[0], {})
        weighted_minutes = float(distances.get(best_goal, 0.0))
        base_ride_minutes = sum(float(edge.get("minutes") or 0.0) for edge in path_edges if edge.get("kind") == "ride")
        transfer_walk_minutes = sum(
            float(edge.get("minutes") or 0.0) for edge in path_edges if edge.get("kind") == "station_transfer"
        )
        transfer_count = sum(
            1
            for edge in path_edges
            if edge.get("kind") in {"direction_switch", "line_switch", "station_transfer"}
        )
        line_change_count = sum(
            1
            for edge in path_edges
            if edge.get("kind") in {"line_switch", "station_transfer"}
            and str(edge.get("from_line") or "") != str(edge.get("to_line") or "")
        )
        suggested_lines = sorted(
            {
                str(leg.get("line") or "").strip().upper()
                for leg in legs
                if leg.get("kind") == "ride" and str(leg.get("line") or "").strip()
            }
        )

        nodes_payload = []
        for node in path_nodes:
            station_id, line_id, direction = _split_node_key(node)
            station = stations.get(station_id) or {}
            nodes_payload.append(
                {
                    "node": node,
                    "station_id": station_id,
                    "station_name": station.get("name", station_id),
                    "line": line_id,
                    "direction": direction,
                }
            )

        station_sequence_payload: list[dict[str, Any]] = []
        last_station_id = ""
        for node_payload in nodes_payload:
            station_id = str(node_payload.get("station_id") or "")
            if not station_id or station_id == last_station_id:
                continue
            station_sequence_payload.append(
                {
                    "station_id": station_id,
                    "station_name": str(node_payload.get("station_name") or station_id),
                }
            )
            last_station_id = station_id

        origin_station = stations.get(resolved_origin) or {}
        destination_station = stations.get(resolved_destination) or {}
        direct_distance = _haversine_miles(
            (
                float(origin_station.get("latitude", 0.0)),
                float(origin_station.get("longitude", 0.0)),
            ),
            (
                float(destination_station.get("latitude", 0.0)),
                float(destination_station.get("longitude", 0.0)),
            ),
        )

        return {
            "available": True,
            "error": "",
            "status_code": 200,
            "from": {
                "station_id": resolved_origin,
                "name": origin_station.get("name", resolved_origin),
                "borough": origin_station.get("borough", ""),
                "latitude": float(origin_station.get("latitude", 0.0)),
                "longitude": float(origin_station.get("longitude", 0.0)),
                "lines": list(origin_lines),
                "complex_station_ids": origin_station_ids,
            },
            "to": {
                "station_id": resolved_destination,
                "name": destination_station.get("name", resolved_destination),
                "borough": destination_station.get("borough", ""),
                "latitude": float(destination_station.get("latitude", 0.0)),
                "longitude": float(destination_station.get("longitude", 0.0)),
                "lines": list(destination_lines),
                "complex_station_ids": destination_station_ids,
            },
            "summary": {
                "weighted_minutes": round(weighted_minutes, 2),
                "base_ride_minutes": round(base_ride_minutes, 2),
                "transfer_walk_minutes": round(transfer_walk_minutes, 2),
                "origin_wait_minutes": round(float(best_start_runtime.get("applied_minutes") or 0.0), 2),
                "transfer_count": transfer_count,
                "line_change_count": line_change_count,
                "station_count": len(station_sequence_payload),
                "node_hops": max(0, len(path_nodes) - 1),
            },
            "origin_boarding": {
                "minutes": round(float(best_start_runtime.get("applied_minutes") or 0.0), 2),
                "uses_live_arrivals": bool(best_start_runtime.get("uses_live_arrivals")),
                "next_arrival_minutes": [
                    round(float(value), 2)
                    for value in (best_start_runtime.get("next_arrival_minutes") or [])
                    if _to_float(value, -1.0) >= 0.0
                ],
                "live_wait_minutes": (
                    round(float(best_start_runtime.get("live_wait_minutes")), 2)
                    if best_start_runtime.get("live_wait_minutes") is not None
                    else None
                ),
                "fallback_minutes": round(float(best_start_runtime.get("fallback_minutes") or 0.0), 2),
            },
            "weights": normalized_weights,
            "suggested_lines": suggested_lines,
            "direct_distance_miles": round(float(direct_distance), 2),
            "path": {
                "nodes": nodes_payload,
                "station_sequence": station_sequence_payload,
                "legs": legs,
            },
            "graph": {
                "loaded_at": cache.get("loaded_at"),
                "node_count": cache.get("node_count", 0),
                "edge_count": cache.get("edge_count", 0),
                "source": cache.get("source", {}),
            },
            "runtime_cache": self.runtime_status(),
        }
