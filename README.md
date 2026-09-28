# Domestique: a tool-calling cycling ride planner

**Live agent:** https://DEPLOY-URL-PENDING.run.app
**Team:** Matthew Reynolds (mcr2235), Rachel (rw3024)
**Course:** IEOR 4570, Project 1

A *domestique* is the rider on a cycling team who looks after the leader: checks the wind, fetches
bottles, knows the road. This agent does that for you. Tell it where you want to ride and when, and
it plans the route, reads the wind hour by hour along your direction of travel, finds water and coffee
on the way, sets your tire pressure, and tells you how long you will be out and what to eat.

Built on the class starter (`gemini-web-tool-calling`): the same LiteLLM harness loop, the same
in-memory session store, and the same `/chat` response shape (`response`, `session_id`, `tool_calls`).
Everything else, the six tools, the physics, the system prompt and the frontend, is ours.

## Sample queries for grading

1. **Plan a road ride from Columbia University, New York to Nyack, NY and tell me how long it takes at 170 W.**
   Calls `plan_route` then `estimate_ride_effort`. Expect ~24 mi, ~1,000 ft of climbing, a paved/road-type
   breakdown, an elevation profile in the tool card, a GPX download, and a moving time near 1h15.

2. **Which direction should I ride a loop from Boulder, CO via Lyons, CO on Saturday at 9am to avoid the headwind?**
   Calls `plan_route` (with `via`) then `analyze_headwind`. Expect a headwind/tailwind chart by mile,
   the minutes the wind costs, the reversed-direction time, and a clear "ride it as planned / reverse it".

3. **What tire pressure should a 170 lb rider run on 32 mm tubeless tires for gravel?**
   Calls `recommend_tire_pressure`. Expect front/rear psi and bar (roughly 40/48 psi) with the reasoning.

Follow-ups that show memory, in the same session: *"Where can I refill water and grab coffee on that route?"*
(`find_refuel_stops` reuses the route it already planned) and *"Is 8am tomorrow better than 3pm?"*
(`get_ride_forecast`). Open the app in two browser windows to see that sessions stay separate.

## Tools

| Tool | What it does | Data | Original? |
|---|---|---|---|
| `plan_route` | Bike route between two places (optionally via a third). Distance, climbing, max/avg grade, paved vs unpaved share, road-type mix, elevation profile, GPX download, map link. | [BRouter](https://brouter.de) routing over OpenStreetMap, geocoding via Nominatim / Open-Meteo | |
| `get_ride_forecast` | Hourly temperature, feels-like, rain chance, wind speed/gust/direction, sunrise/sunset for a ride window, each hour scored 0-100 for cycling, plus the best window of the day. | [Open-Meteo](https://open-meteo.com) | |
| `analyze_headwind` | Crosses the route's bearing, segment by segment, with the hourly wind forecast at the start, middle and end of the route at the time you will be there. Reports headwind / tailwind / crosswind exposure, the worst mile, the minutes the wind costs, and whether to ride the loop in reverse. | Open-Meteo wind + the planned route | **Yes (mcr2235)** |
| `find_refuel_stops` | Drinking-water fountains, bike shops, public repair stands, cafes, convenience stores and restrooms within a corridor of the route, each tagged with its mile marker. | [Overpass API](https://overpass-api.de) (OpenStreetMap) | |
| `recommend_tire_pressure` | Front and rear pressure from rider + bike weight, tire width, surface, tubeless and hookless-rim status. Power-law fit of Frank Berto's 15% tire-drop data (`psi = 305 * wheel_load_lb / width_mm^1.8`) with safety caps. | none (model) | **Yes (rw3024)** |
| `estimate_ride_effort` | Moving time, average speed, work in kJ, calories and a fueling plan from a per-segment power balance (rolling resistance + aerodynamics + gravity) on the real grade profile. | none (physics) | |

All six tools follow the lecture's rules: descriptive names and argument docs, JSON results, and errors
returned as `{"error": ..., "hint": ...}` so the model can fix its call or explain the failure
instead of crashing the loop. `plan_route` returns a `route_id` the other tools take, so the agent
chains route -> headwind -> effort -> stops without re-routing.

## Frontend

`index.html` is a single page with no build step. The sidebar explains what the agent is, lists the
tools, and offers clickable sample prompts. Every tool call appears inline as a card between your
message and the answer, with a visual summary (elevation profile, headwind bars by mile, an hourly
forecast strip with wind arrows, a stop table, pressure tiles) and the raw args + result underneath.
A planned route can be downloaded as GPX for a bike computer.

## Run locally

```bash
gcloud auth application-default login      # the app uses Vertex AI through your gcloud default project
export GOOGLE_CLOUD_PROJECT=your-project-id
uv run app.py                              # http://localhost:8000
```

Optional: `MODEL=vertex_ai/gemini-3.5-flash` to use a bigger model; `PORT` and `HOST` are honored.

Tests (LiteLLM is mocked, tools run for real): `uv run --with pytest --with httpx pytest -q`

## Deploy

Continuous deployment from this repo to Cloud Run: Cloud Run -> Create service -> *Continuously deploy
from a repository* -> this repo, branch `main`, build type **Dockerfile**. The Dockerfile installs from
`uv.lock` and starts the app on `$PORT`. Set the `GOOGLE_CLOUD_PROJECT` environment variable on the
service and give its service account the *Vertex AI User* role. Run **one instance** (max instances = 1)
because sessions are in memory, exactly like the starter.

## Layout

```
app.py          FastAPI app, LiteLLM harness loop, session store, /chat, /clear, /route/{id}.gpx, /health
tools.py        the six tools, their JSON schemas, and run_tool()
index.html      the frontend
Dockerfile      what Cloud Run builds
tests/          harness tests with a mocked model
submission.json deploy URL and authors
```
