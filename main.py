"""FastAPI entry point.

Exposes:
  - REST endpoints for control (tasks, commands, failure simulation, reset)
  - A single /ws WebSocket that streams the world state at simulation rate

The simulation runs in a background asyncio task started on FastAPI's
`startup` event and gracefully stopped on shutdown.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .llm_parser import parse_command
from .models import (
    CommandRequest,
    CreateTaskRequest,
    ObstacleRequest,
    ParsedIntent,
    Priority,
    WorldState,
)
from .simulation import SimulationEngine
from .websocket_manager import ConnectionManager

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("api")

# ── Globals (lifespan-scoped) ──────────────────────────────────────────────
ws_manager = ConnectionManager()
engine: SimulationEngine | None = None


# ── Lifespan ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    num_robots = int(os.getenv("NUM_ROBOTS", "4"))
    engine = SimulationEngine(ws=ws_manager, num_robots=num_robots)
    await engine.start()
    log.info("Simulation engine started.")
    try:
        yield
    finally:
        if engine is not None:
            await engine.stop()
        log.info("Simulation engine stopped.")


app = FastAPI(
    title="Warehouse AI",
    description="LLM-Orchestrated Multi-Agent Autonomous Warehouse Robotics System",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — the dashboard runs on a different port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _engine() -> SimulationEngine:
    if engine is None:
        raise HTTPException(503, "Simulation engine not ready.")
    return engine


# ── Health ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "simulation_running": engine is not None and engine._running,
        "clients": ws_manager.client_count,
        "llm_provider": _detect_llm_provider(),
    }


def _detect_llm_provider() -> str:
    forced = os.getenv("LLM_PROVIDER", "auto").lower()
    if forced == "local":
        return "local"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "local"


# ── State ──────────────────────────────────────────────────────────────────
@app.get("/state", response_model=WorldState)
async def get_state() -> WorldState:
    return await _engine().snapshot()


# ── Tasks ──────────────────────────────────────────────────────────────────
@app.post("/tasks")
async def create_task(req: CreateTaskRequest) -> dict[str, object]:
    task = await _engine().create_task(
        dropoff_location=req.dropoff_location,
        pickup_location=req.pickup_location,
        priority=req.priority,
        package_id=req.package_id,
    )
    if task is None:
        raise HTTPException(400, "Invalid pickup or drop-off location.")
    return {"ok": True, "task": task.model_dump(mode="json")}


# ── Natural-language command ───────────────────────────────────────────────
@app.post("/command")
async def command(req: CommandRequest) -> dict[str, object]:
    intent = await parse_command(req.text)
    result = await _apply_intent(intent)
    return {"ok": result["ok"], "intent": intent.model_dump(mode="json"), **result}


async def _apply_intent(intent: ParsedIntent) -> dict[str, object]:
    eng = _engine()

    if intent.action == "create_task":
        if not intent.dropoff_location:
            return {"ok": False, "message": "Couldn't identify a drop-off location."}
        task = await eng.create_task(
            dropoff_location=intent.dropoff_location,
            pickup_location=intent.pickup_location or "storage",
            priority=intent.priority,
            package_id=intent.package_id,
        )
        if task is None:
            return {"ok": False, "message": f"Unknown location: {intent.dropoff_location}."}
        return {"ok": True, "message": intent.message or f"Created task {task.id}.", "task_id": task.id}

    if intent.action == "fail_robot":
        if not intent.robot_id:
            return {"ok": False, "message": "Couldn't identify which robot to fail."}
        if await eng.fail_robot(intent.robot_id):
            return {"ok": True, "message": intent.message or f"Failed {intent.robot_id}."}
        return {"ok": False, "message": f"No such robot: {intent.robot_id}."}

    if intent.action == "recharge_low_battery":
        n = await eng.recharge_low_battery_robots()
        return {"ok": True, "message": f"Dispatched {n} robot(s) to chargers."}

    if intent.action == "reassign_unfinished":
        n = await eng.reassign_unfinished()
        return {"ok": True, "message": f"Reassigned {n} task(s)."}

    if intent.action == "add_obstacle":
        cell = await eng.add_obstacle()
        return {
            "ok": cell is not None,
            "message": f"Obstacle at {cell}." if cell else "No free cell available for an obstacle.",
        }

    if intent.action == "reset":
        await eng.reset()
        return {"ok": True, "message": "World reset."}

    return {"ok": False, "message": intent.message}


# ── Simulation utilities ───────────────────────────────────────────────────
@app.post("/simulate/failure/{robot_id}")
async def simulate_failure(robot_id: str) -> dict[str, object]:
    ok = await _engine().fail_robot(robot_id)
    if not ok:
        raise HTTPException(404, f"No such robot: {robot_id}")
    return {"ok": True}


@app.post("/simulate/revive/{robot_id}")
async def simulate_revive(robot_id: str) -> dict[str, object]:
    ok = await _engine().revive_robot(robot_id)
    if not ok:
        raise HTTPException(404, f"No such robot: {robot_id}")
    return {"ok": True}


@app.post("/simulate/obstacle")
async def simulate_obstacle(req: ObstacleRequest) -> dict[str, object]:
    cell = await _engine().add_obstacle(req.x, req.y)
    if cell is None:
        raise HTTPException(400, "Could not place obstacle at that cell.")
    return {"ok": True, "cell": cell}


@app.post("/reset")
async def reset() -> dict[str, object]:
    await _engine().reset()
    return {"ok": True}


# ── WebSocket ──────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws_manager.connect(ws)
    try:
        # Send an initial snapshot immediately so the dashboard renders
        # something useful before the next tick fires.
        snap = await _engine().snapshot()
        await ws.send_json({"type": "state", "data": snap.model_dump(mode="json")})

        while True:
            # We don't actually need any client→server messages, but
            # `receive_text` is how we detect disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(ws)
    except Exception as e:
        log.warning("WebSocket error: %s", e)
        await ws_manager.disconnect(ws)


# ── Root ───────────────────────────────────────────────────────────────────
@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": "Warehouse AI",
        "version": "0.1.0",
        "docs": "/docs",
        "websocket": "/ws",
    }
