"""
MBTA Nearby — Personal transit helper.
Shows nearest stops, real-time predictions, and walk-to-stop ETAs.
"""

import os
import time
import math
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import requests
from flask import Flask, jsonify, request, render_template

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")

MBTA_API_KEY = os.environ.get("MBTA_API_KEY", "40b4403167ee4216978dded033c9870a")
ORS_API_KEY = os.environ.get("ORS_API_KEY", "eyJvcmciOiI1YjNjZTM1OTc4NTExMTAwMDFjZjYyNDgiLCJpZCI6IjNiOTE5N2RiMTU0MjQ2MzM4NmFjNmE3ZWVjZGYwMjk1IiwiaCI6Im11cm11cjY0In0=")

MBTA_BASE = "https://api-v3.mbta.com"
ORS_BASE = "https://api.openrouteservice.org"

# Defaults
DEFAULT_RADIUS = 0.01  # ~0.5 mi in degrees
MAX_STOPS = 8
MAX_PREDICTIONS_PER_STOP = 4
WALKING_SPEED_MPS = 1.4  # metres per second fallback

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
    """Return distance in metres between two lat/lon points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def walk_eta_fallback(distance_m):
    """Simple walking-speed estimate (seconds)."""
    return round(distance_m / WALKING_SPEED_MPS)


def walk_eta_ors(origin_lon, origin_lat, dest_lon, dest_lat):
    """
    Get walking duration via OpenRouteService Directions API.
    Returns seconds or None on failure.
    ORS uses [lon, lat] order.
    """
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
            # GeoJSON response: features[0].properties.summary.duration
            duration = data["features"][0]["properties"]["summary"]["duration"]
            return round(duration)
    except Exception as e:
        log.warning("ORS routing failed: %s", e)
    return None


# Route-type labels
ROUTE_TYPE_LABELS = {
    0: "Tram",
    1: "Subway",
    2: "Rail",
    3: "Bus",
    4: "Ferry",
}

ROUTE_TYPE_ICONS = {
    0: "🚊",
    1: "🚇",
    2: "🚆",
    3: "🚌",
    4: "⛴️",
}

# ---------------------------------------------------------------------------
# MBTA API calls
# ---------------------------------------------------------------------------

def fetch_nearby_stops(lat, lon, radius=DEFAULT_RADIUS, limit=MAX_STOPS):
    """Return list of nearby stops sorted by distance."""
    url = f"{MBTA_BASE}/stops"
    params = {
        "filter[latitude]": lat,
        "filter[longitude]": lon,
        "filter[radius]": radius,
        "sort": "distance",
        "page[limit]": limit,
        "fields[stop]": "name,latitude,longitude,location_type,platform_code,description",
    }
    resp = requests.get(url, headers=mbta_headers(), params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    stops = []
    for s in data:
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


def fetch_predictions(stop_ids, limit_per_stop=MAX_PREDICTIONS_PER_STOP):
    """
    Fetch predictions for a list of stop IDs.
    Returns dict: stop_id -> list of prediction dicts.
    """
    url = f"{MBTA_BASE}/predictions"
    params = {
        "filter[stop]": ",".join(stop_ids),
        "sort": "time",
        "page[limit]": limit_per_stop * len(stop_ids),
        "include": "route",
    }
    resp = requests.get(url, headers=mbta_headers(), params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    # Build route lookup from included
    routes = {}
    for inc in payload.get("included", []):
        if inc.get("type") == "route":
            r_attrs = inc.get("attributes", {})
            routes[inc["id"]] = {
                "short_name": r_attrs.get("short_name", ""),
                "long_name": r_attrs.get("long_name", ""),
                "color": r_attrs.get("color", ""),
                "text_color": r_attrs.get("text_color", ""),
                "type": r_attrs.get("type"),
            }

    # Group predictions by stop
    by_stop = {sid: [] for sid in stop_ids}
    for p in payload.get("data", []):
        attrs = p.get("attributes", {})
        rels = p.get("relationships", {})
        stop_rel = rels.get("stop", {}).get("data", {})
        route_rel = rels.get("route", {}).get("data", {})
        sid = stop_rel.get("id", "")
        rid = route_rel.get("id", "")

        arrival = attrs.get("arrival_time")
        departure = attrs.get("departure_time")
        status = attrs.get("status")
        direction = attrs.get("direction_id")

        route_info = routes.get(rid, {})

        pred = {
            "route_id": rid,
            "route_name": route_info.get("short_name") or route_info.get("long_name") or rid,
            "route_color": route_info.get("color", ""),
            "route_text_color": route_info.get("text_color", ""),
            "route_type": route_info.get("type"),
            "direction_id": direction,
            "arrival_time": arrival,
            "departure_time": departure,
            "status": status,
        }

        if sid in by_stop:
            if len(by_stop[sid]) < limit_per_stop:
                by_stop[sid].append(pred)

    return by_stop


# ---------------------------------------------------------------------------
# API route
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/nearby")
def api_nearby():
    """
    Main endpoint. Query params: lat, lon, radius (optional).
    Returns nearby stops with walk ETAs and predictions.
    """
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
        status = e.response.status_code if e.response else 500
        if status == 429:
            return jsonify({"error": "MBTA rate limit hit. Try again shortly."}), 429
        return jsonify({"error": f"MBTA stops API error ({status})"}), 502
    except Exception as e:
        log.error("Stops fetch failed: %s", e)
        return jsonify({"error": "Failed to fetch nearby stops"}), 502

    if not stops:
        return jsonify({"stops": [], "updated_at": now.isoformat(), "message": "No stops found nearby. Try a larger radius."})

    # 2. Walk ETAs
    for s in stops:
        dist = haversine_m(lat, lon, s["lat"], s["lon"])
        s["distance_m"] = round(dist)

        # Try ORS first, fallback to simple estimate
        ors_eta = walk_eta_ors(lon, lat, s["lon"], s["lat"])
        if ors_eta is not None:
            s["walk_seconds"] = ors_eta
            s["walk_method"] = "openrouteservice"
        else:
            s["walk_seconds"] = walk_eta_fallback(dist)
            s["walk_method"] = "estimate"

        s["walk_minutes"] = round(s["walk_seconds"] / 60, 1)

    # 3. Predictions
    stop_ids = [s["stop_id"] for s in stops]
    try:
        preds_by_stop = fetch_predictions(stop_ids)
    except requests.HTTPError as e:
        log.warning("Predictions fetch failed: %s", e)
        preds_by_stop = {sid: [] for sid in stop_ids}
    except Exception as e:
        log.warning("Predictions fetch failed: %s", e)
        preds_by_stop = {sid: [] for sid in stop_ids}

    # 4. Compute "leave-by" for each prediction
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

    return jsonify({
        "stops": stops,
        "updated_at": now.isoformat(),
        "origin": {"lat": lat, "lon": lon},
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)