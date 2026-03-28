#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib import error, request

from dotenv import load_dotenv
from google.transit import gtfs_realtime_pb2
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


MTA_FEED_PATHS_DEFAULT = [
    "nyct%2Fgtfs",
    "nyct%2Fgtfs-ace",
    "nyct%2Fgtfs-bdfm",
    "nyct%2Fgtfs-g",
    "nyct%2Fgtfs-jz",
    "nyct%2Fgtfs-l",
    "nyct%2Fgtfs-nqrw",
    "nyct%2Fgtfs-si",
]

MTA_FEED_BASE_URLS_DEFAULT = [
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds",
    "https://api.mta.info/Dataservice/mtagtfsfeeds",
]


class Base(DeclarativeBase):
    pass


class RealtimeSnapshotLog(Base):
    __tablename__ = "realtime_snapshot_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_label: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    fetched_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_timestamp_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    auth_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    used_cached_vehicles: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vehicle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trip_update_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    feed_errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TripUpdatePredictionLog(Base):
    __tablename__ = "trip_update_prediction_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("realtime_snapshot_log.id"), nullable=False, index=True)
    run_label: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    fetched_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    route_id: Mapped[str] = mapped_column(String(16), nullable=False, default="", index=True)
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    stop_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    arrival_time_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_time_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_relationship: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trip_timestamp_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    snapshot: Mapped[RealtimeSnapshotLog] = relationship()

    __table_args__ = (
        Index("ix_trip_update_prediction_trip_stop_fetch", "trip_id", "stop_id", "fetched_at_epoch"),
    )


class VehicleStopArrivalLog(Base):
    __tablename__ = "vehicle_stop_arrival_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("realtime_snapshot_log.id"), nullable=False, index=True)
    run_label: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    event_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    snapshot_fetched_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vehicle_key: Mapped[str] = mapped_column(String(160), nullable=False, default="", index=True)
    vehicle_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    route_id: Mapped[str] = mapped_column(String(16), nullable=False, default="", index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    stop_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    snapshot: Mapped[RealtimeSnapshotLog] = relationship()

    __table_args__ = (
        Index("ix_vehicle_stop_arrival_trip_stop_event", "trip_id", "stop_id", "event_epoch"),
    )


class TripUpdateArrivalEvalLog(Base):
    __tablename__ = "trip_update_arrival_eval_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    arrival_event_id: Mapped[int] = mapped_column(
        ForeignKey("vehicle_stop_arrival_log.id"),
        nullable=False,
        index=True,
    )
    prediction_id: Mapped[int | None] = mapped_column(
        ForeignKey("trip_update_prediction_log.id"),
        nullable=True,
        index=True,
    )
    run_label: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    prediction_field: Mapped[str] = mapped_column(String(16), nullable=False, default="arrival")
    match_reason: Mapped[str] = mapped_column(String(40), nullable=False, default="", index=True)
    event_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    prediction_snapshot_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    predicted_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lead_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    residual_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_id: Mapped[str] = mapped_column(String(16), nullable=False, default="", index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    stop_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    arrival_event: Mapped[VehicleStopArrivalLog] = relationship()
    prediction: Mapped[TripUpdatePredictionLog | None] = relationship()

    __table_args__ = (
        Index("ix_trip_update_arrival_eval_trip_stop_event", "trip_id", "stop_id", "event_epoch"),
    )


class VehicleStateCache(Base):
    __tablename__ = "vehicle_state_cache"

    vehicle_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    route_id: Mapped[str] = mapped_column(String(16), nullable=False, default="", index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    trip_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    stop_id: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    event_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_fetched_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)


def parse_csv_env(raw_value: str, default_values: list[str]) -> list[str]:
    value = str(raw_value or "").strip()
    if not value:
        return list(default_values)
    parsed = [item.strip() for item in value.split(",") if item.strip()]
    return parsed or list(default_values)


def _normalize_route(route_id: str) -> str:
    value = (route_id or "").strip().upper()
    if not value:
        return ""
    if value.endswith("X") and len(value) > 1:
        return value[:-1]
    return value


def _normalize_direction(raw_value: str) -> str:
    value = str(raw_value or "").strip().lower()
    if value in {"n", "north", "0"}:
        return "north"
    if value in {"s", "south", "1"}:
        return "south"
    return ""


def _direction_from_stop_id(stop_id: str) -> str:
    value = str(stop_id or "").strip().upper()
    if value.endswith("N"):
        return "north"
    if value.endswith("S"):
        return "south"
    return ""


def _finite_float(raw_value: Any) -> float | None:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value


def parse_epoch(raw_value: Any) -> int | None:
    if raw_value is None:
        return None
    try:
        value = int(float(raw_value))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_int(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def normalize_stop_id(raw_value: Any) -> str:
    return str(raw_value or "").strip().upper()


def vehicle_tracking_key(vehicle: dict[str, Any]) -> tuple[str, str]:
    vehicle_id = str(vehicle.get("vehicle_id") or "").strip()
    trip_id = str(vehicle.get("trip_id") or "").strip()
    if vehicle_id:
        return (f"vehicle:{vehicle_id}", vehicle_id)
    if trip_id:
        return (f"trip:{trip_id}", "")
    return ("", "")


def prediction_epoch_from_values(arrival_epoch: int | None, departure_epoch: int | None, prediction_field: str) -> int | None:
    if prediction_field == "arrival":
        return arrival_epoch
    if prediction_field == "departure":
        return departure_epoch
    return arrival_epoch if arrival_epoch is not None else departure_epoch


def prediction_epoch_from_model(model: TripUpdatePredictionLog, prediction_field: str) -> int | None:
    return prediction_epoch_from_values(
        parse_epoch(model.arrival_time_epoch),
        parse_epoch(model.departure_time_epoch),
        prediction_field,
    )


def pick_prediction_from_cache(entries: list[dict[str, Any]], actual_epoch: int) -> dict[str, Any] | None:
    for item in reversed(entries):
        fetched_at = parse_epoch(item.get("fetched_at_epoch"))
        predicted_epoch = parse_epoch(item.get("predicted_epoch"))
        if fetched_at is None or predicted_epoch is None:
            continue
        if fetched_at <= actual_epoch:
            return item
    return None


def should_continue(iteration: int, deadline_epoch: float | None, args: argparse.Namespace) -> bool:
    if args.iterations > 0 and iteration >= args.iterations:
        return False
    if deadline_epoch is not None and time.time() >= deadline_epoch:
        return False
    return True


class MTARealtimeClient:
    def __init__(
        self,
        api_key: str,
        feed_paths: list[str],
        feed_base_urls: list[str],
        timeout_seconds: int = 8,
        cache_seconds: int = 20,
        max_age_seconds: int = 240,
    ):
        self.api_key = (api_key or "").strip()
        self.feed_paths = list(feed_paths)
        self.feed_base_urls = [str(item).strip().rstrip("/") for item in feed_base_urls if str(item).strip()]
        self.timeout_seconds = max(2, int(timeout_seconds))
        self.cache_seconds = max(5, int(cache_seconds))
        self.max_age_seconds = max(30, int(max_age_seconds))
        self._cache: dict[str, Any] = {
            "fetched_at": 0,
            "vehicles": [],
            "trip_updates": [],
            "status": "unknown",
            "error": None,
            "warning": "",
            "feed_errors": [],
            "source_timestamp": None,
            "used_cached_vehicles": False,
            "auth_mode": "x-api-key" if self.api_key else "anonymous",
        }

    def _feed_urls(self, feed_path: str) -> list[str]:
        normalized = str(feed_path or "").strip().lstrip("/")
        decoded = normalized.replace("%2F", "/").replace("%2f", "/")
        urls: list[str] = []
        for base_url in self.feed_base_urls:
            urls.append(f"{base_url}/{normalized}")
            if decoded != normalized:
                urls.append(f"{base_url}/{decoded}")
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped

    def snapshot(self, force_refresh: bool = False) -> dict[str, Any]:
        now = int(time.time())
        cache_age = now - int(self._cache.get("fetched_at") or 0)
        if not force_refresh and cache_age <= self.cache_seconds:
            return dict(self._cache)

        previous = dict(self._cache)
        data = self._fetch(now=now)
        if not data.get("vehicles") and previous.get("vehicles"):
            data["vehicles"] = previous.get("vehicles") or []
            data["source_timestamp"] = previous.get("source_timestamp")
            data["used_cached_vehicles"] = True
            feed_errors = data.get("feed_errors") or []
            feed_errors.append("serving cached vehicle positions from previous successful snapshot")
            data["feed_errors"] = feed_errors
            data["status"] = "degraded"
            data["warning"] = "Using cached vehicle positions while upstream feed refresh recovers."
            data["error"] = None
        if not data.get("trip_updates") and previous.get("trip_updates"):
            data["trip_updates"] = previous.get("trip_updates") or []

        self._cache = data
        return dict(self._cache)

    def _fetch(self, now: int) -> dict[str, Any]:
        vehicles: list[dict[str, Any]] = []
        trip_updates: list[dict[str, Any]] = []
        feed_errors: list[str] = []
        source_timestamps: list[int] = []
        seen_vehicle_keys: set[str] = set()
        seen_trip_update_keys: set[str] = set()
        auth_mode = "x-api-key" if self.api_key else "anonymous"

        for feed_path in self.feed_paths:
            payload: bytes | None = None
            last_error: str | None = None
            for url in self._feed_urls(feed_path):
                headers = {"User-Agent": "TripUpdateSQLRecorder/1.0"}
                if self.api_key:
                    headers["x-api-key"] = self.api_key
                    headers["Ocp-Apim-Subscription-Key"] = self.api_key
                req = request.Request(url, headers=headers)
                try:
                    with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                        payload = resp.read()
                    break
                except error.HTTPError as exc:
                    last_error = f"{url}: HTTP {exc.code}"
                except error.URLError as exc:
                    last_error = f"{url}: {exc.reason}"
                except Exception as exc:
                    last_error = f"{url}: {exc}"

            if payload is None:
                feed_errors.append(f"{feed_path}: {last_error or 'request failed'}")
                continue

            feed = gtfs_realtime_pb2.FeedMessage()
            try:
                feed.ParseFromString(payload)
            except Exception:
                feed_errors.append(f"{feed_path}: invalid protobuf payload")
                continue

            if feed.header and getattr(feed.header, "timestamp", 0):
                source_timestamps.append(int(feed.header.timestamp))

            for entity in feed.entity:
                if entity.HasField("trip_update"):
                    trip_update = entity.trip_update
                    trip_descriptor = trip_update.trip if trip_update.HasField("trip") else None
                    route_id = _normalize_route(trip_descriptor.route_id if trip_descriptor else "")
                    trip_id = str(trip_descriptor.trip_id if trip_descriptor else "").strip()
                    inferred_direction = ""
                    latest_update_time = 0
                    stop_updates_payload: list[dict[str, Any]] = []
                    for stop_update in trip_update.stop_time_update:
                        stop_id = str(stop_update.stop_id or "").strip()
                        arrival_time = (
                            int(stop_update.arrival.time)
                            if stop_update.HasField("arrival") and getattr(stop_update.arrival, "time", 0)
                            else None
                        )
                        departure_time = (
                            int(stop_update.departure.time)
                            if stop_update.HasField("departure") and getattr(stop_update.departure, "time", 0)
                            else None
                        )
                        if arrival_time:
                            latest_update_time = max(latest_update_time, arrival_time)
                        if departure_time:
                            latest_update_time = max(latest_update_time, departure_time)
                        if not inferred_direction:
                            inferred_direction = _direction_from_stop_id(stop_id)
                        stop_updates_payload.append(
                            {
                                "stop_id": stop_id,
                                "arrival_time": arrival_time,
                                "departure_time": departure_time,
                                "scheduled_relationship": int(stop_update.schedule_relationship),
                            }
                        )

                    dedupe_key = trip_id or f"{route_id}:{inferred_direction}:{entity.id}"
                    if stop_updates_payload and dedupe_key not in seen_trip_update_keys:
                        seen_trip_update_keys.add(dedupe_key)
                        trip_updates.append(
                            {
                                "entity_id": str(entity.id or ""),
                                "route_id": route_id,
                                "trip_id": trip_id,
                                "direction": inferred_direction,
                                "timestamp": latest_update_time or None,
                                "stop_time_updates": stop_updates_payload,
                            }
                        )
                    if latest_update_time:
                        source_timestamps.append(latest_update_time)

                if not entity.HasField("vehicle"):
                    continue

                vehicle = entity.vehicle
                lat = None
                lon = None
                bearing = None
                speed_mps = None
                if vehicle.HasField("position"):
                    pos_lat = _finite_float(vehicle.position.latitude)
                    pos_lon = _finite_float(vehicle.position.longitude)
                    bearing = _finite_float(vehicle.position.bearing)
                    speed_mps = _finite_float(vehicle.position.speed)
                    if (
                        pos_lat is not None
                        and pos_lon is not None
                        and -90.0 <= pos_lat <= 90.0
                        and -180.0 <= pos_lon <= 180.0
                        and not (pos_lat == 0.0 and pos_lon == 0.0)
                    ):
                        lat = pos_lat
                        lon = pos_lon

                route_id = _normalize_route(vehicle.trip.route_id if vehicle.HasField("trip") else "")
                vehicle_id = str(vehicle.vehicle.id if vehicle.HasField("vehicle") else "").strip()
                trip_id = str(vehicle.trip.trip_id if vehicle.HasField("trip") else "").strip()
                stop_id = str(vehicle.stop_id or "").strip()
                dedupe_key = vehicle_id or trip_id or str(entity.id or "")
                if not dedupe_key or dedupe_key in seen_vehicle_keys:
                    continue
                seen_vehicle_keys.add(dedupe_key)

                timestamp = int(vehicle.timestamp) if vehicle.timestamp else 0
                is_stale = bool(timestamp and (now - timestamp) > self.max_age_seconds)
                if timestamp:
                    source_timestamps.append(timestamp)

                current_status = vehicle.current_status
                status_label = {
                    gtfs_realtime_pb2.VehiclePosition.INCOMING_AT: "INCOMING_AT",
                    gtfs_realtime_pb2.VehiclePosition.STOPPED_AT: "STOPPED_AT",
                    gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO: "IN_TRANSIT_TO",
                }.get(current_status, "UNKNOWN")

                vehicles.append(
                    {
                        "route_id": route_id,
                        "vehicle_id": vehicle_id,
                        "trip_id": trip_id,
                        "stop_id": stop_id,
                        "direction": _direction_from_stop_id(stop_id),
                        "status": status_label,
                        "latitude": lat,
                        "longitude": lon,
                        "bearing": bearing,
                        "speed_mps": speed_mps,
                        "timestamp": timestamp or None,
                        "_is_stale": is_stale,
                    }
                )

        fresh_vehicles = [item for item in vehicles if not item.get("_is_stale")]
        stale_vehicles = [item for item in vehicles if item.get("_is_stale")]
        if fresh_vehicles:
            vehicles = fresh_vehicles
        elif stale_vehicles:
            vehicles = stale_vehicles
            feed_errors.append(f"all vehicle timestamps are older than {self.max_age_seconds}s; showing stale positions")
        else:
            vehicles = []
            if not feed_errors:
                feed_errors.append("upstream returned zero vehicle positions")
            if trip_updates and auth_mode == "anonymous":
                feed_errors.append("trip updates present in anonymous mode; MTA_API_KEY may be required for vehicle positions")

        for vehicle in vehicles:
            vehicle.pop("_is_stale", None)

        vehicles.sort(key=lambda item: (item.get("timestamp") or 0), reverse=True)

        status = "ok"
        error_message = None
        warning_message = ""
        if vehicles:
            if feed_errors:
                status = "degraded"
                warning_message = "Vehicle positions are available, but one or more feeds reported issues."
        elif trip_updates:
            status = "degraded"
            unauthorized = any("HTTP 401" in item or "HTTP 403" in item for item in feed_errors)
            if auth_mode == "anonymous" or unauthorized:
                warning_message = "Trip updates are available, but vehicle positions may require MTA_API_KEY."
            else:
                warning_message = "Vehicle positions are temporarily unavailable; trip updates are still available."
        elif feed_errors:
            status = "down"
            unauthorized = any("HTTP 401" in item or "HTTP 403" in item for item in feed_errors)
            dns_lookup_failed = any(
                "nodename nor servname provided" in item
                or "Name or service not known" in item
                or "Temporary failure in name resolution" in item
                for item in feed_errors
            )
            if dns_lookup_failed:
                error_message = "Could not resolve MTA realtime host from server DNS/network."
            elif unauthorized and self.api_key:
                error_message = "MTA realtime request was rejected. Check MTA_API_KEY."
            elif unauthorized:
                error_message = "MTA realtime request was rejected by upstream."
            else:
                error_message = "MTA realtime feed is temporarily unavailable."

        return {
            "fetched_at": now,
            "vehicles": vehicles,
            "trip_updates": trip_updates,
            "status": status,
            "error": error_message,
            "warning": warning_message,
            "feed_errors": feed_errors,
            "source_timestamp": max(source_timestamps) if source_timestamps else None,
            "auth_mode": auth_mode,
            "used_cached_vehicles": False,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone TripUpdate recorder that writes predictions, actual arrivals, and eval rows to SQL.",
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""), help="SQLAlchemy database URL.")
    parser.add_argument("--mta-api-key", default=os.getenv("MTA_API_KEY", ""), help="Optional MTA API key.")
    parser.add_argument(
        "--mta-feed-paths",
        default=os.getenv("MTA_FEED_PATHS", ""),
        help="Comma-separated GTFS-RT feed paths.",
    )
    parser.add_argument(
        "--mta-feed-base-urls",
        default=os.getenv("MTA_FEED_BASE_URLS", ""),
        help="Comma-separated GTFS-RT base URLs.",
    )
    parser.add_argument(
        "--mta-timeout-seconds",
        type=int,
        default=int(os.getenv("MTA_TIMEOUT_SECONDS", "8") or "8"),
        help="HTTP timeout per feed request.",
    )
    parser.add_argument(
        "--mta-cache-seconds",
        type=int,
        default=int(os.getenv("MTA_CACHE_SECONDS", "20") or "20"),
        help="Local in-process cache window.",
    )
    parser.add_argument(
        "--mta-max-age-seconds",
        type=int,
        default=int(os.getenv("MTA_MAX_AGE_SECONDS", "240") or "240"),
        help="Drop stale vehicle records older than this threshold.",
    )
    parser.add_argument("--interval-seconds", type=float, default=20.0, help="Polling interval between snapshots.")
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Stop after this many snapshots (0 means unlimited unless --duration-minutes is set).",
    )
    parser.add_argument(
        "--duration-minutes",
        type=float,
        default=0.0,
        help="Stop after this many minutes (0 means unlimited unless --iterations is set).",
    )
    parser.add_argument(
        "--run-label",
        default="",
        help="Optional run identifier written on every SQL row. Defaults to UTC timestamp label.",
    )
    parser.add_argument(
        "--prediction-field",
        choices=("arrival", "departure", "either"),
        default="arrival",
        help="TripUpdate field used as prediction target.",
    )
    parser.add_argument(
        "--debounce-seconds",
        type=int,
        default=45,
        help="Minimum seconds between repeated arrival events for the same vehicle/trip/stop.",
    )
    parser.add_argument(
        "--max-prediction-age-minutes",
        type=float,
        default=180.0,
        help="Ignore predictions older than this relative to actual arrival time.",
    )
    parser.add_argument("--store-raw-snapshot", action="store_true", help="Store full snapshot JSON in SQL.")
    parser.add_argument("--use-cache", action="store_true", help="Use local cache instead of force-refreshing feeds.")
    parser.add_argument("--quiet", action="store_true", help="Reduce logging output.")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    database_url = str(args.database_url or "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required. Set it in .env or pass --database-url.")

    feed_paths = parse_csv_env(args.mta_feed_paths, MTA_FEED_PATHS_DEFAULT)
    feed_base_urls = parse_csv_env(args.mta_feed_base_urls, MTA_FEED_BASE_URLS_DEFAULT)
    run_label = str(args.run_label or "").strip() or datetime.utcnow().strftime("tripupdate_eval_%Y%m%dT%H%M%SZ")
    interval_seconds = max(1.0, float(args.interval_seconds))
    max_prediction_age_seconds = int(max(60.0, float(args.max_prediction_age_minutes) * 60.0))
    debounce_seconds = max(0, int(args.debounce_seconds))

    deadline_epoch = None
    if float(args.duration_minutes) > 0.0:
        deadline_epoch = time.time() + (float(args.duration_minutes) * 60.0)

    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    realtime_client = MTARealtimeClient(
        api_key=args.mta_api_key,
        feed_paths=feed_paths,
        feed_base_urls=feed_base_urls,
        timeout_seconds=args.mta_timeout_seconds,
        cache_seconds=args.mta_cache_seconds,
        max_age_seconds=args.mta_max_age_seconds,
    )

    vehicle_state: dict[str, dict[str, Any]] = {}
    recent_arrivals: dict[tuple[str, str, str], int] = {}
    prediction_cache: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    warm_cutoff = int(time.time()) - max_prediction_age_seconds
    with SessionLocal() as session:
        warm_rows = session.execute(
            select(TripUpdatePredictionLog)
            .where(TripUpdatePredictionLog.fetched_at_epoch >= warm_cutoff)
            .order_by(TripUpdatePredictionLog.fetched_at_epoch.asc(), TripUpdatePredictionLog.id.asc())
        ).scalars().all()
        for row in warm_rows:
            trip_id = str(row.trip_id or "").strip()
            stop_id = normalize_stop_id(row.stop_id)
            if not trip_id or not stop_id:
                continue
            predicted_epoch = prediction_epoch_from_model(row, args.prediction_field)
            if predicted_epoch is None:
                continue
            prediction_cache[(trip_id, stop_id)].append(
                {
                    "id": int(row.id),
                    "fetched_at_epoch": int(row.fetched_at_epoch),
                    "predicted_epoch": int(predicted_epoch),
                    "route_id": str(row.route_id or ""),
                    "direction": _normalize_direction(row.direction),
                }
            )

    if not args.quiet:
        print("Run label:", run_label)
        print("Prediction field:", args.prediction_field)
        print("Max prediction age seconds:", max_prediction_age_seconds)
        print("Debounce seconds:", debounce_seconds)
        print("Force refresh:", not args.use_cache)
        print("Warm predictions loaded:", sum(len(values) for values in prediction_cache.values()))

    iteration = 0
    total_predictions = 0
    total_arrivals = 0
    total_matched = 0

    while should_continue(iteration, deadline_epoch, args):
        now_epoch = int(time.time())
        snapshot = realtime_client.snapshot(force_refresh=not args.use_cache)
        if not isinstance(snapshot, dict):
            snapshot = {"fetched_at": now_epoch, "error": "invalid snapshot payload"}

        fetched_at = parse_epoch(snapshot.get("fetched_at")) or now_epoch
        source_timestamp = parse_epoch(snapshot.get("source_timestamp"))
        trip_updates = snapshot.get("trip_updates") if isinstance(snapshot.get("trip_updates"), list) else []
        vehicles = snapshot.get("vehicles") if isinstance(snapshot.get("vehicles"), list) else []
        feed_errors = snapshot.get("feed_errors") if isinstance(snapshot.get("feed_errors"), list) else []
        snapshot_status = str(snapshot.get("status") or "unknown")
        snapshot_warning = str(snapshot.get("warning") or "").strip()
        if snapshot_warning:
            feed_errors = list(feed_errors) + [f"warning: {snapshot_warning}"]

        iteration_predictions = 0
        iteration_arrivals = 0
        iteration_matched = 0

        with SessionLocal() as session:
            snapshot_row = RealtimeSnapshotLog(
                run_label=run_label,
                fetched_at_epoch=int(fetched_at),
                source_timestamp_epoch=source_timestamp,
                auth_mode=str(snapshot.get("auth_mode") or "unknown"),
                used_cached_vehicles=bool(snapshot.get("used_cached_vehicles")),
                vehicle_count=len(vehicles),
                trip_update_count=len(trip_updates),
                error=str(snapshot.get("error") or ""),
                feed_errors_json=json.dumps(feed_errors, separators=(",", ":"), sort_keys=True),
                payload_json=(json.dumps(snapshot, separators=(",", ":"), sort_keys=True) if args.store_raw_snapshot else ""),
            )
            session.add(snapshot_row)
            session.flush()

            prediction_models: list[TripUpdatePredictionLog] = []
            for trip_update in trip_updates:
                if not isinstance(trip_update, dict):
                    continue
                trip_id = str(trip_update.get("trip_id") or "").strip()
                route_id = str(trip_update.get("route_id") or "").strip().upper()
                direction = _normalize_direction(trip_update.get("direction"))
                entity_id = str(trip_update.get("entity_id") or "").strip()
                trip_timestamp = parse_epoch(trip_update.get("timestamp"))
                stop_time_updates = trip_update.get("stop_time_updates")
                if not isinstance(stop_time_updates, list):
                    continue
                for stop_update in stop_time_updates:
                    if not isinstance(stop_update, dict):
                        continue
                    stop_id = normalize_stop_id(stop_update.get("stop_id"))
                    if not stop_id:
                        continue
                    model = TripUpdatePredictionLog(
                        snapshot_id=int(snapshot_row.id),
                        run_label=run_label,
                        fetched_at_epoch=int(fetched_at),
                        route_id=route_id,
                        trip_id=trip_id,
                        direction=direction,
                        entity_id=entity_id,
                        stop_id=stop_id,
                        arrival_time_epoch=parse_epoch(stop_update.get("arrival_time")),
                        departure_time_epoch=parse_epoch(stop_update.get("departure_time")),
                        scheduled_relationship=parse_int(stop_update.get("scheduled_relationship")),
                        trip_timestamp_epoch=trip_timestamp,
                    )
                    session.add(model)
                    prediction_models.append(model)
                    iteration_predictions += 1

            session.flush()
            prediction_cutoff = int(fetched_at) - max_prediction_age_seconds

            for model in prediction_models:
                predicted_epoch = prediction_epoch_from_model(model, args.prediction_field)
                trip_id = str(model.trip_id or "").strip()
                stop_id = normalize_stop_id(model.stop_id)
                if predicted_epoch is None or not trip_id or not stop_id:
                    continue
                key = (trip_id, stop_id)
                values = prediction_cache[key]
                values.append(
                    {
                        "id": int(model.id),
                        "fetched_at_epoch": int(model.fetched_at_epoch),
                        "predicted_epoch": int(predicted_epoch),
                        "route_id": str(model.route_id or ""),
                        "direction": _normalize_direction(model.direction),
                    }
                )
                while values and int(values[0].get("fetched_at_epoch") or 0) < prediction_cutoff:
                    values.pop(0)
                if len(values) > 256:
                    del values[:-256]

            vehicle_keys: list[str] = []
            for vehicle in vehicles:
                if not isinstance(vehicle, dict):
                    continue
                tracking_key, _ = vehicle_tracking_key(vehicle)
                if tracking_key:
                    vehicle_keys.append(tracking_key)

            db_vehicle_state: dict[str, VehicleStateCache] = {}
            if vehicle_keys:
                existing_states = session.execute(
                    select(VehicleStateCache).where(VehicleStateCache.vehicle_key.in_(sorted(set(vehicle_keys))))
                ).scalars().all()
                db_vehicle_state = {str(row.vehicle_key): row for row in existing_states}

            for vehicle in vehicles:
                if not isinstance(vehicle, dict):
                    continue
                tracking_key, vehicle_id = vehicle_tracking_key(vehicle)
                if not tracking_key:
                    continue

                status = str(vehicle.get("status") or "").strip().upper()
                trip_id = str(vehicle.get("trip_id") or "").strip()
                route_id = str(vehicle.get("route_id") or "").strip().upper()
                stop_id = normalize_stop_id(vehicle.get("stop_id"))
                direction = _normalize_direction(vehicle.get("direction"))
                event_epoch = parse_epoch(vehicle.get("timestamp")) or int(fetched_at)

                previous = vehicle_state.get(tracking_key)
                if previous is None:
                    previous_row = db_vehicle_state.get(tracking_key)
                    if previous_row is not None:
                        previous = {
                            "status": str(previous_row.status or ""),
                            "trip_id": str(previous_row.trip_id or ""),
                            "stop_id": str(previous_row.stop_id or ""),
                            "timestamp": int(previous_row.event_epoch or 0),
                        }
                is_arrival = False
                if status == "STOPPED_AT" and stop_id and previous is not None:
                    was_stopped = str(previous.get("status") or "").upper() == "STOPPED_AT"
                    same_stop = normalize_stop_id(previous.get("stop_id")) == stop_id
                    same_trip = str(previous.get("trip_id") or "") == trip_id
                    if (not was_stopped) or (not same_stop) or (not same_trip):
                        is_arrival = True

                if is_arrival:
                    dedupe_key = (tracking_key, trip_id, stop_id)
                    previous_event_epoch = recent_arrivals.get(dedupe_key)
                    if previous_event_epoch is None or (int(event_epoch) - int(previous_event_epoch)) >= debounce_seconds:
                        duplicate_count = session.execute(
                            select(func.count())
                            .select_from(VehicleStopArrivalLog)
                            .where(
                                VehicleStopArrivalLog.vehicle_key == tracking_key,
                                VehicleStopArrivalLog.trip_id == trip_id,
                                VehicleStopArrivalLog.stop_id == stop_id,
                                VehicleStopArrivalLog.event_epoch >= int(event_epoch) - debounce_seconds,
                                VehicleStopArrivalLog.event_epoch <= int(event_epoch) + debounce_seconds,
                            )
                        ).scalar_one()
                        if int(duplicate_count or 0) == 0:
                            recent_arrivals[dedupe_key] = int(event_epoch)
                            iteration_arrivals += 1

                            arrival_row = VehicleStopArrivalLog(
                                snapshot_id=int(snapshot_row.id),
                                run_label=run_label,
                                event_epoch=int(event_epoch),
                                snapshot_fetched_at_epoch=int(fetched_at),
                                vehicle_key=tracking_key,
                                vehicle_id=vehicle_id,
                                route_id=route_id,
                                direction=direction,
                                trip_id=trip_id,
                                stop_id=stop_id,
                                status=status,
                            )
                            session.add(arrival_row)
                            session.flush()

                            match_reason = ""
                            prediction_id = None
                            prediction_snapshot_epoch = None
                            predicted_epoch = None
                            lead_seconds = None
                            residual_seconds = None
                            matched_route = ""
                            matched_direction = ""

                            if not trip_id:
                                match_reason = "missing_trip_id"
                            else:
                                key = (trip_id, stop_id)
                                candidate = pick_prediction_from_cache(prediction_cache.get(key, []), int(event_epoch))
                                if candidate is None:
                                    db_candidates = session.execute(
                                        select(TripUpdatePredictionLog)
                                        .where(
                                            TripUpdatePredictionLog.trip_id == trip_id,
                                            TripUpdatePredictionLog.stop_id == stop_id,
                                            TripUpdatePredictionLog.fetched_at_epoch <= int(event_epoch),
                                        )
                                        .order_by(
                                            TripUpdatePredictionLog.fetched_at_epoch.desc(),
                                            TripUpdatePredictionLog.id.desc(),
                                        )
                                        .limit(50)
                                    ).scalars().all()
                                    for row in db_candidates:
                                        candidate_epoch = prediction_epoch_from_model(row, args.prediction_field)
                                        if candidate_epoch is None:
                                            continue
                                        candidate = {
                                            "id": int(row.id),
                                            "fetched_at_epoch": int(row.fetched_at_epoch),
                                            "predicted_epoch": int(candidate_epoch),
                                            "route_id": str(row.route_id or ""),
                                            "direction": _normalize_direction(row.direction),
                                        }
                                        break

                                if candidate is None:
                                    match_reason = "no_prior_prediction"
                                else:
                                    prediction_id = int(candidate.get("id"))
                                    prediction_snapshot_epoch = int(candidate.get("fetched_at_epoch"))
                                    predicted_epoch = int(candidate.get("predicted_epoch"))
                                    matched_route = str(candidate.get("route_id") or "")
                                    matched_direction = str(candidate.get("direction") or "")
                                    lead_seconds = int(event_epoch) - int(prediction_snapshot_epoch)
                                    if int(lead_seconds) > max_prediction_age_seconds:
                                        match_reason = "prediction_too_old"
                                    else:
                                        match_reason = "matched"
                                        residual_seconds = int(event_epoch) - int(predicted_epoch)
                                        iteration_matched += 1

                            eval_row = TripUpdateArrivalEvalLog(
                                arrival_event_id=int(arrival_row.id),
                                prediction_id=prediction_id,
                                run_label=run_label,
                                prediction_field=args.prediction_field,
                                match_reason=match_reason,
                                event_epoch=int(event_epoch),
                                prediction_snapshot_epoch=prediction_snapshot_epoch,
                                predicted_epoch=predicted_epoch,
                                lead_seconds=lead_seconds,
                                residual_seconds=residual_seconds,
                                route_id=route_id or matched_route,
                                direction=direction or matched_direction,
                                trip_id=trip_id,
                                stop_id=stop_id,
                            )
                            session.add(eval_row)

                vehicle_state[tracking_key] = {
                    "status": status,
                    "trip_id": trip_id,
                    "stop_id": stop_id,
                    "timestamp": int(event_epoch),
                }

                cache_row = db_vehicle_state.get(tracking_key)
                if cache_row is None:
                    cache_row = VehicleStateCache(vehicle_key=tracking_key)
                    session.add(cache_row)
                    db_vehicle_state[tracking_key] = cache_row
                cache_row.vehicle_id = str(vehicle_id or "")
                cache_row.route_id = str(route_id or "")
                cache_row.direction = str(direction or "")
                cache_row.trip_id = str(trip_id or "")
                cache_row.stop_id = str(stop_id or "")
                cache_row.status = str(status or "")
                cache_row.event_epoch = int(event_epoch)
                cache_row.snapshot_fetched_at_epoch = int(fetched_at)
                cache_row.updated_at = datetime.utcnow()

            try:
                session.commit()
            except Exception as exc:
                session.rollback()
                if not args.quiet:
                    print(f"[{iteration + 1}] commit failed: {exc}")
                iteration += 1
                if should_continue(iteration, deadline_epoch, args):
                    time.sleep(interval_seconds)
                continue

        total_predictions += iteration_predictions
        total_arrivals += iteration_arrivals
        total_matched += iteration_matched

        if not args.quiet:
            fetched_iso = datetime.fromtimestamp(int(fetched_at)).isoformat()
            print(
                f"[{iteration + 1}] fetched_at={fetched_iso} "
                f"vehicles={len(vehicles)} trip_updates={len(trip_updates)} "
                f"predictions={iteration_predictions} arrivals={iteration_arrivals} matched={iteration_matched} "
                f"status={snapshot_status}"
                + (f" warning={snapshot_warning}" if snapshot_warning else "")
                + (f" error={snapshot.get('error')}" if snapshot.get("error") else ""),
            )

        if (iteration + 1) % 50 == 0:
            cleanup_cutoff = int(fetched_at) - max_prediction_age_seconds
            for key in list(prediction_cache.keys()):
                values = prediction_cache[key]
                values = [item for item in values if int(item.get("fetched_at_epoch") or 0) >= cleanup_cutoff]
                if values:
                    prediction_cache[key] = values[-256:]
                else:
                    prediction_cache.pop(key, None)

        iteration += 1
        if should_continue(iteration, deadline_epoch, args):
            time.sleep(interval_seconds)

    if not args.quiet:
        print(
            "Done:",
            json.dumps(
                {
                    "run_label": run_label,
                    "iterations": iteration,
                    "predictions_written": total_predictions,
                    "arrivals_detected": total_arrivals,
                    "matched_arrivals": total_matched,
                },
                sort_keys=True,
            ),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
