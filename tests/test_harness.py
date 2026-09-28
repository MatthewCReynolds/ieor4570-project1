"""Harness tests that never touch Gemini: LiteLLM is mocked, tools run for real (pure ones only).

Run:  uv run --with pytest --with httpx pytest -q
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import tools  # noqa: E402


def _reply(content=None, tool_calls=None):
    """Shape of litellm's choices[0].message, as far as the harness uses it."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    msg.model_dump = lambda: {"role": "assistant", "content": content, "tool_calls": tool_calls}
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tool_call(name, args, call_id="call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_chat_runs_tool_and_keeps_shape():
    scripted = [
        _reply(tool_calls=[_tool_call("recommend_tire_pressure", {"rider_weight": 170, "tire_width_mm": 28})]),
        _reply(content="Run about 70 psi rear, 58 front."),
    ]
    with patch.object(app_module.litellm, "completion", side_effect=scripted) as mocked:
        client = TestClient(app_module.app)
        r = client.post("/chat", json={"message": "What pressure for 28s at 170 lb?"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"response", "session_id", "tool_calls"}
    assert body["response"].startswith("Run about")
    assert len(body["tool_calls"]) == 1
    call = body["tool_calls"][0]
    assert call["name"] == "recommend_tire_pressure"
    assert call["args"] == {"rider_weight": 170, "tire_width_mm": 28}
    assert json.loads(call["result"])["rear_psi"] > json.loads(call["result"])["front_psi"]
    # The tool result went back to the model as a tool message (the list is shared and
    # mutated in place, so look for the tool message rather than the last element).
    sent = mocked.call_args_list[1].kwargs["messages"]
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_1"
    assert json.loads(tool_msgs[0]["content"])["rear_psi"] == json.loads(call["result"])["rear_psi"]


def test_index_page_is_the_custom_frontend():
    client = TestClient(app_module.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "Domestique" in r.text and "tool call" in r.text


def test_sessions_are_separate_and_remembered():
    with patch.object(app_module.litellm, "completion", side_effect=lambda **kw: _reply(content=f"{len(kw['messages'])} msgs")):
        client = TestClient(app_module.app)
        a1 = client.post("/chat", json={"message": "hi"}).json()
        b1 = client.post("/chat", json={"message": "hi"}).json()
        a2 = client.post("/chat", json={"message": "again", "session_id": a1["session_id"]}).json()
    assert a1["session_id"] != b1["session_id"]
    assert a1["response"] == "2 msgs" and b1["response"] == "2 msgs"   # system + user
    assert a2["response"] == "4 msgs"                                   # system + user + assistant + user
    assert a2["session_id"] == a1["session_id"]


def test_model_failure_is_reported_not_500():
    with patch.object(app_module.litellm, "completion", side_effect=RuntimeError("no credentials")):
        client = TestClient(app_module.app)
        r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 200
    assert r.json()["response"].startswith("Model call failed: RuntimeError")


def test_clear_and_gpx_and_health():
    client = TestClient(app_module.app)
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/clear", params={"session_id": "nope"}).json() == {"status": "ok"}
    assert client.get("/route/doesnotexist.gpx").status_code == 404
    tools.ROUTES["abc12345"] = {"start": {"name": "A"}, "end": {"name": "B"}, "coords": [[-73.96, 40.80, 30.0], [-73.95, 40.81, 35.0]]}
    r = client.get("/route/abc12345.gpx")
    assert r.status_code == 200 and "<trkpt" in r.text and "<ele>30.0</ele>" in r.text
    assert r.headers["content-type"].startswith("application/gpx+xml")


def test_unknown_tool_and_bad_args_do_not_crash():
    assert "error" in json.loads(tools.run_tool("nope", {}))
    assert "hint" in json.loads(tools.run_tool("plan_route", {"bogus": 1}))
    assert "error" in json.loads(tools.run_tool("recommend_tire_pressure", {"rider_weight": 170, "tire_width_mm": 500}))


def test_physics_is_sane():
    flat = tools._speed_for_power(150, 0, 0, 84, 0.005)
    climb = tools._speed_for_power(150, 8, 0, 84, 0.005)
    head = tools._speed_for_power(150, 0, 5, 84, 0.005)
    assert 7 < flat < 10          # ~17-20 mph at 150 W on the flat
    assert 2 < climb < 3.5        # ~5-7 mph up an 8% grade
    assert head < flat            # a headwind slows you down
    assert tools._speed_for_power(150, -10, 0, 84, 0.005) == tools.MAX_DESCENT_MS
