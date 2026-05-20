#  Warehouse AI — LLM-Orchestrated Multi-Agent Autonomous Warehouse

> A research-grade prototype of a cooperative multi-robot warehouse, where
> autonomous robots execute deliveries under an LLM-driven coordination
> layer. Runs locally with one Docker command.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Tailwind](https://img.shields.io/badge/TailwindCSS-3-38BDF8?logo=tailwindcss&logoColor=white)](https://tailwindcss.com/)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

---

##  Overview

Warehouse AI simulates a 2D warehouse with **multiple autonomous mobile
robots**. Robots pick up packages from a storage area and deliver them to
one of five drop-off zones while navigating shelves, dynamic obstacles, and
each other. A **central coordinator** assigns tasks using a battery- and
priority-aware cost function. An **LLM parser** turns natural-language
operator commands ("*Deliver package P3 to Zone B urgently*") into
structured task specs. A modern **React command-center UI** streams every
tick over WebSockets.

This project is intentionally MVP-first: it runs as a clean, self-contained
local simulation today, and its module boundaries map cleanly onto a future
ROS 2 + Gazebo upgrade (see [docs/ros2_upgrade_plan.md](docs/ros2_upgrade_plan.md)).

---

##  Features

| Capability                       | Where                                   |
|----------------------------------|------------------------------------------|
|  Multi-robot fleet (4 default) | `backend/app/simulation.py`              |
|  A* path planning              | `backend/app/pathfinding.py`             |
|  LLM command parser            | `backend/app/llm_parser.py`              |
|  Cost-based task assignment    | `backend/app/coordinator.py`             |
|  Battery + auto-charging       | `backend/app/simulation.py`              |
|  Dynamic obstacles & replanning| `backend/app/pathfinding.py`             |
|  Robot failure & reassignment  | `backend/app/simulation.py`              |
|  WebSocket telemetry           | `backend/app/websocket_manager.py`       |
| Live metrics + logs           | `backend/app/metrics.py`                 |
| Dark-mode command-center UI  | `frontend/src/`                          |

---

##  Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          OPERATOR (You)                            │
└──────────────────────────┬─────────────────────────────────────────┘
                           │  natural-language commands
                ┌──────────▼──────────┐
                │   React Dashboard   │  (Vite + Tailwind, dark mode)
                │  ─ Warehouse map    │
                │  ─ Robot panel      │
                │  ─ Command console  │
                │  ─ Task / log view  │
                └──────────┬──────────┘
                           │  REST + WebSocket
                ┌──────────▼──────────┐
                │   FastAPI Backend   │
                │  ┌─────────────┐    │
                │  │ LLM Parser  │ ◀──── Claude / GPT / local rules
                │  ├─────────────┤    │
                │  │ Coordinator │    │   cost-based MRTA
                │  ├─────────────┤    │
                │  │ Simulation  │    │   tick loop @ 4Hz
                │  │   - A*      │    │
                │  │   - Fleet   │    │
                │  │   - Tasks   │    │
                │  └─────────────┘    │
                └─────────────────────┘
```

More detail in [docs/architecture.md](docs/architecture.md).

---

##  Quickstart

### Docker (recommended)

```bash
git clone <this-repo> warehouse_ai && cd warehouse_ai
cp .env.example .env                       # (optional) add an LLM key
docker compose up --build
```

Then open **<http://localhost:5173>**.

The backend takes ~3 seconds to start; the dashboard auto-connects via
WebSocket and begins streaming the world state.

### Native (without Docker)

```bash
# ─── Backend ──────────────────────────────────────────────────────
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# ─── Frontend (separate terminal) ────────────────────────────────
cd frontend
npm install
npm run dev
```

---

##  Example Commands

Type any of these into the dashboard's command console:

1. `Deliver package P3 to Zone B urgently`
2. `Send a robot to Zone A`
3. `Bring item P7 from storage to zone D quickly`
4. `Prioritize urgent package to Zone E`
5. `Recharge low battery robots`
6. `Simulate robot 2 failure`
7. `Reassign unfinished tasks`
8. `Add an obstacle to block an aisle`
9. `Move package to zone C`
10. `Reset everything`

The parser works **offline** out of the box (rule-based). Add an LLM API key
to `.env` for richer language understanding.

---

##  Environment Variables

| Variable               | Default              | Purpose                                                |
|------------------------|----------------------|--------------------------------------------------------|
| `ANTHROPIC_API_KEY`    | (unset)              | Use Anthropic Claude for command parsing               |
| `OPENAI_API_KEY`       | (unset)              | Use OpenAI for command parsing                         |
| `LLM_PROVIDER`         | `auto`               | `auto` / `anthropic` / `openai` / `local`              |
| `ANTHROPIC_MODEL`      | `claude-opus-4-5`    | Override Claude model                                  |
| `OPENAI_MODEL`         | `gpt-4o-mini`        | Override OpenAI model                                  |
| `BACKEND_PORT`         | `8000`               | Host port for the FastAPI service                      |
| `FRONTEND_PORT`        | `5173`               | Host port for the dashboard                            |
| `NUM_ROBOTS`           | `4`                  | Number of robots to spawn                              |
| `LOG_LEVEL`            | `INFO`               | Backend logger verbosity                               |

---

##  Project Structure

```
warehouse_ai/
├── backend/
│   ├── app/
│   │   ├── main.py               FastAPI + WebSocket entry point
│   │   ├── models.py             Pydantic data contracts
│   │   ├── warehouse.py          Grid + shelves + zones + obstacles
│   │   ├── pathfinding.py        A* with dynamic-blocker support
│   │   ├── coordinator.py        Task allocation cost function
│   │   ├── simulation.py         Async tick loop, fleet, lifecycle
│   │   ├── llm_parser.py         Anthropic / OpenAI / local rules
│   │   ├── metrics.py            Counters + derived stats
│   │   └── websocket_manager.py  Broadcast helper
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.tsx               3-column command-center layout
│   │   ├── api.ts                REST + auto-reconnecting WebSocket
│   │   ├── types.ts              TypeScript mirrors of Pydantic models
│   │   └── components/
│   │       ├── WarehouseMap.tsx  SVG floor + animated robots + paths
│   │       ├── RobotCard.tsx     Per-robot panel
│   │       ├── TaskPanel.tsx     Queue / active / done
│   │       ├── CommandConsole.tsx Natural-language input
│   │       ├── MetricsPanel.tsx  KPI tiles
│   │       └── LogsPanel.tsx     Streaming system log
│   ├── package.json
│   ├── tailwind.config.js
│   ├── vite.config.ts
│   └── Dockerfile
├── docs/
│   ├── architecture.md           Diagrams + data flow
│   ├── research_summary.md       Academic-style writeup
│   └── ros2_upgrade_plan.md      How to lift this onto ROS 2 + Nav2
├── docker-compose.yml
├── .env.example
└── README.md
```

---

##  Research Motivation

Three observations drive the design choices:

1. **LLMs are good at intent, bad at control.** Plain-text directives like
   *"send the closest free robot to clear Zone C"* are easy for an LLM to
   decompose into a structured task, but unsafe to put directly in the
   control loop. Pushing the LLM up to the *task-specification* layer keeps
   the deterministic algorithms in charge of motion.
2. **Cost-based MRTA is a strong baseline.** Multi-robot task allocation
   with a hand-tuned cost function (distance + battery + priority) is
   competitive with much heavier optimizers on warehouse-scale fleets and
   easy to log and explain.
3. **Explainability matters in operations.** A coordinator that can justify
   *why* a particular robot was picked — and re-explain in plain English —
   is far more useful in deployment than a black-box scheduler.

Full writeup in [docs/research_summary.md](docs/research_summary.md).

---

##  Technical Details

- **Tick rate**: 4Hz simulation loop (one A* per blocked path; cheap on
  30×18 grids). Smooth on browser; trivial CPU load.
- **Pathfinding**: 4-connected A* with Manhattan heuristic and stable
  tie-breaking. Dynamic blockers (other robots) are merged at query time.
- **Allocation**: lowest-cost robot wins, where
  `cost = distance + W_b·(1−battery) − W_p·priority_weight`.
- **Failure**: a failed robot's task is returned to the queue with
  `previous_robots` populated; the auction excludes it next round.
- **Battery**: drained per cell moved; auto-charge below 20%.
- **State sync**: full world snapshot pushed every tick via WebSocket. The
  dashboard is stateless beyond the latest snapshot — refresh = no surprise.

---

##  ROS 2 / Gazebo Upgrade Path

This MVP was designed to map cleanly onto a future ROS 2 stack:

- Robots → ROS 2 nodes publishing `/robot_N/pose`, `/robot_N/status`.
- Pathfinding → Nav2 (BehaviorTree planner + DWB local controller).
- Coordinator → keep as-is; it speaks JSON over a thin ROS bridge.
- Simulation engine → replaced by Gazebo Classic (TurtleBot3) or Ignition.
- Dashboard → unchanged; same WebSocket payload.

Step-by-step plan in [docs/ros2_upgrade_plan.md](docs/ros2_upgrade_plan.md).

---

##  Troubleshooting

| Symptom                                              | Fix                                                                              |
|------------------------------------------------------|----------------------------------------------------------------------------------|
| Dashboard says **OFFLINE** in red                    | Backend not up; `docker compose logs backend` or check port 8000 is free.        |
| Backend logs "Anthropic parse failed; falling back"  | Your `ANTHROPIC_API_KEY` is unset or invalid. The rule parser still works.       |
| Robots stuck "moving_to_pickup"                      | Path got cut by obstacles; click **+ Obstacle** to test replanning, then **Reset**. |
| Port already in use                                  | Set `BACKEND_PORT` / `FRONTEND_PORT` in `.env`.                                  |
| Docker build slow on first run                       | Normal — installing `anthropic` + `openai` SDKs takes ~30s. Subsequent builds cache. |

---

##  Screenshots



- `docs/images/dashboard.png` — main view, fleet active
- `docs/images/command_console.png` — close-up of NL parsing
- `docs/images/failure_recovery.png` — robot failed + task reassigned

---

## License

MIT — see [LICENSE](LICENSE).

---

##  Citation

```bibtex
@software{warehouse_ai_2025,
  title  = {Warehouse AI: LLM-Orchestrated Multi-Agent Autonomous Warehouse},
  year   = {2025},
  note   = {Research prototype.}
}
```
