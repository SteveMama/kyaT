"""
MBTA Nearby — Personal transit helper.
Shows nearest stops, all routes serving each stop, real-time predictions,
walk-to-stop ETAs, and headsigns/directions.
"""

import os
import math
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import requests
from flask import Flask, jsonify, request, render_template, redirect

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")

MBTA_API_KEY = os.environ.get("MBTA_API_KEY", "")
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")

MBTA_BASE = "https://api-v3.mbta.com"
ORS_BASE = "https://api.openrouteservice.org"

DEFAULT_RADIUS = 0.01
MAX_STOPS = 12
MAX_PREDICTIONS_PER_STOP = 8
WALKING_SPEED_MPS = 1.4

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mbta_headers():
    h = {"Accept": "application/vnd.api+json"}
    if MBTA_API_KEY:
        h["x-api-key"] = MBTA_API_KEY
    return h


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def walk_eta_fallback(distance_m):
    return round(distance_m / WALKING_SPEED_MPS)


def walk_eta_ors(origin_lon, origin_lat, dest_lon, dest_lat):
    if not ORS_API_KEY:
        return None
    url = f"{ORS_BASE}/v2/directions/foot-walking"
    params = {
        "api_key": ORS_API_KEY,
        "start": f"{origin_lon},{origin_lat}",
        "end": f"{dest_lon},{dest_lat}",
    }
    try:
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return round(data["features"][0]["properties"]["summary"]["duration"])
    except Exception as e:
        log.warning("ORS routing failed: %s", e)
    return None


ROUTE_TYPE_LABELS = {0: "Light Rail", 1: "Subway", 2: "Commuter Rail", 3: "Bus", 4: "Ferry"}
ROUTE_TYPE_ICONS = {0: "🚊", 1: "🚇", 2: "🚆", 3: "🚌", 4: "⛴️"}


# ---------------------------------------------------------------------------
# MBTA API calls
# ---------------------------------------------------------------------------

def fetch_nearby_stops(lat, lon, radius=DEFAULT_RADIUS, limit=MAX_STOPS):
    url = f"{MBTA_BASE}/stops"
    params = {
        "filter[latitude]": lat,
        "filter[longitude]": lon,
        "filter[radius]": radius,
        "sort": "distance",
        "page[limit]": limit,
    }
    resp = requests.get(url, headers=mbta_headers(), params=params, timeout=10)
    resp.raise_for_status()
    stops = []
    for s in resp.json().get("data", []):
        attrs = s.get("attributes", {})
        stops.append({
            "stop_id": s["id"],
            "name": attrs.get("name", "Unknown"),
            "lat": attrs.get("latitude"),
            "lon": attrs.get("longitude"),
            "location_type": attrs.get("location_type", 0),
            "platform_code": attrs.get("platform_code"),
            "description": attrs.get("description"),
        })
    return stops


def fetch_routes_for_stop(stop_id):
    """Fetch all routes serving a given stop."""
    url = f"{MBTA_BASE}/routes"
    params = {"filter[stop]": stop_id}
    try:
        resp = requests.get(url, headers=mbta_headers(), params=params, timeout=10)
        resp.raise_for_status()
        routes = []
        for r in resp.json().get("data", []):
            a = r.get("attributes", {})
            routes.append({
                "route_id": r["id"],
                "short_name": a.get("short_name", ""),
                "long_name": a.get("long_name", ""),
                "color": a.get("color", ""),
                "text_color": a.get("text_color", ""),
                "type": a.get("type"),
                "type_label": ROUTE_TYPE_LABELS.get(a.get("type"), ""),
                "type_icon": ROUTE_TYPE_ICONS.get(a.get("type"), "🚏"),
                "description": a.get("description", ""),
                "direction_names": a.get("direction_names", []),
            })
        return routes
    except Exception as e:
        log.warning("Routes fetch for stop %s failed: %s", stop_id, e)
        return []


def fetch_predictions(stop_ids, limit_per_stop=MAX_PREDICTIONS_PER_STOP):
    url = f"{MBTA_BASE}/predictions"
    params = {
        "filter[stop]": ",".join(stop_ids),
        "sort": "time",
        "page[limit]": limit_per_stop * len(stop_ids),
        "include": "route,trip",
    }
    resp = requests.get(url, headers=mbta_headers(), params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    # Build lookups from included resources
    routes = {}
    trips = {}
    for inc in payload.get("included", []):
        if inc["type"] == "route":
            a = inc.get("attributes", {})
            routes[inc["id"]] = {
                "short_name": a.get("short_name", ""),
                "long_name": a.get("long_name", ""),
                "color": a.get("color", ""),
                "text_color": a.get("text_color", ""),
                "type": a.get("type"),
                "type_label": ROUTE_TYPE_LABELS.get(a.get("type"), ""),
                "type_icon": ROUTE_TYPE_ICONS.get(a.get("type"), "🚏"),
                "direction_names": a.get("direction_names", []),
            }
        elif inc["type"] == "trip":
            a = inc.get("attributes", {})
            trips[inc["id"]] = {
                "headsign": a.get("headsign", ""),
                "direction_id": a.get("direction_id"),
                "name": a.get("name", ""),
            }

    by_stop = {sid: [] for sid in stop_ids}
    for p in payload.get("data", []):
        attrs = p.get("attributes", {})
        rels = p.get("relationships", {})
        sid = (rels.get("stop", {}).get("data") or {}).get("id", "")
        rid = (rels.get("route", {}).get("data") or {}).get("id", "")
        tid = (rels.get("trip", {}).get("data") or {}).get("id", "")

        route_info = routes.get(rid, {})
        trip_info = trips.get(tid, {})

        direction = attrs.get("direction_id")
        direction_names = route_info.get("direction_names", [])
        direction_name = ""
        if direction is not None and direction < len(direction_names):
            direction_name = direction_names[direction]

        pred = {
            "route_id": rid,
            "route_name": route_info.get("short_name") or route_info.get("long_name") or rid,
            "route_long_name": route_info.get("long_name", ""),
            "route_color": route_info.get("color", ""),
            "route_text_color": route_info.get("text_color", ""),
            "route_type": route_info.get("type"),
            "route_type_label": route_info.get("type_label", ""),
            "route_type_icon": route_info.get("type_icon", ""),
            "direction_id": direction,
            "direction_name": direction_name,
            "headsign": trip_info.get("headsign", ""),
            "arrival_time": attrs.get("arrival_time"),
            "departure_time": attrs.get("departure_time"),
            "status": attrs.get("status"),
        }

        if sid in by_stop and len(by_stop[sid]) < limit_per_stop:
            by_stop[sid].append(pred)

    return by_stop


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    status = {"ok": True, "mbta_key_set": bool(MBTA_API_KEY), "ors_key_set": bool(ORS_API_KEY)}
    try:
        r = requests.get(f"{MBTA_BASE}/stops", headers=mbta_headers(),
                         params={"page[limit]": 1}, timeout=5)
        status["mbta_status"] = r.status_code
        status["mbta_ratelimit_remaining"] = r.headers.get("x-ratelimit-remaining")
    except Exception as e:
        status["mbta_status"] = str(e)
        status["ok"] = False
    return jsonify(status)


@app.route("/api/test")
def api_test():
    return redirect("/api/nearby?lat=42.3564&lon=-71.0624&radius=0.01")


@app.route("/api/nearby")
def api_nearby():
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lon query params required"}), 400

    radius = float(request.args.get("radius", DEFAULT_RADIUS))
    now = datetime.now(timezone.utc)

    # 1. Nearby stops
    try:
        stops = fetch_nearby_stops(lat, lon, radius)
    except requests.HTTPError as e:
        sc = e.response.status_code if e.response else 500
        if sc == 429:
            return jsonify({"error": "MBTA rate limit hit. Try again shortly."}), 429
        return jsonify({"error": f"MBTA stops API error ({sc})"}), 502
    except Exception as e:
        log.error("Stops fetch failed: %s", e)
        return jsonify({"error": "Failed to fetch nearby stops"}), 502

    if not stops:
        return jsonify({"stops": [], "updated_at": now.isoformat(),
                        "message": "No stops found nearby. Try a larger radius."})

    stop_ids = [s["stop_id"] for s in stops]

    # 2. Walk ETAs
    for s in stops:
        dist = haversine_m(lat, lon, s["lat"], s["lon"])
        s["distance_m"] = round(dist)
        ors_eta = walk_eta_ors(lon, lat, s["lon"], s["lat"])
        if ors_eta is not None:
            s["walk_seconds"] = ors_eta
            s["walk_method"] = "openrouteservice"
        else:
            s["walk_seconds"] = walk_eta_fallback(dist)
            s["walk_method"] = "estimate"
        s["walk_minutes"] = round(s["walk_seconds"] / 60, 1)

    # 3. Routes per stop
    for s in stops:
        s["routes"] = fetch_routes_for_stop(s["stop_id"])
        types_seen = sorted(set(r.get("type") for r in s["routes"] if r.get("type") is not None))
        s["stop_types"] = types_seen
        s["stop_type_icons"] = " ".join(ROUTE_TYPE_ICONS.get(t, "") for t in types_seen)

    # 4. Predictions
    try:
        preds_by_stop = fetch_predictions(stop_ids)
    except Exception as e:
        log.warning("Predictions fetch failed: %s", e)
        preds_by_stop = {sid: [] for sid in stop_ids}

    # 5. Compute leave-by
    for s in stops:
        s["predictions"] = preds_by_stop.get(s["stop_id"], [])
        for p in s["predictions"]:
            dep_str = p.get("departure_time") or p.get("arrival_time")
            if dep_str:
                try:
                    dep_dt = datetime.fromisoformat(dep_str)
                    secs_until = (dep_dt - now).total_seconds()
                    leave_in = secs_until - s["walk_seconds"]
                    p["departs_in_seconds"] = round(secs_until)
                    p["departs_in_minutes"] = round(secs_until / 60, 1)
                    p["leave_in_seconds"] = round(leave_in)
                    p["leave_in_minutes"] = round(leave_in / 60, 1)
                    p["catchable"] = leave_in > 0
                except Exception:
                    p["departs_in_seconds"] = None
                    p["leave_in_seconds"] = None
                    p["catchable"] = None
        s["has_predictions"] = len(s["predictions"]) > 0

    return jsonify({"stops": stops, "updated_at": now.isoformat(), "origin": {"lat": lat, "lon": lon}})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
