"""Two original route-analysis tools that build on the route plan_route stores in ROUTES.

  assess_route_safety   Level-of-Traffic-Stress scoring from the OSM tags of every way on the route.
  analyze_sun_exposure  Solar position along the route *as you ride it*: glare, darkness, UV.

Same rules as tools.py: every tool returns a JSON string, failures come back as
{"error": ..., "hint": ...} instead of raising, and the descriptions in EXTRA_TOOLS tell the
model when to call each tool and what every argument means.

Importing this module registers both tools into tools.TOOLS and tools.TOOL_MAP (see the bottom of
the file). app.py imports it; tools.py does not import it, so there is no import cycle.
"""

from __future__ import annotations

import bisect
import json
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tools
from tools import (CRR, FORECAST_URL, ROUTES, ToolError, _compass, _error, _get_json,
                   _haversine_m, _resolve_date, _speed_for_power, _to_kg)

# --------------------------------------------------------------------------------------
# Shared geometry helpers
# --------------------------------------------------------------------------------------


def _cum_dist(coords: list) -> list[float]:
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _haversine_m(a[1], a[0], b[1], b[0]))
    return cum


def _point_at(coords: list, cum: list[float], m: float) -> tuple[float, float]:
    """(lat, lon) of the point `m` meters along the route polyline."""
    i = min(max(bisect.bisect_left(cum, m), 1), len(cum) - 1)
    span = (cum[i] - cum[i - 1]) or 1.0
    f = min(1.0, max(0.0, (m - cum[i - 1]) / span))
    a, b = coords[i - 1], coords[i]
    return a[1] + (b[1] - a[1]) * f, a[0] + (b[0] - a[0]) * f


def _mi(meters: float) -> float:
    return round(meters / 1609.344, 1)


# --------------------------------------------------------------------------------------
# Tool 1: assess_route_safety  (Level of Traffic Stress)
# --------------------------------------------------------------------------------------
# Method: Mekuria, Furth & Nixon (2012), "Low-Stress Bicycling and Network Connectivity",
# Mineta Transportation Institute. LTS 1 is comfortable for almost anyone, LTS 4 is only for
# confident riders. This is a simplified version that works from the tags BRouter reports.

KEEP_TAGS = ("highway", "maxspeed", "cycleway", "cycleway:left", "cycleway:right", "cycleway:both",
             "bicycle", "lit", "surface")
OFF_ROAD = {"cycleway", "path", "footway", "pedestrian", "track", "bridleway"}
DEFAULT_SPEED_MPH = {"living_street": 15, "service": 15, "residential": 25, "unclassified": 30, "tertiary": 30,
                     "tertiary_link": 25, "secondary": 35, "secondary_link": 30, "primary": 40, "primary_link": 35,
                     "trunk": 50, "trunk_link": 40, "motorway": 65}
LTS_KEY = {1: "protected_or_path", 2: "calm_street_or_lane", 3: "moderate_stress", 4: "high_stress"}
LTS_WEIGHT = {1: 100, 2: 75, 3: 40, 4: 0}
SHORT_CROSSING_M = 150


compact_way = tools._compact_way   # plan_route stores one of these per way; kept here for reference


def _parse_speed_mph(value: str | None) -> float | None:
    """OSM maxspeed: bare numbers are km/h. Returns None for 'none', 'signals', '50;30', etc."""
    if not value:
        return None
    v = value.replace("_", " ").strip().lower()      # BRouter may write '30 mph' as '30_mph'
    if v == "walk":
        return 4.0
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(mph|km/h|kmh)?$", v)
    if not m:
        return None
    n = float(m.group(1))
    return n if m.group(2) == "mph" else n * 0.621371


def _infra(tags: dict) -> str:
    vals = {tags.get(k) for k in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both")}
    if vals & {"track", "separate", "opposite_track"}:
        return "protected"
    if vals & {"lane", "opposite_lane", "shoulder"}:
        return "lane"
    return "none"


def _lts(tags: dict) -> dict | None:
    """Rate one way. Returns None when the way has no usable highway tag."""
    hw = tags.get("highway", "")
    if not hw:
        return None
    if hw in OFF_ROAD:
        return {"lts": 1, "kind": "off-road path", "hw": hw, "speed": None, "explicit": True, "road": False}
    speed = _parse_speed_mph(tags.get("maxspeed"))
    explicit = speed is not None
    if speed is None:
        speed = DEFAULT_SPEED_MPH.get(hw, 30)
    infra = _infra(tags)
    if infra == "protected":
        return {"lts": 1, "kind": "protected bike track", "hw": hw, "speed": speed, "explicit": explicit, "road": True}
    if infra == "lane":
        lts = 1 if speed <= 25 else 2 if speed <= 35 else 3 if speed <= 45 else 4
        return {"lts": lts, "kind": "painted bike lane", "hw": hw, "speed": speed, "explicit": explicit, "road": True}
    if hw == "living_street" or speed <= 20:
        lts = 1
    elif speed <= 25:
        lts = 2
    elif speed <= 35:
        lts = 3
    else:
        lts = 4
    if hw in ("primary", "primary_link", "trunk", "trunk_link", "motorway") and lts < 3:
        lts = 3                      # arterials are never calm, even when the posted limit is low
    return {"lts": lts, "kind": "mixed traffic", "hw": hw, "speed": speed, "explicit": explicit, "road": True}


def assess_route_safety(route_id: str, comfort: str = "average") -> str:
    """Score a planned route with Level of Traffic Stress and list its worst stretches."""
    route = ROUTES.get(route_id)
    if not route:
        return _error(f"Unknown route_id '{route_id}'.", "Call plan_route first and pass the route_id it returns.")
    comfort = (comfort or "average").strip().lower()
    if comfort not in ("cautious", "average"):
        return _error(f"Unknown comfort level '{comfort}'.", "Use 'cautious' (beginners, kids, nervous riders) or 'average'.")
    ways = route.get("ways") or []
    if not ways:
        return _error("This route has no road-tag data, so its safety cannot be scored.",
                      "Plan the route again with plan_route. If it still fails, tell the rider and use road_type_pct from plan_route instead.")

    flag_at = 3 if comfort == "cautious" else 4
    rated, total_m, rated_m = [], 0.0, 0.0
    dist_by_lts = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    road_m = speed_tagged_m = infra_m = lit_m = 0.0
    signals = 0
    for w in ways:
        d = float(w["dist_m"])
        total_m += d
        tags = w.get("tags") or {}
        signals += 1 if tags.get("signals") else 0
        if tags.get("lit") == "yes":
            lit_m += d
        r = _lts(tags)
        if r is None:
            continue
        rated_m += d
        dist_by_lts[r["lts"]] += d
        if r["road"]:
            road_m += d
            speed_tagged_m += d if r["explicit"] else 0.0
            infra_m += d if _infra(tags) != "none" else 0.0
        rated.append({**r, "start_m": float(w["start_m"]), "dist_m": d})
    if rated_m <= 0:
        return _error("None of the road segments on this route carried usable tags.",
                      "Tell the rider safety could not be assessed for this route.")

    score = round(sum(LTS_WEIGHT[k] * v for k, v in dist_by_lts.items()) / rated_m)
    verdict = ("low stress" if score >= 80 else "mostly comfortable" if score >= 60
               else "mixed, with some stressful stretches" if score >= 40 else "high stress")

    # Merge adjacent ways at or above the flag level into stretches.
    runs, cur = [], None
    for w in rated:
        if w["lts"] >= flag_at:
            if cur and abs(cur["end_m"] - w["start_m"]) < 1.0:
                cur["end_m"] = w["start_m"] + w["dist_m"]
                cur["ways"].append(w)
            else:
                cur = {"start_m": w["start_m"], "end_m": w["start_m"] + w["dist_m"], "ways": [w]}
                runs.append(cur)
        else:
            cur = None
    for r in runs:
        r["len_m"] = r["end_m"] - r["start_m"]
        r["max_lts"] = max(w["lts"] for w in r["ways"])
        r["main"] = max(r["ways"], key=lambda w: w["dist_m"])
    big = sorted((r for r in runs if r["len_m"] >= SHORT_CROSSING_M), key=lambda r: (-r["max_lts"], -r["len_m"]))
    cum = _cum_dist(route["coords"])
    stretches = []
    for r in big[:5]:
        lat, lon = _point_at(route["coords"], cum, (r["start_m"] + r["end_m"]) / 2)
        main = r["main"]
        stretches.append({
            "from_mi": _mi(r["start_m"]), "to_mi": _mi(r["end_m"]), "length_ft": round(r["len_m"] * 3.28084),
            "stress": LTS_KEY[r["max_lts"]], "road": main["hw"], "kind": main["kind"],
            "speed_mph": None if main["speed"] is None else round(main["speed"]),
            "speed_source": "posted" if main["explicit"] else "assumed from road class",
            "lat": round(lat, 5), "lon": round(lon, 5),
        })

    high_pct = round(100 * dist_by_lts[4] / rated_m)
    suggestion = None
    if score < 60 or high_pct >= 15:
        suggestion = ("Re-plan with plan_route using style='safe' (maximizes bike paths) or style='quiet' (paved, low traffic), "
                      "then assess that route and compare scores.")
    notes = ["Stress levels follow the Level of Traffic Stress method (Mekuria, Furth & Nixon 2012), simplified to OSM tags.",
             "Speeds without a posted maxspeed tag are assumed from the road class; OSM bike-lane tagging is incomplete in places."]
    return json.dumps({
        "route_id": route_id, "comfort": comfort, "comfort_score": score, "verdict": verdict,
        "pct_by_stress": {LTS_KEY[k]: round(100 * v / rated_m) for k, v in dist_by_lts.items()},
        "flagged_stretches": stretches,
        "more_flagged_stretches": max(0, len(big) - 5),
        "short_crossings_of_stressful_roads": sum(1 for r in runs if r["len_m"] < SHORT_CROSSING_M),
        "traffic_signals": signals, "signals_per_mile": round(signals / max(0.1, total_m / 1609.344), 1),
        "street_lighting_tagged_pct": round(100 * lit_m / total_m),
        "data_quality": {
            "posted_speed_pct_of_road": round(100 * speed_tagged_m / road_m) if road_m else None,
            "bike_infra_tagged_pct_of_road": round(100 * infra_m / road_m) if road_m else None,
            "untagged_pct_of_route": round(100 * (total_m - rated_m) / total_m),
        },
        "suggestion": suggestion, "notes": notes,
    })


# --------------------------------------------------------------------------------------
# Tool 2: analyze_sun_exposure
# --------------------------------------------------------------------------------------

GLARE_MAX_ELEV = 20.0    # degrees: above this the sun is high enough to sit above most sightlines
GLARE_CONE = 30.0        # degrees either side of the direction of travel
HORIZON = -0.833         # sun's center at sunrise/sunset (refraction + disc radius)


def _solar_position(lat: float, lon: float, when_utc: datetime) -> tuple[float, float]:
    """(elevation_deg, azimuth_deg clockwise from north). Low-precision solar model, good to ~0.1 degree."""
    n = when_utc.timestamp() / 86400.0 + 2440587.5 - 2451545.0     # days since J2000
    mean_lon = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(mean_lon + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (280.46061837 + 360.98564736629 * n) % 360
    ha = math.radians((gmst + lon) % 360) - ra
    phi = math.radians(lat)
    el = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(phi) - math.sin(phi) * math.cos(ha))
    return math.degrees(el), (math.degrees(az) + 360) % 360


def _tz_from_lon(lon: float) -> str:
    """Fallback when the weather service is down: a fixed-offset zone from longitude (no daylight saving)."""
    return f"Etc/GMT{-round(lon / 15):+d}"


def _scan_glare(legs: list[dict], start_utc: datetime) -> list[tuple]:
    out = []
    for leg in legs:
        el, az = _solar_position(leg["lat"], leg["lon"], start_utc + timedelta(seconds=leg["t_mid"]))
        rel = ((az - leg["bearing"] + 540) % 360) - 180             # -180..180, 0 = straight ahead
        out.append((el, az, rel, -0.5 <= el <= GLARE_MAX_ELEV and abs(rel) <= GLARE_CONE))
    return out


def _uv_advice(uv: float) -> str:
    if uv >= 8:
        return "very high: sunscreen, sleeves and a helmet liner; avoid riding at peak hours"
    if uv >= 6:
        return "high: sunscreen on all exposed skin, reapply on a long ride"
    if uv >= 3:
        return "moderate: sunscreen on face, neck and arms"
    return "low: sunscreen optional"


def analyze_sun_exposure(route_id: str, date: str | None = None, start_hour: int | None = None,
                         rider_power_w: float = 150, rider_weight_lb: float = 165) -> str:
    """Where the sun is relative to the rider along a route: glare, darkness, best start time, UV."""
    route = ROUTES.get(route_id)
    if not route:
        return _error(f"Unknown route_id '{route_id}'.", "Call plan_route first and pass the route_id it returns.")
    try:
        power = float(rider_power_w or 150)
        mass = _to_kg(rider_weight_lb or 165, "lb") + 9.0
    except (TypeError, ValueError):
        return _error("rider_power_w and rider_weight_lb must be numbers.")
    if not 40 <= power <= 500:
        return _error("rider_power_w should be between 40 and 500 W (casual 100, fitness 150, strong 200, racer 260).")
    if start_hour is not None:           # check before any network call
        try:
            start_hour = int(start_hour)
        except (TypeError, ValueError):
            return _error("start_hour must be an integer hour 0-23 in local time.", "Use e.g. 7 for 7 AM or 14 for 2 PM.")
        if not 0 <= start_hour <= 23:
            return _error("start_hour must be between 0 and 23.", "Use the local hour, e.g. 7 for 7 AM or 14 for 2 PM.")
    coords, segs = route["coords"], route["segments"]
    if not segs:
        return _error("This route has no segments to analyze.", "Plan the route again with plan_route.")
    lat0, lon0 = coords[0][1], coords[0][0]

    # One small weather call gives the local timezone and the UV forecast. If it fails, the sun
    # math still works: it only needs a timezone, which we approximate from longitude.
    notes, weather = [], None
    try:
        weather = _get_json(FORECAST_URL, {"latitude": f"{lat0:.4f}", "longitude": f"{lon0:.4f}",
                                           "hourly": "uv_index,cloud_cover", "timezone": "auto", "forecast_days": 16},
                            timeout=12, what="Open-Meteo UV forecast")
        tz_name = weather.get("timezone") or _tz_from_lon(lon0)
    except ToolError:
        tz_name = _tz_from_lon(lon0)
        notes.append("Weather service unreachable: timezone approximated from longitude (no daylight saving) and UV unavailable.")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(_tz_from_lon(lon0))
        notes.append("Timezone data missing on the server; local times may be off by an hour.")

    day = _resolve_date(date, tz.key)
    if start_hour is None:
        now = datetime.now(tz)
        start_hour = min(23, now.hour + 1) if day == now.date() else 9

    # When is the rider at each part of the route? Calm-air speed per segment at the given power.
    cum = _cum_dist(coords)
    legs, t = [], 0.0
    for s in segs:
        v = _speed_for_power(power, s["grade_pct"], 0.0, mass, CRR["pavement"])
        dt = s["dist_m"] / v
        lat, lon = _point_at(coords, cum, s["km"] * 1000 + s["dist_m"] / 2)
        legs.append({"mi": s["km"] / 1.609344, "dist_m": s["dist_m"], "dt": dt, "bearing": s["bearing"],
                     "t_mid": t + dt / 2, "lat": lat, "lon": lon})
        t += dt
    ride_s = t
    total_m = sum(leg["dist_m"] for leg in legs)

    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    to_utc = lambda minutes: (midnight + timedelta(minutes=minutes)).astimezone(timezone.utc)  # noqa: E731
    rise = sset = None
    prev = None
    for m in range(0, 1441):
        el, _ = _solar_position(lat0, lon0, to_utc(m))
        if prev is not None:
            if prev < HORIZON <= el and rise is None:
                rise = m
            if prev >= HORIZON > el:
                sset = m
        prev = el
    hhmm = lambda minutes: (midnight + timedelta(minutes=minutes)).strftime("%H:%M")  # noqa: E731

    start_min = start_hour * 60
    start_utc = to_utc(start_min)
    start_local = midnight + timedelta(minutes=start_min)
    finish_local = start_local + timedelta(seconds=ride_s)
    finish_txt = finish_local.strftime("%H:%M") + (" (+1 day)" if finish_local.date() > day else "")

    scan = _scan_glare(legs, start_utc)
    glare_m = sum(leg["dist_m"] for leg, r in zip(legs, scan) if r[3])
    glare_s = sum(leg["dt"] for leg, r in zip(legs, scan) if r[3])
    lights_s = sum(leg["dt"] for leg, r in zip(legs, scan) if r[0] < HORIZON)

    # Consecutive glare legs become stretches with a mile range and the local time you reach them.
    stretches, cur = [], None
    for leg, r in zip(legs, scan):
        if r[3]:
            if cur:
                cur["end_mi"] = leg["mi"] + leg["dist_m"] / 1609.344
                cur["len_m"] += leg["dist_m"]
            else:
                cur = {"start_mi": leg["mi"], "end_mi": leg["mi"] + leg["dist_m"] / 1609.344, "len_m": leg["dist_m"],
                       "at": (start_local + timedelta(seconds=leg["t_mid"])).strftime("%H:%M"),
                       "heading": _compass(leg["bearing"]), "sun_from": _compass(r[1]), "sun_elev": round(r[0])}
                stretches.append(cur)
        else:
            cur = None
    top = sorted(stretches, key=lambda s: -s["len_m"])[:3]
    glare_stretches = [{"from_mi": round(s["start_mi"], 1), "to_mi": round(s["end_mi"], 1), "reached_at": s["at"],
                        "heading": s["heading"], "sun_in_the": s["sun_from"], "sun_elevation_deg": s["sun_elev"]}
                       for s in sorted(top, key=lambda s: s["start_mi"])]

    # Would starting earlier or later cut the glare? Try every half hour of daylight that still finishes by sunset.
    better = None
    if rise is not None and sset is not None:
        latest = sset - math.ceil(ride_s / 60)
        candidates = list(range(((rise + 29) // 30) * 30, latest + 1, 30))
        best = None
        for c in candidates:
            g = sum(leg["dist_m"] for leg, r in zip(legs, _scan_glare(legs, to_utc(c))) if r[3])
            key = (g, abs(c - start_min))
            if best is None or key < best[0]:
                best = (key, c)
        if best and glare_m - best[0][0] >= 0.05 * total_m:
            c = best[1]
            better = {"start": hhmm(c), "glare_pct_distance": round(100 * best[0][0] / total_m),
                      "finishes_about": (midnight + timedelta(minutes=c, seconds=ride_s)).strftime("%H:%M")}

    sun0 = _solar_position(lat0, lon0, start_utc)
    daylight = {"finishes_after_sunset": bool(sset is not None and (finish_local - midnight).total_seconds() / 60 > sset),
                "starts_before_sunrise": bool(rise is not None and start_min < rise),
                "lights_needed_minutes": round(lights_s / 60)}
    if sset is not None:
        daylight["minutes_after_sunset"] = max(0, round((finish_local - midnight).total_seconds() / 60 - sset))
        daylight["latest_start_to_finish_by_sunset"] = hhmm(max(0, sset - math.ceil(ride_s / 60)))
    if rise is None or sset is None:
        notes.append("The sun does not rise or set on this date at this latitude (polar day or night).")

    uv = None
    if weather and "hourly" in weather:
        times, hourly = weather["hourly"]["time"], weather["hourly"]
        idx = [times.index(k) for k in (f"{day.isoformat()}T{h:02d}:00"
                                          for h in range(start_hour, min(23, start_hour + math.ceil(ride_s / 3600)) + 1)) if k in times]
        uvs = [hourly["uv_index"][i] for i in idx if hourly["uv_index"][i] is not None]
        clouds = [hourly["cloud_cover"][i] for i in idx if hourly["cloud_cover"][i] is not None]
        if uvs:
            uv = {"max_uv_index": round(max(uvs), 1), "advice": _uv_advice(max(uvs)),
                  "avg_cloud_cover_pct": round(sum(clouds) / len(clouds)) if clouds else None}
        else:
            notes.append("UV forecast only covers about 15 days ahead.")
    notes.append(f"Glare = sun below {GLARE_MAX_ELEV:.0f} degrees elevation and within {GLARE_CONE:.0f} degrees of your heading; "
                 "heavy cloud cover reduces it.")
    return json.dumps({
        "route_id": route_id, "date": day.isoformat(), "timezone": tz.key,
        "sunrise": hhmm(rise) if rise is not None else None, "sunset": hhmm(sset) if sset is not None else None,
        "start_time": start_local.strftime("%H:%M"), "estimated_finish": finish_txt, "ride_minutes": round(ride_s / 60),
        "daylight": daylight,
        "sun_at_start": {"elevation_deg": round(sun0[0]), "from": _compass(sun0[1])},
        "glare": {"pct_distance": round(100 * glare_m / total_m), "minutes": round(glare_s / 60),
                  "stretches": glare_stretches},
        "better_start": better,
        "uv": uv,
        "assumptions": {"rider_power_w": power, "total_mass_kg": round(mass), "timing": "calm-air speed per segment"},
        "notes": notes,
    })


# --------------------------------------------------------------------------------------
# What the model sees
# --------------------------------------------------------------------------------------

EXTRA_TOOLS = [
    {"type": "function", "function": {
        "name": "assess_route_safety",
        "description": ("ORIGINAL. Score how stressful a planned route is for a cyclist from the OpenStreetMap tags of every road segment "
                        "BRouter chose: road class, posted or assumed speed limit, bike lane or protected track, and traffic signals. "
                        "Uses the Level of Traffic Stress method. Returns a 0-100 comfort score, the share of the route at each stress level, "
                        "the worst stretches with mile markers and coordinates, and the number of traffic signals. "
                        "Requires a route_id from plan_route. Use for 'is this route safe', 'how much traffic is there', "
                        "'is it good for a beginner / kid', or before recommending a route to a nervous rider. "
                        "Not for weather, wind or daylight."),
        "parameters": {"type": "object", "properties": {
            "route_id": {"type": "string", "description": "The route_id returned by plan_route."},
            "comfort": {"type": "string", "enum": ["cautious", "average"],
                        "description": "Rider's comfort with traffic. 'cautious' (beginners, kids, nervous riders) also flags moderate-stress "
                                       "stretches; 'average' (default) flags only high-stress ones."},
        }, "required": ["route_id"]},
    }},
    {"type": "function", "function": {
        "name": "analyze_sun_exposure",
        "description": ("ORIGINAL. Work out where the sun is, in the sky and relative to the rider's heading, at each point along a planned route "
                        "at the moment the rider gets there. Returns sunrise and sunset, whether the ride runs into the dark (and the latest start that "
                        "finishes by sunset), how much of the route has low sun in the rider's eyes with mile markers, a better start time if one "
                        "cuts the glare, and the UV index for the ride window. Requires a route_id from plan_route. "
                        "Use for 'will I ride into the sun', 'do I need lights', 'can I finish before dark', 'sunscreen / UV'. "
                        "For rain, temperature or wind use get_ride_forecast or analyze_headwind instead."),
        "parameters": {"type": "object", "properties": {
            "route_id": {"type": "string", "description": "The route_id returned by plan_route."},
            "date": {"type": "string", "description": "'today' (default), 'tomorrow', a weekday name, or YYYY-MM-DD."},
            "start_hour": {"type": "integer", "description": "Local departure hour 0-23 (7 = 7 AM). Defaults to next hour today, else 9."},
            "rider_power_w": {"type": "number", "description": "Steady power in watts, used to time the ride (casual 100, fitness 150 default, strong 200, racer 260)."},
            "rider_weight_lb": {"type": "number", "description": "Rider weight in pounds, default 165."},
        }, "required": ["route_id"]},
    }},
]

EXTRA_TOOL_MAP = {
    "assess_route_safety": assess_route_safety,
    "analyze_sun_exposure": analyze_sun_exposure,
}


# Register with the harness. Idempotent so re-imports (tests, reloads) do not duplicate tools.
for _t in EXTRA_TOOLS:
    if all(x["function"]["name"] != _t["function"]["name"] for x in tools.TOOLS):
        tools.TOOLS.append(_t)
tools.TOOL_MAP.update(EXTRA_TOOL_MAP)
