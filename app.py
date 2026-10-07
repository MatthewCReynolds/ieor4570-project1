"""Domestique: a cycling ride-planner agent.

Same harness as the class starter (gemini-web-tool-calling): LiteLLM completion loop
that runs tools until the model answers, an in-memory session store, and a /chat
endpoint that returns {response, session_id, tool_calls}. Added on top:

  * a system prompt that knows today's date, so "tomorrow" and "saturday" work
  * GET /route/{route_id}.gpx so a planned route can be loaded on a bike computer
  * GET /health for Cloud Run
  * host/port from the environment, which Cloud Run requires
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import route_insights  # noqa: F401  (registers assess_route_safety and analyze_sun_exposure into TOOLS)
from tools import ROUTES, TOOLS, run_tool

# --- Config ---

MODEL = os.environ.get("MODEL", "vertex_ai/gemini-3.5-flash-lite")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
VERTEX_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("VERTEXAI_PROJECT") or None
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are Domestique, a cycling ride-planning assistant. A domestique is the rider who \
looks after the team leader: checks the wind, fetches bottles, knows the road. Do that for the user.

You have tools. Use them instead of guessing:
- plan_route whenever the rider names a start and a destination. It returns a route_id that the other tools take.
- get_ride_forecast for anything about weather, wind, rain, temperature, daylight, or "when should I ride".
- analyze_headwind after plan_route when wind, direction, "which way round", or time-of-day matters.
- find_refuel_stops for water, bike shops, cafes, restrooms or "what's along the way".
- recommend_tire_pressure for any tire pressure question.
- estimate_ride_effort for how long a ride takes, pace, calories, or fueling.

Chain tools when the question needs it (route -> headwind -> effort, for example), and you may call several \
tools in one turn. If a tool returns an error, read its hint, fix the call once, and otherwise tell the rider \
plainly what failed and what you could still work out.

Defaults: if the rider gives no weight, power, or bike details, assume a 165 lb rider, 20 lb bike, 150 W \
steady power, and say so in one short clause. Use miles, feet, mph and °F unless the rider uses metric. \
Do not ask clarifying questions when a sensible default exists; ask only when the answer would be useless without it.

Answer like a knowledgeable riding partner: lead with the decision (go / don't go / ride it clockwise / 80 psi rear), \
then the two or three numbers that justify it, then anything to watch out for. Use short paragraphs or a compact \
bulleted list. Never dump raw JSON or mention route_ids; the interface already shows the tool calls. \
Today's date (UTC) is {today}."""

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        kwargs = {"model": MODEL, "messages": messages, "tools": TOOLS}
        if MODEL.startswith("vertex_ai/"):
            kwargs["vertex_location"] = VERTEX_LOCATION
            if VERTEX_PROJECT:
                kwargs["vertex_project"] = VERTEX_PROJECT
        reply = litellm.completion(**kwargs).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content or "", tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing. Try a narrower question.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process (Cloud Run: run 1 instance).
sessions: dict[str, list] = {}
MAX_SESSIONS = 500


def new_session() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    return [{"role": "system", "content": SYSTEM_PROMPT.format(today=today)}]


# --- FastAPI App ---

app = FastAPI(title="Domestique")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "sessions": len(sessions)}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        if len(sessions) >= MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))
        sessions[session_id] = new_session()

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        first_line = str(e).strip().splitlines()[0] if str(e).strip() else ""
        response, tool_calls = f"Model call failed: {type(e).__name__}: {first_line[:300]}", []
        sessions[session_id].pop()  # drop the user turn so the context is not left half-finished

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


@app.get("/route/{route_id}.gpx")
def route_gpx(route_id: str):
    """The planned route as GPX, for a Garmin/Wahoo or a phone app."""
    route = ROUTES.get(route_id)
    if not route:
        raise HTTPException(404, "Unknown route_id; routes live in memory only while the server runs.")
    name = f"{route['start']['name']} to {route['end']['name']}"
    pts = "\n".join(
        f'      <trkpt lat="{c[1]:.6f}" lon="{c[0]:.6f}">' + (f"<ele>{c[2]:.1f}</ele>" if len(c) > 2 and c[2] is not None else "") + "</trkpt>"
        for c in route["coords"]
    )
    gpx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="Domestique" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <trk><name>{name}</name><trkseg>\n{pts}\n  </trkseg></trk>\n</gpx>\n"
    )
    return Response(gpx, media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="domestique-{route_id}.gpx"'})


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
