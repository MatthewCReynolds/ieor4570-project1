"""The tools Domestique can run, and the JSON that describes them to the model.

Design rules (from the lecture on tool writing):
  * Every tool returns a JSON *string*. The model reads it, so it must be self-describing.
  * Failures never raise out of a tool. They return {"error": ..., "hint": ...} where the
    hint tells the model what to do next (fix an argument, call another tool first, ...).
  * Argument names and descriptions in TOOLS say exactly what the tool expects.

Tools
  get_ride_forecast      Open-Meteo hourly forecast, scored for cycling.        (external API)
  plan_route             BRouter bike routing + OSM surface breakdown.          (external API)
  analyze_headwind       Wind vs. route bearing, hour by hour. ORIGINAL (mcr2235)
  find_refuel_stops      Water, bike shops, cafes along the route via Overpass. (external API)
  recommend_tire_pressure Berto 15%-drop tire pressure model.   ORIGINAL (rw3024)
  estimate_ride_effort   Physics model: time, speed, kJ, fueling for a route.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --- External services (all free, none need an API key) ---

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
BROUTER_URL = "https://brouter.de/brouter"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "domestique-ride-planner/1.0 (IEOR 4570 student project)"}

# Routes planned in this process, keyed by route_id, so later tools (headwind, refuel
# stops, effort, GPX download) can reuse the geometry without re-routing.
ROUTES: "OrderedDict[str, dict]" = OrderedDict()
MAX_CACHED_ROUTES = 200


class ToolError(Exception):
    """A failure the model should hear about. `hint` says what to try next."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def _error(message: str, hint: str | None = None) -> str:
    payload = {"error": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload)


def _get_json(url: str, params: dict | None = None, timeout: int = 12, what: str = "service"):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    except requests.Timeout:
        raise ToolError(f"{what} timed out after {timeout}s.", "Try again once, or simplify the request.")
    except requests.RequestException as e:
        raise ToolError(f"{what} request failed: {e}", "Try again; if it keeps failing tell the rider the service is down.")
    if r.status_code == 429:
        raise ToolError(f"{what} is rate limiting us (HTTP 429).", "Wait a moment and retry, or ask for a smaller area.")
    if r.status_code >= 400:
        raise ToolError(f"{what} returned HTTP {r.status_code}: {r.text[:200]}", "Check the arguments and retry.")
    try:
        return r.json()
    except ValueError:
        raise ToolError(f"{what} returned something that is not JSON: {r.text[:200]}")


# --- Geography helpers ---

EARTH_R = 6371000.0


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(deg: float) -> str:
    return COMPASS[int((deg + 11.25) // 22.5) % 16]


_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _geocode(place: str) -> dict:
    """Turn a place name (or 'lat,lon') into {name, lat, lon}. Nominatim handles landmarks
    and addresses; Open-Meteo's geocoder is the fallback for plain city names."""
    if not place or not str(place).strip():
        raise ToolError("Empty place name.", "Pass a city, address, or landmark such as 'Nyack, NY'.")
    place = str(place).strip()
    m = _LATLON.match(place)
    if m:
        return {"name": place, "lat": float(m.group(1)), "lon": float(m.group(2))}

    try:
        hits = _get_json(NOMINATIM_URL, {"q": place, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                         timeout=8, what="Geocoder (Nominatim)")
        if hits:
            h = hits[0]
            addr = h.get("address", {})
            town = next((addr[k] for k in ("city", "town", "village", "hamlet", "municipality", "county") if addr.get(k)), None)
            parts = [h.get("name") or place.split(",")[0], town, addr.get("state")]
            label = ", ".join(dict.fromkeys(p for p in parts if p))  # dedupe, keep order
            return {"name": label, "lat": float(h["lat"]), "lon": float(h["lon"])}
    except ToolError:
        pass  # fall through to the second geocoder

    data = _get_json(GEOCODE_URL, {"name": place, "count": 1}, timeout=8, what="Geocoder (Open-Meteo)")
    if not data.get("results"):
        raise ToolError(f"Could not find a place called '{place}'.",
                        "Try a more specific name, add the state or country (e.g. 'Nyack, NY'), or give 'lat,lon'.")
    h = data["results"][0]
    label = ", ".join(x for x in [h.get("name"), h.get("admin1"), h.get("country_code", "").upper()] if x)
    return {"name": label, "lat": h["latitude"], "lon": h["longitude"]}


# --- Date helpers ---

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _resolve_date(text: str | None, tz_name: str) -> date:
    """'today', 'tomorrow', a weekday name, or YYYY-MM-DD, interpreted in the ride's timezone."""
    today = datetime.now(ZoneInfo(tz_name)).date()
    if text is None or str(text).strip() == "":
        return today
    t = str(text).strip().lower().replace("this ", "").replace("next ", "")
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t in ("day after tomorrow", "overmorrow"):
        return today + timedelta(days=2)
    if t in WEEKDAYS:
        delta = (WEEKDAYS.index(t) - today.weekday()) % 7
        return today + timedelta(days=delta)
    try:
        return date.fromisoformat(t)
    except ValueError:
        raise ToolError(f"Could not understand the date '{text}'.",
                        "Use 'today', 'tomorrow', a weekday name, or YYYY-MM-DD.")


# --- Weather ---

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}

HOURLY_VARS = ("temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
               "wind_speed_10m,wind_gusts_10m,wind_direction_10m,weather_code")


def _fetch_hourly(lat: float, lon: float, days: int = 16) -> dict:
    return _get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon, "hourly": HOURLY_VARS, "daily": "sunrise,sunset",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
        "timezone": "auto", "forecast_days": days,
    }, timeout=12, what="Open-Meteo forecast")


def _hour_row(fc: dict, i: int) -> dict:
    h = fc["hourly"]
    code = h["weather_code"][i]
    return {
        "time": h["time"][i][11:16],
        "temp_f": h["temperature_2m"][i],
        "feels_like_f": h["apparent_temperature"][i],
        "precip_prob_pct": h["precipitation_probability"][i],
        "precip_in": h["precipitation"][i],
        "wind_mph": h["wind_speed_10m"][i],
        "gust_mph": h["wind_gusts_10m"][i],
        "wind_from": _compass(h["wind_direction_10m"][i]),
        "wind_from_deg": h["wind_direction_10m"][i],
        "sky": WMO.get(code, f"code {code}"),
    }


def _ride_score(row: dict) -> int:
    """0-100 cycling comfort score for one hour. Simple, transparent penalties."""
    score = 100.0
    score -= 0.5 * (row["precip_prob_pct"] or 0)
    score -= 1.5 * max(0.0, (row["wind_mph"] or 0) - 5)
    score -= 1.0 * abs((row["feels_like_f"] or 65) - 65)
    sky = row["sky"]
    if "thunder" in sky:
        score -= 40
    elif "rain" in sky or "showers" in sky or "snow" in sky or "freezing" in sky:
        score -= 20
    elif "drizzle" in sky or "fog" in sky:
        score -= 10
    return int(max(0, min(100, round(score))))


def _verdict(score: int) -> str:
    if score >= 80:
        return "great"
    if score >= 60:
        return "good"
    if score >= 40:
        return "marginal"
    return "poor"


def get_ride_forecast(location: str | None = None, date: str | None = None, start_hour: int | None = None,
                      duration_hours: float = 2, route_id: str | None = None) -> str:
    """Hourly forecast for a ride window, scored for cycling, plus the best window that day."""
    if route_id:
        route = ROUTES.get(route_id)
        if not route:
            return _error(f"Unknown route_id '{route_id}'.", "Call plan_route first, or pass a location instead.")
        place = {"name": route["start"]["name"], "lat": route["start"]["lat"], "lon": route["start"]["lon"]}
    elif location:
        place = _geocode(location)
    else:
        return _error("Need a location or a route_id.", "Pass location='Nyack, NY' or the route_id from plan_route.")

    fc = _fetch_hourly(place["lat"], place["lon"])
    tz = fc.get("timezone", "UTC")
    day = _resolve_date(date, tz)
    day_iso = day.isoformat()
    times = fc["hourly"]["time"]
    idx = [i for i, t in enumerate(times) if t.startswith(day_iso)]
    if not idx:
        return _error(f"No forecast available for {day_iso}.", "Open-Meteo covers today through 15 days out. Pick a closer date.")

    try:
        d = fc["daily"]["time"].index(day_iso)
        sunrise, sunset = fc["daily"]["sunrise"][d][11:16], fc["daily"]["sunset"][d][11:16]
    except (ValueError, KeyError, IndexError):
        sunrise, sunset = None, None
    sunrise_h = int(sunrise[:2]) if sunrise else 6
    sunset_h = int(sunset[:2]) + (1 if sunset and int(sunset[3:5]) > 0 else 0) if sunset else 19

    try:
        duration_hours = float(duration_hours or 2)
    except (TypeError, ValueError):
        return _error("duration_hours must be a number of hours, e.g. 2.5")
    n_hours = max(1, int(math.ceil(duration_hours)))

    rows = [_hour_row(fc, i) for i in idx]
    for r in rows:
        r["ride_score"] = _ride_score(r)

    # Best start hour: highest mean score over a daylight window of the requested length.
    candidates = []
    for s in range(sunrise_h, max(sunrise_h + 1, sunset_h - n_hours + 1)):
        window = rows[s:s + n_hours]
        if window:
            candidates.append((sum(r["ride_score"] for r in window) / len(window), s))
    best_score, best_start = max(candidates) if candidates else (None, None)

    out = {
        "location": place["name"], "date": day_iso, "timezone": tz,
        "sunrise": sunrise, "sunset": sunset,
        "best_window": None if best_start is None else {
            "start": f"{best_start:02d}:00", "end": f"{min(23, best_start + n_hours):02d}:00",
            "avg_ride_score": round(best_score), "verdict": _verdict(round(best_score)),
        },
    }

    if start_hour is not None:
        try:
            s = int(start_hour)
        except (TypeError, ValueError):
            return _error("start_hour must be an integer hour 0-23 in local time, e.g. 7 for 7 AM.")
        if not 0 <= s <= 23:
            return _error("start_hour must be between 0 and 23.")
        window = rows[s:s + n_hours]
        avg = round(sum(r["ride_score"] for r in window) / len(window))
        end_h = s + duration_hours
        out["requested_window"] = {
            "start": f"{s:02d}:00", "duration_hours": duration_hours, "avg_ride_score": avg, "verdict": _verdict(avg),
            "temp_range_f": [min(r["temp_f"] for r in window), max(r["temp_f"] for r in window)],
            "max_precip_prob_pct": max(r["precip_prob_pct"] or 0 for r in window),
            "max_wind_mph": max(r["wind_mph"] for r in window),
            "max_gust_mph": max(r["gust_mph"] for r in window),
            "finishes_after_sunset": bool(sunset and end_h > int(sunset[:2]) + int(sunset[3:5]) / 60),
            "hours": window,
        }
    else:
        out["daylight_hours"] = rows[sunrise_h:sunset_h + 1]
    return json.dumps(out)


# --- Routing ---

PROFILES = {"road": "fastbike", "fast": "fastbike", "quiet": "fastbike-lowtraffic", "mixed": "trekking",
            "trekking": "trekking", "gravel": "trekking", "safe": "safety", "safety": "safety", "shortest": "shortest"}

PAVED = {"asphalt", "paved", "concrete", "paving_stones", "concrete:plates", "concrete:lanes", "chipseal", "sett"}
UNPAVED = {"unpaved", "gravel", "fine_gravel", "compacted", "dirt", "ground", "earth", "grass", "sand", "mud",
           "wood", "pebblestone", "cobblestone", "unhewn_cobblestone", "rock"}
PAVED_HIGHWAYS = {"primary", "secondary", "tertiary", "residential", "unclassified", "living_street", "service",
                  "primary_link", "secondary_link", "tertiary_link", "trunk", "trunk_link", "cycleway"}


def _classify_surface(way_tags: str) -> tuple[str, str]:
    tags = dict(t.split("=", 1) for t in way_tags.split() if "=" in t)
    surface, highway = tags.get("surface", ""), tags.get("highway", "")
    if surface in PAVED:
        s = "paved"
    elif surface in UNPAVED:
        s = "unpaved"
    elif highway in PAVED_HIGHWAYS:
        s = "paved"
    elif highway in ("track", "path", "bridleway"):
        s = "unpaved"
    else:
        s = "unknown"
    if highway == "cycleway" or tags.get("cycleway") in ("track", "lane") or tags.get("bicycle") == "designated":
        k = "bike path or lane"
    elif highway in ("primary", "secondary", "trunk", "primary_link", "secondary_link", "trunk_link"):
        k = "busy road"
    elif highway in ("path", "track", "footway", "bridleway"):
        k = "path or trail"
    else:
        k = "quiet road"
    return s, k


def _build_segments(coords: list, target_m: float = 800.0) -> tuple[list, list, float, float, float]:
    """Walk the polyline. Returns (segments, profile, total_m, ascent_m, descent_m).
    Each segment: {km, dist_m, bearing, grade_pct}. Profile: [[km, elev_m], ...] ~60 points."""
    if len(coords) < 2:
        raise ToolError("Route has fewer than two points.")
    cum = [0.0]
    for (lon1, lat1, *_), (lon2, lat2, *_) in zip(coords, coords[1:]):
        cum.append(cum[-1] + _haversine_m(lat1, lon1, lat2, lon2))
    total = cum[-1]
    elevs = [c[2] if len(c) > 2 and c[2] is not None else 0.0 for c in coords]

    # Smooth elevation a little before summing ascent, so GPS/DEM noise does not inflate it.
    sm = [sum(elevs[max(0, i - 2):i + 3]) / len(elevs[max(0, i - 2):i + 3]) for i in range(len(elevs))]
    ascent = sum(max(0.0, b - a) for a, b in zip(sm, sm[1:]))
    descent = sum(max(0.0, a - b) for a, b in zip(sm, sm[1:]))

    segments, i, n = [], 0, len(coords)
    while i < n - 1:
        j = i + 1
        while j < n - 1 and cum[j] - cum[i] < target_m:
            j += 1
        dist = cum[j] - cum[i]
        if dist <= 0:
            i = j
            continue
        grade = 100.0 * (sm[j] - sm[i]) / dist
        segments.append({"km": round(cum[i] / 1000, 2), "dist_m": round(dist), "bearing": round(_bearing_deg(coords[i][1], coords[i][0], coords[j][1], coords[j][0])),
                         "grade_pct": round(grade, 1)})
        i = j

    step = max(1, len(coords) // 60)
    profile = [[round(cum[k] / 1000, 2), round(sm[k])] for k in range(0, n, step)]
    if profile[-1][0] != round(total / 1000, 2):
        profile.append([round(total / 1000, 2), round(sm[-1])])
    return segments, profile, total, ascent, descent


def _remember_route(route: dict) -> None:
    ROUTES[route["route_id"]] = route
    while len(ROUTES) > MAX_CACHED_ROUTES:
        ROUTES.popitem(last=False)


def plan_route(start: str, end: str, style: str = "road", via: str | None = None) -> str:
    """Bike route from BRouter with distance, climbing, max grade, surface mix and elevation profile."""
    style_key = (style or "road").strip().lower()
    if style_key not in PROFILES:
        return _error(f"Unknown style '{style}'.", f"Use one of: {', '.join(sorted(set(PROFILES)))}.")
    profile = PROFILES[style_key]

    try:
        a = _geocode(start)
        b = _geocode(end)
        stops = [a] + ([_geocode(via)] if via else []) + [b]
    except ToolError as e:
        return _error(str(e), e.hint)

    straight_km = sum(_haversine_m(p["lat"], p["lon"], q["lat"], q["lon"]) for p, q in zip(stops, stops[1:])) / 1000
    if straight_km > 250:
        return _error(f"Endpoints are {straight_km:.0f} km apart as the crow flies; too far for a single day's bike routing here.",
                      "Split the trip into legs under ~250 km and plan each leg.")
    if straight_km < 0.05:
        return _error("Start and end are the same place.", "Give two different places, or add a 'via' point for a loop.")

    lonlats = "|".join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in stops)
    try:
        gj = _get_json(BROUTER_URL, {"lonlats": lonlats, "profile": profile, "alternativeidx": 0, "format": "geojson"},
                       timeout=40, what="BRouter routing")
    except ToolError as e:
        return _error(str(e), e.hint or "Try again, try a nearer end point, or use style='mixed'.")
    feats = gj.get("features") or []
    if not feats:
        return _error("BRouter returned no route between those points.",
                      "Check the places are on land and reachable by bike; try style='mixed'.")
    props = feats[0].get("properties", {})
    coords = feats[0]["geometry"]["coordinates"]

    try:
        segments, profile_pts, total_m, ascent_m, descent_m = _build_segments(coords)
    except ToolError as e:
        return _error(str(e))

    # Surface / road-type breakdown from BRouter's per-way messages (distance weighted).
    surface_m, kind_m = {"paved": 0.0, "unpaved": 0.0, "unknown": 0.0}, {}
    msgs = props.get("messages") or []
    if len(msgs) > 1:
        head = msgs[0]
        try:
            di, ti = head.index("Distance"), head.index("WayTags")
            for row in msgs[1:]:
                dist = float(row[di] or 0)
                s, k = _classify_surface(row[ti] or "")
                surface_m[s] += dist
                kind_m[k] = kind_m.get(k, 0.0) + dist
        except (ValueError, IndexError):
            pass
    known = surface_m["paved"] + surface_m["unpaved"] + surface_m["unknown"]

    grades = [s["grade_pct"] for s in segments]
    steepest = max(segments, key=lambda s: s["grade_pct"]) if segments else None
    route_id = uuid.uuid4().hex[:8]
    route = {
        "route_id": route_id, "start": a, "end": b, "via": stops[1:-1], "style": style_key, "profile": profile,
        "coords": coords, "segments": segments, "elevation_profile": profile_pts,
        "distance_m": total_m, "ascent_m": ascent_m, "descent_m": descent_m,
        "brouter_time_s": float(props.get("total-time") or 0),
        "surface_pct": {k: round(100 * v / known) for k, v in surface_m.items()} if known else {},
    }
    _remember_route(route)

    mid = coords[len(coords) // 2]
    return json.dumps({
        "route_id": route_id,
        "start": a["name"], "end": b["name"], "via": [p["name"] for p in stops[1:-1]], "style": style_key,
        "distance_mi": round(total_m / 1609.344, 1), "distance_km": round(total_m / 1000, 1),
        "ascent_ft": round(ascent_m * 3.28084), "ascent_m": round(ascent_m),
        "descent_ft": round(descent_m * 3.28084),
        "climbing_ft_per_mi": round(ascent_m * 3.28084 / max(0.1, total_m / 1609.344)),
        "max_grade_pct": max(grades) if grades else None,
        "steepest_at_mi": round(steepest["km"] / 1.609344, 1) if steepest else None,
        "avg_grade_pct": round(sum(g for g in grades if g > 0) / max(1, sum(1 for g in grades if g > 0)), 1) if grades else None,
        "brouter_estimated_time_min": round(route["brouter_time_s"] / 60) if route["brouter_time_s"] else None,
        "surface_pct": route["surface_pct"],
        "road_type_pct": {k: round(100 * v / known) for k, v in sorted(kind_m.items(), key=lambda kv: -kv[1])} if known else {},
        "overall_bearing": _compass(_bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"])),
        "elevation_profile_km_m": profile_pts,
        "midpoint": {"lat": round(mid[1], 4), "lon": round(mid[0], 4)},
        "gpx_download": f"/route/{route_id}.gpx",
        "map_link": f"https://brouter.de/brouter-web/#map=11/{mid[1]:.4f}/{mid[0]:.4f}/standard&lonlats={lonlats.replace('|', ';')}&profile={profile}",
    })


# --- Physics: how fast does a rider go at a given power? ---

RHO = 1.226          # kg/m^3 air density at sea level
G = 9.81
CDA = 0.34           # m^2, hands on the hoods
DRIVETRAIN_EFF = 0.97
CRR = {"smooth_pavement": 0.004, "pavement": 0.005, "rough_pavement": 0.0065, "mixed": 0.008, "gravel": 0.011}
MAX_DESCENT_MS = 15.6   # ~35 mph: riders brake beyond this
MIN_SPEED_MS = 0.9      # walking the bike


def _speed_for_power(power_w: float, grade_pct: float, headwind_ms: float, mass_kg: float, crr: float) -> float:
    theta = math.atan(grade_pct / 100.0)

    def demand(v: float) -> float:
        air = v + headwind_ms
        return (v * mass_kg * G * (crr * math.cos(theta) + math.sin(theta)) + 0.5 * RHO * CDA * air * abs(air) * v) / DRIVETRAIN_EFF

    lo, hi = MIN_SPEED_MS, MAX_DESCENT_MS
    if demand(lo) > power_w:
        return lo
    if demand(hi) <= power_w:
        return hi
    for _ in range(50):
        mid = (lo + hi) / 2
        if demand(mid) > power_w:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _ride_time_s(segments: list, power_w: float, mass_kg: float, crr: float, headwind_by_segment=None) -> float:
    total = 0.0
    for i, s in enumerate(segments):
        hw = headwind_by_segment[i] if headwind_by_segment else 0.0
        total += s["dist_m"] / _speed_for_power(power_w, s["grade_pct"], hw, mass_kg, crr)
    return total


def _to_kg(weight: float, unit: str) -> float:
    return float(weight) * (0.45359237 if unit.lower().startswith("lb") else 1.0)


# --- ORIGINAL TOOL 1 (mcr2235): headwind analysis ---

def analyze_headwind(route_id: str, date: str | None = None, start_hour: int | None = None,
                     rider_power_w: float = 150, rider_weight_lb: float = 165) -> str:
    """Combine the route's segment bearings with the hourly wind forecast at three points along it.
    Reports headwind/tailwind/crosswind exposure, the time cost, and whether to ride it in reverse."""
    route = ROUTES.get(route_id)
    if not route:
        return _error(f"Unknown route_id '{route_id}'.", "Call plan_route first and pass the route_id it returns.")
    try:
        power = float(rider_power_w or 150)
        mass = _to_kg(rider_weight_lb or 165, "lb") + 9.0
    except (TypeError, ValueError):
        return _error("rider_power_w and rider_weight_lb must be numbers.")

    coords = route["coords"]
    samples = [coords[0], coords[len(coords) // 2], coords[-1]]
    fc_list = _get_json(FORECAST_URL, {
        "latitude": ",".join(f"{c[1]:.4f}" for c in samples), "longitude": ",".join(f"{c[0]:.4f}" for c in samples),
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m", "wind_speed_unit": "mph",
        "timezone": "auto", "forecast_days": 16,
    }, timeout=12, what="Open-Meteo wind forecast")
    if isinstance(fc_list, dict):
        fc_list = [fc_list]
    tz = fc_list[0].get("timezone", "UTC")
    day = _resolve_date(date, tz)
    if start_hour is None:
        now = datetime.now(ZoneInfo(tz))
        start_hour = now.hour + 1 if day == now.date() else 9
    try:
        start_hour = int(start_hour)
    except (TypeError, ValueError):
        return _error("start_hour must be an integer hour 0-23 in local time.")
    day_idx = [i for i, t in enumerate(fc_list[0]["hourly"]["time"]) if t.startswith(day.isoformat())]
    if not day_idx:
        return _error(f"No wind forecast for {day.isoformat()}.", "Pick a date within the next 15 days.")
    base = day_idx[0]

    def wind_at(sample_i: int, hour_offset: float) -> tuple[float, float, float]:
        h = fc_list[sample_i]["hourly"]
        i = min(len(h["time"]) - 1, base + start_hour + int(hour_offset))
        return h["wind_speed_10m"][i], h["wind_gusts_10m"][i], h["wind_direction_10m"][i]

    segs = route["segments"]
    total_m = route["distance_m"]
    # First pass at calm speed to place each segment in time; then look up wind for that hour.
    calm_speed = total_m / max(1.0, _ride_time_s(segs, power, mass, CRR["pavement"]))
    elapsed, head_by_seg, cross_by_seg, rows = 0.0, [], [], []
    for s in segs:
        frac = s["km"] * 1000 / max(1.0, total_m)
        sample_i = 0 if frac < 0.33 else (1 if frac < 0.67 else 2)
        ws, gust, wdir = wind_at(sample_i, elapsed / 3600)
        rel = math.radians(wdir - s["bearing"])
        head = ws * math.cos(rel)            # + = headwind, - = tailwind (mph)
        cross = abs(ws * math.sin(rel))
        head_by_seg.append(head * 0.44704)   # m/s for the physics
        cross_by_seg.append(cross)
        rows.append({"km": s["km"], "dist_m": s["dist_m"], "bearing": s["bearing"], "wind_mph": ws, "gust_mph": gust,
                     "wind_from_deg": wdir, "head_mph": round(head, 1), "cross_mph": round(cross, 1)})
        elapsed += s["dist_m"] / calm_speed

    def wsum(key, pred=lambda x: True):
        return sum(r["dist_m"] for r in rows if pred(r[key]))

    avg_head = sum(r["head_mph"] * r["dist_m"] for r in rows) / total_m
    t_calm = _ride_time_s(segs, power, mass, CRR["pavement"])
    t_wind = _ride_time_s(segs, power, mass, CRR["pavement"], head_by_seg)
    t_rev = _ride_time_s(list(reversed([{**s, "grade_pct": -s["grade_pct"]} for s in segs])), power, mass, CRR["pavement"],
                         [-h for h in reversed(head_by_seg)])
    worst = max(rows, key=lambda r: r["head_mph"])
    gustiest = max(rows, key=lambda r: r["gust_mph"])
    max_cross = max(rows, key=lambda r: r["cross_mph"])

    # Compress the per-segment rows to ~24 for the model and the UI chart.
    step = max(1, len(rows) // 24)
    exposure = [{"mi": round(r["km"] / 1.609344, 1), "head_mph": r["head_mph"], "cross_mph": r["cross_mph"]} for r in rows[::step]]

    penalty_min = (t_wind - t_calm) / 60
    if avg_head > 3:
        summary = "net headwind"
    elif avg_head < -3:
        summary = "net tailwind"
    else:
        summary = "roughly neutral wind"
    return json.dumps({
        "route_id": route_id, "date": day.isoformat(), "start_time": f"{start_hour:02d}:00", "timezone": tz,
        "summary": summary,
        "avg_wind_mph": round(sum(r["wind_mph"] * r["dist_m"] for r in rows) / total_m, 1),
        "prevailing_wind_from": _compass(sum(r["wind_from_deg"] * r["dist_m"] for r in rows) / total_m),
        "avg_headwind_component_mph": round(avg_head, 1),
        "pct_distance_headwind": round(100 * wsum("head_mph", lambda h: h > 3) / total_m),
        "pct_distance_tailwind": round(100 * wsum("head_mph", lambda h: h < -3) / total_m),
        "pct_distance_crosswind_over_10mph": round(100 * wsum("cross_mph", lambda c: c > 10) / total_m),
        "worst_headwind": {"at_mi": round(worst["km"] / 1.609344, 1), "head_mph": worst["head_mph"], "heading": _compass(worst["bearing"])},
        "strongest_crosswind": {"at_mi": round(max_cross["km"] / 1.609344, 1), "cross_mph": max_cross["cross_mph"]},
        "max_gust_mph": gustiest["gust_mph"],
        "time_estimate_min": {"calm": round(t_calm / 60), "with_wind_as_planned": round(t_wind / 60), "reversed_direction": round(t_rev / 60)},
        "wind_time_penalty_min": round(penalty_min),
        "recommend_reverse": bool(t_rev < t_wind - 120),
        "assumptions": {"rider_power_w": power, "total_mass_kg": round(mass), "wind_sampled_at": "start, midpoint, end"},
        "exposure_by_mile": exposure,
    })


# --- Refuel stops along the route (Overpass / OpenStreetMap) ---

STOP_KINDS = {
    "water": ('["amenity"="drinking_water"]', "drinking water"),
    "bike_shop": ('["shop"="bicycle"]', "bike shop"),
    "repair_station": ('["amenity"="bicycle_repair_station"]', "public repair stand"),
    "cafe": ('["amenity"="cafe"]', "cafe"),
    "convenience": ('["shop"="convenience"]', "convenience store"),
    "toilets": ('["amenity"="toilets"]', "restroom"),
}


def find_refuel_stops(route_id: str | None = None, location: str | None = None, kinds: list[str] | None = None,
                      corridor_m: int = 250) -> str:
    """Water fountains, bike shops, cafes, restrooms within `corridor_m` of a planned route (or near a place)."""
    kinds = kinds or ["water", "bike_shop", "cafe"]
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",")]
    bad = [k for k in kinds if k not in STOP_KINDS]
    if bad:
        return _error(f"Unknown stop kind(s): {bad}.", f"Use any of: {', '.join(STOP_KINDS)}.")
    try:
        corridor_m = int(corridor_m or 250)
    except (TypeError, ValueError):
        return _error("corridor_m must be an integer number of meters.")
    corridor_m = max(50, min(corridor_m, 3000))

    if route_id:
        route = ROUTES.get(route_id)
        if not route:
            return _error(f"Unknown route_id '{route_id}'.", "Call plan_route first, or pass a location instead.")
        coords = route["coords"]
        cum, pts = 0.0, [(coords[0][1], coords[0][0], 0.0)]
        for (lon1, lat1, *_), (lon2, lat2, *_) in zip(coords, coords[1:]):
            cum += _haversine_m(lat1, lon1, lat2, lon2)
            if cum - pts[-1][2] >= 600:
                pts.append((lat2, lon2, cum))
        if len(pts) > 180:                      # keep the Overpass query short
            stride = math.ceil(len(pts) / 180)
            pts = pts[::stride]
        area = f"around:{corridor_m}," + ",".join(f"{la:.5f},{lo:.5f}" for la, lo, _ in pts)
        label = f"within {corridor_m} m of route {route_id}"
    elif location:
        try:
            p = _geocode(location)
        except ToolError as e:
            return _error(str(e), e.hint)
        radius = max(corridor_m, 1500)
        pts = [(p["lat"], p["lon"], 0.0)]
        area = f"around:{radius},{p['lat']:.5f},{p['lon']:.5f}"
        label = f"within {radius} m of {p['name']}"
    else:
        return _error("Need a route_id or a location.", "Pass the route_id from plan_route, or a place name.")

    union = "".join(f"nwr{STOP_KINDS[k][0]}({area});" for k in kinds)
    query = f"[out:json][timeout:25];({union});out center tags 80;"
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=35)
    except requests.RequestException as e:
        return _error(f"Overpass (OpenStreetMap) request failed: {e}", "Retry once; if it fails again tell the rider to check the map manually.")
    if r.status_code == 429 or r.status_code == 504:
        return _error(f"Overpass is busy (HTTP {r.status_code}).", "Wait a few seconds and retry, or narrow kinds/corridor.")
    if r.status_code >= 400:
        return _error(f"Overpass returned HTTP {r.status_code}: {r.text[:200]}")
    try:
        elements = r.json().get("elements", [])
    except ValueError:
        return _error("Overpass returned non-JSON output.", "Retry once.")

    tag_to_kind = {STOP_KINDS[k][0]: k for k in kinds}
    stops = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = (el.get("lat"), el.get("lon")) if "lat" in el else (el.get("center", {}).get("lat"), el.get("center", {}).get("lon"))
        if lat is None:
            continue
        kind = next((k for k in kinds if _matches(tags, k)), "other")
        nearest = min(pts, key=lambda q: _haversine_m(lat, lon, q[0], q[1]))
        stops.append({
            "kind": STOP_KINDS.get(kind, ("", kind))[1], "name": tags.get("name") or "(unnamed)",
            "at_mi": round(nearest[2] / 1609.344, 1), "lat": round(lat, 5), "lon": round(lon, 5),
            "hours": tags.get("opening_hours"), "note": tags.get("description") or tags.get("bottle") and "bottle refill" or None,
        })
    stops.sort(key=lambda s: (s["at_mi"], s["kind"]))
    counts = {}
    for s in stops:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    # Keep the payload readable for the model: drop empty fields, collapse duplicates at the
    # same mile (chains, fountain clusters), and cap each kind so one busy block cannot crowd out the rest.
    seen, per_kind, compact = set(), {}, []
    for s in stops:
        key = (s["kind"], s["name"], s["at_mi"])
        if key in seen or per_kind.get(s["kind"], 0) >= 15:
            continue
        seen.add(key)
        per_kind[s["kind"]] = per_kind.get(s["kind"], 0) + 1
        compact.append({k: v for k, v in s.items() if v is not None})
    return json.dumps({
        "search": label, "kinds_requested": kinds, "counts": counts, "total": len(stops),
        "stops": compact,
        "note": "OpenStreetMap data; unnamed drinking_water nodes are usually park fountains. Verify hours before relying on a shop." if stops else
                "Nothing found. Widen corridor_m, add kinds like 'convenience' or 'toilets', or plan to carry more water.",
    })


def _matches(tags: dict, kind: str) -> bool:
    key, val = STOP_KINDS[kind][0].strip("[]").replace('"', "").split("=")
    return tags.get(key) == val


# --- ORIGINAL TOOL 2 (rw3024): tire pressure ---

SURFACE_ADJ = {"smooth_pavement": 0.0, "pavement": -0.03, "rough_pavement": -0.08, "mixed": -0.12, "gravel": -0.18}


def recommend_tire_pressure(rider_weight: float, tire_width_mm: float, bike_weight: float = 20, weight_unit: str = "lb",
                            surface: str = "pavement", tubeless: bool = False, hookless_rim: bool = False,
                            max_sidewall_psi: float | None = None, front_weight_pct: float = 45) -> str:
    """Front and rear tire pressure from the rider+bike load per wheel and tire width, using a
    power-law fit of Frank Berto's 15%-tire-drop data (psi ~= 305 * wheel_load_lb / width_mm^1.8),
    then adjusted for surface, tubeless, hookless rims and the tire's sidewall maximum."""
    try:
        total_lb = _to_kg(rider_weight, weight_unit) / 0.45359237 + _to_kg(bike_weight or 20, weight_unit) / 0.45359237
        width = float(tire_width_mm)
        front_pct = float(front_weight_pct or 45)
    except (TypeError, ValueError):
        return _error("rider_weight, bike_weight, tire_width_mm and front_weight_pct must be numbers.")
    if not 18 <= width <= 80:
        return _error(f"tire_width_mm={width} is outside the model's range.", "Use a width between 18 and 80 mm (road 25-32, gravel 38-50, MTB 55-65).")
    if not 40 <= total_lb <= 450:
        return _error("Combined rider + bike weight looks wrong.", "Check weight_unit ('lb' or 'kg') and the values.")
    if not 30 <= front_pct <= 60:
        return _error("front_weight_pct should be 30-60 (road ~45, upright/touring ~40).")
    surface = (surface or "pavement").lower()
    if surface not in SURFACE_ADJ:
        return _error(f"Unknown surface '{surface}'.", f"Use one of: {', '.join(SURFACE_ADJ)}.")

    def berto(load_lb: float) -> float:
        return 305.0 * load_lb / (width ** 1.8)

    front, rear = berto(total_lb * front_pct / 100), berto(total_lb * (100 - front_pct) / 100)
    adj = SURFACE_ADJ[surface] + (-0.08 if tubeless else 0.0)
    front, rear = front * (1 + adj), rear * (1 + adj)

    caps, notes = [], []
    if hookless_rim:
        caps.append(("hookless rim limit (ETRTO)", 72.5))
    if max_sidewall_psi:
        try:
            caps.append(("tire sidewall maximum", float(max_sidewall_psi)))
        except (TypeError, ValueError):
            return _error("max_sidewall_psi must be a number.")
    floor = 15.0 if width >= 45 else 25.0
    for name, cap in caps:
        if rear > cap or front > cap:
            notes.append(f"Capped at {cap:.0f} psi by the {name}; the model wanted more, so expect a slightly harsher ride.")
        front, rear = min(front, cap), min(rear, cap)
    if front < floor or rear < floor:
        notes.append(f"Raised to a {floor:.0f} psi floor to avoid rim strikes; consider a wider tire for this load.")
        front, rear = max(front, floor), max(rear, floor)
    if tubeless:
        notes.append("Tubeless: 8% lower than a tubed setup because pinch flats are not a concern.")
    if surface != "smooth_pavement":
        notes.append(f"Surface '{surface}': {abs(SURFACE_ADJ[surface]) * 100:.0f}% lower than glass-smooth pavement for grip and comfort.")
    if width <= 25 and total_lb > 220:
        notes.append("Heavy rider on a narrow tire: a 28-32 mm tire would be faster and more comfortable at sane pressures.")

    return json.dumps({
        "front_psi": round(front), "rear_psi": round(rear),
        "front_bar": round(front / 14.5038, 2), "rear_bar": round(rear / 14.5038, 2),
        "inputs": {"total_weight_lb": round(total_lb), "tire_width_mm": width, "surface": surface, "tubeless": bool(tubeless),
                   "hookless_rim": bool(hookless_rim), "front_weight_pct": front_pct},
        "method": "Berto 15% tire-drop fit: psi = 305 * load_lb / width_mm^1.8, per wheel",
        "notes": notes,
        "check": "Start here, then adjust +/-3 psi by feel. Never exceed the lower of the tire's and rim's printed maximums.",
    })


# --- Effort estimate (physics) ---

POWER_LEVELS = {"casual": 100, "fitness": 150, "strong": 200, "racer": 260}


def estimate_ride_effort(route_id: str | None = None, distance_mi: float | None = None, ascent_ft: float | None = None,
                         rider_power_w: float | None = None, rider_level: str | None = None,
                         rider_weight_lb: float = 165, bike_weight_lb: float = 20,
                         surface: str = "pavement", headwind_mph: float = 0) -> str:
    """Moving time, average speed, work in kJ and a fueling plan for a route (or a distance+climbing pair)
    at a steady power, using a rolling-resistance + aero + gravity power balance per segment."""
    if rider_power_w is None:
        rider_power_w = POWER_LEVELS.get((rider_level or "fitness").lower())
        if rider_power_w is None:
            return _error(f"Unknown rider_level '{rider_level}'.", f"Use one of {list(POWER_LEVELS)} or give rider_power_w directly.")
    try:
        power = float(rider_power_w)
        mass = _to_kg(rider_weight_lb or 165, "lb") + _to_kg(bike_weight_lb or 20, "lb")
        headwind_ms = float(headwind_mph or 0) * 0.44704
    except (TypeError, ValueError):
        return _error("rider_power_w, rider_weight_lb, bike_weight_lb and headwind_mph must be numbers.")
    if not 40 <= power <= 500:
        return _error("rider_power_w should be between 40 and 500 W (casual 100, fitness 150, strong 200, racer 260).")
    surface = (surface or "pavement").lower()
    if surface not in CRR:
        return _error(f"Unknown surface '{surface}'.", f"Use one of: {', '.join(CRR)}.")

    if route_id:
        route = ROUTES.get(route_id)
        if not route:
            return _error(f"Unknown route_id '{route_id}'.", "Call plan_route first, or pass distance_mi and ascent_ft instead.")
        segments, dist_m, ascent_m, source = route["segments"], route["distance_m"], route["ascent_m"], f"route {route_id}"
        if route["surface_pct"].get("unpaved", 0) > 30 and surface == "pavement":
            surface = "mixed"
    else:
        if distance_mi is None:
            return _error("Need a route_id or a distance_mi (and ascent_ft).")
        try:
            dist_m = float(distance_mi) * 1609.344
            ascent_m = float(ascent_ft or 0) / 3.28084
        except (TypeError, ValueError):
            return _error("distance_mi and ascent_ft must be numbers.")
        # Synthetic profile: climbs at 5%, matching descents, flat for the rest.
        climb_m = ascent_m / 0.05
        flat_m = max(0.0, dist_m - 2 * climb_m)
        segments = [{"dist_m": climb_m, "grade_pct": 5.0}, {"dist_m": flat_m, "grade_pct": 0.0}, {"dist_m": climb_m, "grade_pct": -5.0}]
        segments = [s for s in segments if s["dist_m"] > 0]
        source = "distance + climbing (synthetic 5% climbs)"

    t = _ride_time_s(segments, power, mass, CRR[surface], [headwind_ms] * len(segments))
    hours = t / 3600
    kj = power * t / 1000
    kcal = kj / 4.184 / 0.24                     # ~24% gross efficiency
    if hours < 1.25:
        carbs = "water is enough; 20-30 g carbs/h if you go hard"
    elif hours < 2.5:
        carbs = "30-60 g carbs/h (a bar or gel every 30-40 min)"
    else:
        carbs = "60-90 g carbs/h from mixed glucose/fructose; start eating in the first 30 min"
    fluid_oz_h = 20 if power < 180 else 24
    return json.dumps({
        "source": source, "distance_mi": round(dist_m / 1609.344, 1), "ascent_ft": round(ascent_m * 3.28084),
        "assumed_power_w": power, "total_mass_lb": round(mass / 0.45359237), "surface": surface, "headwind_mph": round(headwind_ms / 0.44704, 1),
        "moving_time_min": round(t / 60), "moving_time_hm": f"{int(hours)}h{int(round((hours % 1) * 60)):02d}",
        "avg_speed_mph": round(dist_m / 1609.344 / hours, 1),
        "work_kj": round(kj), "calories_kcal": round(kcal),
        "fueling": {"carbs": carbs, "fluid_oz_per_hour": fluid_oz_h, "bottles_needed": math.ceil(hours * fluid_oz_h / 24)},
        "method": "Per-segment power balance: rolling + aero (CdA 0.34) + gravity, 3% drivetrain loss, descents capped at 35 mph",
    })


# --- What the model sees ---

TOOLS = [
    {"type": "function", "function": {
        "name": "get_ride_forecast",
        "description": ("Hourly cycling forecast for a place and day: temperature, feels-like, rain chance, wind speed/gusts/direction, "
                        "sunrise/sunset, and a 0-100 ride score per hour. Also returns the best start window of the day. "
                        "Call this for any question about whether/when to ride, weather, wind, rain, or daylight."),
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": "City, town or landmark, e.g. 'Nyack, NY' or 'Central Park'. Omit if you pass route_id."},
            "route_id": {"type": "string", "description": "Optional. A route_id from plan_route; the forecast is taken at the route's start."},
            "date": {"type": "string", "description": "'today' (default), 'tomorrow', a weekday name like 'saturday', or YYYY-MM-DD. Up to 15 days ahead."},
            "start_hour": {"type": "integer", "description": "Local start hour 0-23 (7 = 7 AM). Omit to get every daylight hour and a best-window suggestion."},
            "duration_hours": {"type": "number", "description": "Ride length in hours, default 2. Used to size the window and the best-window search."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "plan_route",
        "description": ("Plan a bike route between two places using BRouter (OpenStreetMap routing). Returns a route_id for other tools, "
                        "distance, total climbing, max and average grade, paved vs unpaved share, road-type mix, an elevation profile, "
                        "a GPX download path and a map link. Call this whenever the rider names a start and a destination."),
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "Start place: address, landmark or town, e.g. 'Columbia University, New York'."},
            "end": {"type": "string", "description": "Destination, e.g. 'Nyack, NY'. For a loop, use the same start and end and give a 'via'."},
            "via": {"type": "string", "description": "Optional intermediate place to route through (makes loops possible)."},
            "style": {"type": "string", "enum": ["road", "quiet", "mixed", "gravel", "safe", "shortest"],
                      "description": "Routing style. 'road' = fast paved (default), 'quiet' = paved but low traffic, 'mixed'/'gravel' = allows unpaved, 'safe' = maximizes bike paths, 'shortest'."},
        }, "required": ["start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "analyze_headwind",
        "description": ("ORIGINAL. Cross the route's direction of travel, mile by mile, with the hourly wind forecast at the start, middle and end "
                        "of the route at the time the rider will be there. Returns headwind/tailwind/crosswind exposure, the worst mile, "
                        "the minutes the wind costs, the time if ridden in reverse, and whether reversing the loop is better. "
                        "Requires a route_id from plan_route. Use for 'which direction should I ride', 'how bad is the wind', 'when is the wind lightest'."),
        "parameters": {"type": "object", "properties": {
            "route_id": {"type": "string", "description": "The route_id returned by plan_route."},
            "date": {"type": "string", "description": "'today' (default), 'tomorrow', a weekday name, or YYYY-MM-DD."},
            "start_hour": {"type": "integer", "description": "Local departure hour 0-23. Defaults to next hour today, else 9."},
            "rider_power_w": {"type": "number", "description": "Steady power in watts (casual 100, fitness 150 default, strong 200, racer 260)."},
            "rider_weight_lb": {"type": "number", "description": "Rider weight in pounds, default 165."},
        }, "required": ["route_id"]},
    }},
    {"type": "function", "function": {
        "name": "find_refuel_stops",
        "description": ("Find drinking-water fountains, bike shops, public repair stands, cafes, convenience stores and restrooms along a planned "
                        "route (within a corridor) or near a place, from OpenStreetMap via Overpass. Results carry the mile marker along the route. "
                        "Use for 'where can I refill', 'is there a bike shop on the way', 'coffee stop'."),
        "parameters": {"type": "object", "properties": {
            "route_id": {"type": "string", "description": "A route_id from plan_route. Searches a corridor along the whole route."},
            "location": {"type": "string", "description": "Alternative to route_id: a place to search around (1.5 km radius)."},
            "kinds": {"type": "array", "items": {"type": "string", "enum": list(STOP_KINDS)},
                      "description": "Which stops to look for. Default ['water', 'bike_shop', 'cafe']."},
            "corridor_m": {"type": "integer", "description": "How far off the route to look, in meters (50-3000). Default 250."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "recommend_tire_pressure",
        "description": ("ORIGINAL. Recommend front and rear tire pressure (psi and bar) from rider weight, bike weight, tire width, surface, "
                        "tubeless and hookless-rim status, using a fit of Berto's 15% tire-drop data with safety caps. "
                        "Use whenever the rider asks what pressure to run, or before a gravel/rough ride."),
        "parameters": {"type": "object", "properties": {
            "rider_weight": {"type": "number", "description": "Rider weight (in weight_unit)."},
            "tire_width_mm": {"type": "number", "description": "Tire width in mm, 18-80. Road 25-32, gravel 38-50."},
            "bike_weight": {"type": "number", "description": "Bike + gear weight (in weight_unit). Default 20 lb."},
            "weight_unit": {"type": "string", "enum": ["lb", "kg"], "description": "Unit for both weights. Default 'lb'."},
            "surface": {"type": "string", "enum": list(SURFACE_ADJ), "description": "Riding surface. Default 'pavement'."},
            "tubeless": {"type": "boolean", "description": "True if the tires are tubeless. Default false."},
            "hookless_rim": {"type": "boolean", "description": "True if the rims are hookless (caps pressure at 72.5 psi). Default false."},
            "max_sidewall_psi": {"type": "number", "description": "Optional max pressure printed on the tire; the result never exceeds it."},
            "front_weight_pct": {"type": "number", "description": "Share of weight on the front wheel, 30-60. Road ~45 (default), upright ~40."},
        }, "required": ["rider_weight", "tire_width_mm"]},
    }},
    {"type": "function", "function": {
        "name": "estimate_ride_effort",
        "description": ("Estimate moving time, average speed, work (kJ), calories and a fueling plan (carbs per hour, bottles) for a planned route "
                        "or for a distance + climbing pair, using a physics power model. Use for 'how long will it take', 'how much should I eat', "
                        "'can I do this before dark'."),
        "parameters": {"type": "object", "properties": {
            "route_id": {"type": "string", "description": "A route_id from plan_route (preferred: uses the real grade profile)."},
            "distance_mi": {"type": "number", "description": "If no route_id: distance in miles."},
            "ascent_ft": {"type": "number", "description": "If no route_id: total climbing in feet."},
            "rider_power_w": {"type": "number", "description": "Steady power in watts. Overrides rider_level."},
            "rider_level": {"type": "string", "enum": list(POWER_LEVELS), "description": "casual 100 W, fitness 150 W (default), strong 200 W, racer 260 W."},
            "rider_weight_lb": {"type": "number", "description": "Rider weight in pounds, default 165."},
            "bike_weight_lb": {"type": "number", "description": "Bike + gear weight in pounds, default 20."},
            "surface": {"type": "string", "enum": list(CRR), "description": "Rolling surface. Default 'pavement'."},
            "headwind_mph": {"type": "number", "description": "Average headwind in mph (negative for tailwind). Default 0; get a real value from analyze_headwind."},
        }, "required": []},
    }},
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "get_ride_forecast": get_ride_forecast,
    "plan_route": plan_route,
    "analyze_headwind": analyze_headwind,
    "find_refuel_stops": find_refuel_stops,
    "recommend_tire_pressure": recommend_tire_pressure,
    "estimate_ride_effort": estimate_ride_effort,
}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return _error(f"Unknown tool '{name}'.", f"Available tools: {list(TOOL_MAP)}.")
    try:
        return TOOL_MAP[name](**args)
    except TypeError as e:
        return _error(f"Bad arguments for {name}: {e}", "Check the parameter names against the tool description and retry.")
    except ToolError as e:
        return _error(str(e), e.hint)
    except Exception as e:  # a bug in a tool should reach the model as text, not kill the request
        return _error(f"{name} failed unexpectedly: {type(e).__name__}: {str(e)[:200]}", "Try once more or answer without this tool.")
