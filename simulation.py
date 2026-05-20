"""The simulation engine.

A single async loop ticks at `TICK_HZ`. Each tick:

  1. The coordinator assigns pending tasks (priority-ordered).
  2. Each robot advances one cell along its planned path.
  3. Robots that are blocked replan (or fail, after enough retries).
  4. Battery is drained while moving; low-battery robots head to charge.
  5. The world state is broadcast to all WebSocket clients.

The engine owns the `Warehouse`, the robot fleet, the task list, the
metrics tracker, and the recent-log ring buffer. The FastAPI layer in
`main.py` is the only entry point that mutates state — it does so through
async-safe methods on `SimulationEngine`, never by poking attributes
directly. This keeps the locking discipline simple.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import deque
from typing import Any

from .coordinator import pick_robot, sort_pending_tasks
from .metrics import MetricsTracker
from .models import (
    LogEntry,
    Priority,
    Robot,
    RobotStatus,
    Task,
    TaskStatus,
    WorldState,
)
from .pathfinding import astar, path_blocked_by
from .warehouse import Warehouse
from .websocket_manager import ConnectionManager

log = logging.getLogger("simulation")


# ── Tuning constants ──────────────────────────────────────────────────────
TICK_HZ = 4.0
"""Simulation ticks per second. 4Hz feels smooth and is gentle on browsers."""

BATTERY_DRAIN_PER_STEP = 0.004
"""Fraction of full battery consumed per cell moved."""

BATTERY_CHARGE_PER_TICK = 0.01
"""Fraction of full battery restored per tick while parked on a charger."""

LOW_BATTERY = 0.20
"""Below this, an IDLE robot will head to a charger on its own."""

REPLAN_RETRY_LIMIT = 5
"""If a robot's path remains unwalkable after N replans in a row, fail the task."""

LOG_BUFFER_SIZE = 200


# ── Robot name helpers ────────────────────────────────────────────────────
ROBOT_NAMES = ["Atlas", "Boomer", "Cypher", "Drift", "Echo", "Flash", "Gizmo", "Hawk"]


class SimulationEngine:
    """Owns the world state and steps it forward in time."""

    def __init__(
        self,
        ws: ConnectionManager,
        num_robots: int = 4,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        self.ws = ws
        self.num_robots = num_robots

        # Mutable world state — guarded by `self._lock` for any cross-tick
        # mutation. Reads from the loop itself don't need to lock (single
        # writer = the tick coroutine + locked mutators from the API).
        self._lock = asyncio.Lock()

        self.warehouse = Warehouse(
            width=width or 30,
            height=height or 18,
        )
        self.robots: dict[str, Robot] = {}
        self.tasks: dict[str, Task] = {}
        self.metrics = MetricsTracker()
        self.logs: deque[LogEntry] = deque(maxlen=LOG_BUFFER_SIZE)
        self.tick = 0
        self._replan_attempts: dict[str, int] = {}
        self._running = False
        self._task_handle: asyncio.Task | None = None
        self._package_counter = 0
        self._seed_robots()

    # ──────────────────────────────────────────────────────────────────────
    # Setup
    # ──────────────────────────────────────────────────────────────────────
    def _seed_robots(self) -> None:
        chargers = self.warehouse.charging_stations()
        # Spread the robots along the bottom edge so they don't all spawn
        # on top of each other.
        spawn_y = self.warehouse.height - 1
        spacing = max(1, self.warehouse.width // (self.num_robots + 1))
        for i in range(self.num_robots):
            spawn_x = spacing * (i + 1)
            # Find the nearest free cell to the intended spawn point.
            sx, sy = spawn_x, spawn_y
            while self.warehouse.is_blocked(sx, sy):
                sx = (sx + 1) % self.warehouse.width
            robot_id = f"robot_{i}"
            self.robots[robot_id] = Robot(
                id=robot_id,
                name=ROBOT_NAMES[i % len(ROBOT_NAMES)],
                x=sx,
                y=sy,
                status=RobotStatus.IDLE,
                battery=round(random.uniform(0.6, 1.0), 2),
                home_charger=chargers[i % len(chargers)],
            )
        self._log("info", "sim", f"Spawned {self.num_robots} robots.")

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task_handle = asyncio.create_task(self._run_loop())
        self._log("info", "sim", "Simulation started.")

    async def stop(self) -> None:
        self._running = False
        if self._task_handle is not None:
            self._task_handle.cancel()
            try:
                await self._task_handle
            except asyncio.CancelledError:
                pass
            self._task_handle = None
        self._log("info", "sim", "Simulation stopped.")

    async def _run_loop(self) -> None:
        period = 1.0 / TICK_HZ
        try:
            while self._running:
                start_t = time.perf_counter()
                try:
                    await self._tick()
                except Exception:
                    # Never let one bad tick crash the whole simulation.
                    log.exception("Tick %d crashed", self.tick)
                    self._log("error", "sim", f"Tick {self.tick} crashed; continuing.")
                elapsed = time.perf_counter() - start_t
                await asyncio.sleep(max(0.0, period - elapsed))
        except asyncio.CancelledError:
            raise

    # ──────────────────────────────────────────────────────────────────────
    # Per-tick logic
    # ──────────────────────────────────────────────────────────────────────
    async def _tick(self) -> None:
        async with self._lock:
            self.tick += 1
            self._assign_pending_tasks()
            self._step_robots()
            self._maybe_send_low_battery_to_charge()
            snapshot = self._snapshot_unlocked()
        await self.ws.broadcast({"type": "state", "data": snapshot.model_dump(mode="json")})

    # --- Assignment ------------------------------------------------------
    def _assign_pending_tasks(self) -> None:
        pending = sort_pending_tasks(list(self.tasks.values()))
        if not pending:
            return
        robots = list(self.robots.values())
        for task in pending:
            winner, rationale = pick_robot(robots, task)
            if winner is None:
                continue
            # Pre-plan: if no path exists at all, postpone this task —
            # don't permanently consume the robot.
            blockers = self._other_robot_cells(exclude=winner.id)
            path = astar(self.warehouse, (winner.x, winner.y), task.pickup_cell, blockers)
            if path is None:
                self._log(
                    "warn",
                    "coordinator",
                    f"No path for {winner.name} → {task.pickup_location} ({task.id}); leaving task pending.",
                )
                continue

            task.assigned_robot_id = winner.id
            task.status = TaskStatus.ASSIGNED
            task.assigned_at = time.time()
            winner.status = RobotStatus.MOVING_TO_PICKUP
            winner.current_task_id = task.id
            winner.path = path[1:]  # drop the current cell
            self._replan_attempts[winner.id] = 0
            self._log("decision", "coordinator", rationale + f" -> {task.id}")

    # --- Robot stepping --------------------------------------------------
    def _step_robots(self) -> None:
        for robot in self.robots.values():
            if robot.failed:
                continue

            # Charging robots regain battery; once full, they go idle again.
            if robot.status == RobotStatus.CHARGING:
                robot.battery = min(1.0, robot.battery + BATTERY_CHARGE_PER_TICK)
                if robot.battery >= 0.99:
                    robot.status = RobotStatus.IDLE
                    robot.path = []
                    self._log(
                        "info",
                        robot.name,
                        f"{robot.name} fully charged ({robot.battery:.0%}).",
                    )
                continue

            # Idle with no path = nothing to do this tick.
            if not robot.path:
                continue

            next_cell = robot.path[0]
            blockers = self._other_robot_cells(exclude=robot.id)

            # Check whether the *immediate* next step is blocked. If so,
            # try a fresh plan from our current location.
            if self.warehouse.is_blocked(*next_cell) or next_cell in blockers:
                if not self._replan(robot):
                    continue
                # After a replan, fall through to attempt the new first step.
                next_cell = robot.path[0] if robot.path else None
                if next_cell is None:
                    continue
                if self.warehouse.is_blocked(*next_cell) or next_cell in blockers:
                    continue

            # Make the move.
            robot.x, robot.y = next_cell
            robot.path = robot.path[1:]
            robot.battery = max(0.0, robot.battery - BATTERY_DRAIN_PER_STEP)
            robot.distance_travelled += 1
            self.metrics.step()
            self._replan_attempts[robot.id] = 0

            # Battery exhaustion = forced failure of the current task.
            if robot.battery <= 0.0 and robot.status not in (RobotStatus.CHARGING,):
                self._handle_robot_exhausted(robot)
                continue

            # Arrival logic.
            if not robot.path:
                self._on_robot_arrived(robot)

    def _replan(self, robot: Robot) -> bool:
        """Recompute a path for a robot whose current path got blocked."""
        attempt = self._replan_attempts.get(robot.id, 0) + 1
        self._replan_attempts[robot.id] = attempt

        goal = self._current_goal_for(robot)
        if goal is None:
            return False

        blockers = self._other_robot_cells(exclude=robot.id)
        new_path = astar(self.warehouse, (robot.x, robot.y), goal, blockers)
        self.metrics.replan()
        robot.replans += 1

        if new_path is None or len(new_path) <= 1:
            self._log("warn", robot.name, f"{robot.name} cannot find a path (attempt {attempt}).")
            if attempt >= REPLAN_RETRY_LIMIT and robot.current_task_id:
                task = self.tasks.get(robot.current_task_id)
                if task is not None:
                    self._fail_and_reassign(task, robot, "unreachable goal after retries")
            return False

        robot.path = new_path[1:]
        self._log("info", robot.name, f"{robot.name} re-planned ({len(robot.path)} cells).")
        return True

    def _current_goal_for(self, robot: Robot) -> tuple[int, int] | None:
        if robot.current_task_id is None:
            return None
        task = self.tasks.get(robot.current_task_id)
        if task is None:
            return None
        if robot.status == RobotStatus.MOVING_TO_PICKUP:
            return task.pickup_cell
        if robot.status == RobotStatus.MOVING_TO_DROPOFF:
            return task.dropoff_cell
        return None

    def _on_robot_arrived(self, robot: Robot) -> None:
        task = self.tasks.get(robot.current_task_id) if robot.current_task_id else None

        if robot.status == RobotStatus.MOVING_TO_PICKUP and task is not None:
            # Picked up — replan to the drop-off.
            robot.carrying_package = task.package_id
            task.status = TaskStatus.IN_PROGRESS
            task.started_at = time.time()
            blockers = self._other_robot_cells(exclude=robot.id)
            path = astar(self.warehouse, (robot.x, robot.y), task.dropoff_cell, blockers)
            if path is None:
                self._fail_and_reassign(task, robot, "no path to drop-off")
                return
            robot.path = path[1:]
            robot.status = RobotStatus.MOVING_TO_DROPOFF
            self._log(
                "info",
                robot.name,
                f"{robot.name} picked up {task.package_id} at {task.pickup_location}.",
            )
            return

        if robot.status == RobotStatus.MOVING_TO_DROPOFF and task is not None:
            duration = time.time() - (task.created_at or time.time())
            task.status = TaskStatus.COMPLETED
            task.completed_at = time.time()
            self.metrics.task_completed(duration)
            robot.tasks_completed += 1
            robot.carrying_package = None
            robot.current_task_id = None
            robot.status = RobotStatus.IDLE
            self._log(
                "info",
                robot.name,
                f"{robot.name} delivered {task.package_id} to {task.dropoff_location} ({duration:.1f}s).",
            )
            return

        # Arrived at a charger (no task).
        if (robot.x, robot.y) in self.warehouse.charging_stations():
            robot.status = RobotStatus.CHARGING
            self._log("info", robot.name, f"{robot.name} docked to charge.")
            return

        robot.status = RobotStatus.IDLE

    # --- Battery management ---------------------------------------------
    def _maybe_send_low_battery_to_charge(self) -> None:
        for robot in self.robots.values():
            if robot.failed:
                continue
            if robot.status != RobotStatus.IDLE:
                continue
            if robot.battery > LOW_BATTERY:
                continue
            charger = self.warehouse.nearest_charger(robot.x, robot.y)
            if (robot.x, robot.y) == charger:
                robot.status = RobotStatus.CHARGING
                continue
            blockers = self._other_robot_cells(exclude=robot.id)
            path = astar(self.warehouse, (robot.x, robot.y), charger, blockers)
            if path is None or len(path) <= 1:
                continue
            robot.path = path[1:]
            robot.status = RobotStatus.MOVING_TO_DROPOFF  # reuse the "going somewhere" state
            robot.current_task_id = None  # no task, just charging
            # We piggy-back on the "arrived at charger" branch in _on_robot_arrived.
            self._log("warn", robot.name, f"{robot.name} low battery ({robot.battery:.0%}); heading to charger.")

    def _handle_robot_exhausted(self, robot: Robot) -> None:
        if robot.current_task_id and (task := self.tasks.get(robot.current_task_id)):
            self._fail_and_reassign(task, robot, "battery exhausted")
        robot.status = RobotStatus.IDLE
        robot.path = []
        self._log("error", robot.name, f"{robot.name} ran out of battery.")

    # --- Task failure / reassignment -------------------------------------
    def _fail_and_reassign(self, task: Task, robot: Robot, reason: str) -> None:
        self._log("warn", robot.name, f"Task {task.id} failed on {robot.name}: {reason}.")
        if robot.id not in task.previous_robots:
            task.previous_robots.append(robot.id)
        task.status = TaskStatus.PENDING  # back to queue
        task.assigned_robot_id = None
        task.failure_reason = reason
        self.metrics.task_reassigned()
        robot.current_task_id = None
        robot.carrying_package = None
        robot.path = []
        if not robot.failed:
            robot.status = RobotStatus.IDLE

    # --- Spatial helpers -------------------------------------------------
    def _other_robot_cells(self, exclude: str | None = None) -> set[tuple[int, int]]:
        return {
            (r.x, r.y)
            for rid, r in self.robots.items()
            if rid != exclude and not r.failed
        }

    # ──────────────────────────────────────────────────────────────────────
    # Public API (must be called from outside the tick coroutine)
    # ──────────────────────────────────────────────────────────────────────
    async def create_task(
        self,
        dropoff_location: str,
        pickup_location: str = "storage",
        priority: Priority = Priority.NORMAL,
        package_id: str | None = None,
    ) -> Task | None:
        async with self._lock:
            pickup_cell = self.warehouse.resolve_location(pickup_location)
            dropoff_cell = self.warehouse.resolve_location(dropoff_location)
            if pickup_cell is None:
                self._log("error", "api", f"Unknown pickup location: {pickup_location}.")
                return None
            if dropoff_cell is None:
                self._log("error", "api", f"Unknown drop-off location: {dropoff_location}.")
                return None

            if not package_id:
                self._package_counter += 1
                package_id = f"P{self._package_counter:03d}"

            task = Task(
                package_id=package_id,
                pickup_location=pickup_location,
                dropoff_location=dropoff_location,
                pickup_cell=pickup_cell,
                dropoff_cell=dropoff_cell,
                priority=priority,
            )
            self.tasks[task.id] = task
            self._log(
                "info",
                "api",
                f"Task {task.id} created: {package_id} {pickup_location} → {dropoff_location} ({priority.value}).",
            )
            return task

    async def fail_robot(self, robot_id: str) -> bool:
        async with self._lock:
            robot = self.robots.get(robot_id)
            if robot is None:
                return False
            robot.failed = True
            robot.status = RobotStatus.FAILED
            if robot.current_task_id and (task := self.tasks.get(robot.current_task_id)):
                self._fail_and_reassign(task, robot, "simulated robot failure")
            robot.path = []
            self._log("error", robot.name, f"{robot.name} marked as FAILED.")
            return True

    async def revive_robot(self, robot_id: str) -> bool:
        async with self._lock:
            robot = self.robots.get(robot_id)
            if robot is None:
                return False
            robot.failed = False
            robot.status = RobotStatus.IDLE
            robot.battery = max(robot.battery, 0.5)
            self._log("info", robot.name, f"{robot.name} restored to service.")
            return True

    async def recharge_low_battery_robots(self) -> int:
        async with self._lock:
            n = 0
            for robot in self.robots.values():
                if robot.failed or robot.status == RobotStatus.CHARGING:
                    continue
                if robot.battery > LOW_BATTERY:
                    continue
                charger = self.warehouse.nearest_charger(robot.x, robot.y)
                blockers = self._other_robot_cells(exclude=robot.id)
                path = astar(self.warehouse, (robot.x, robot.y), charger, blockers)
                if not path:
                    continue
                robot.path = path[1:]
                robot.status = RobotStatus.MOVING_TO_DROPOFF
                robot.current_task_id = None
                n += 1
            self._log("decision", "coordinator", f"Manually dispatched {n} robot(s) to chargers.")
            return n

    async def reassign_unfinished(self) -> int:
        async with self._lock:
            n = 0
            for task in self.tasks.values():
                if task.status in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS):
                    rid = task.assigned_robot_id
                    if rid and rid in self.robots:
                        robot = self.robots[rid]
                        if robot.failed:
                            self._fail_and_reassign(task, robot, "manual reassignment")
                            n += 1
                            continue
                    task.status = TaskStatus.PENDING
                    task.assigned_robot_id = None
                    if rid:
                        if rid not in task.previous_robots:
                            task.previous_robots.append(rid)
                        if rid in self.robots:
                            self.robots[rid].current_task_id = None
                            self.robots[rid].path = []
                            self.robots[rid].status = RobotStatus.IDLE
                    self.metrics.task_reassigned()
                    n += 1
            self._log("decision", "coordinator", f"Reassigned {n} unfinished task(s).")
            return n

    async def add_obstacle(self, x: int | None = None, y: int | None = None) -> tuple[int, int] | None:
        async with self._lock:
            if x is not None and y is not None:
                if self.warehouse.add_obstacle(x, y):
                    cell = (x, y)
                else:
                    return None
            else:
                cell = self.warehouse.add_random_obstacle()

            if cell is None:
                return None

            # Force replanning for any robot whose path now crosses the obstacle.
            for robot in self.robots.values():
                if cell in robot.path:
                    self._log("info", robot.name, f"{robot.name}'s path crosses new obstacle {cell}; will replan.")
            self._log("warn", "world", f"Obstacle dropped at {cell}.")
            return cell

    async def reset(self) -> None:
        async with self._lock:
            self.warehouse = Warehouse(self.warehouse.width, self.warehouse.height)
            self.robots = {}
            self.tasks = {}
            self.metrics.reset()
            self.logs.clear()
            self.tick = 0
            self._replan_attempts = {}
            self._package_counter = 0
            self._seed_robots()
            self._log("info", "sim", "Simulation reset.")

    # ──────────────────────────────────────────────────────────────────────
    # Snapshot
    # ──────────────────────────────────────────────────────────────────────
    async def snapshot(self) -> WorldState:
        async with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> WorldState:
        active = sum(
            1 for r in self.robots.values() if not r.failed and r.status != RobotStatus.CHARGING
        )
        failed = sum(1 for r in self.robots.values() if r.failed)
        return WorldState(
            grid_width=self.warehouse.width,
            grid_height=self.warehouse.height,
            grid=self.warehouse.render_grid(),
            robots=list(self.robots.values()),
            tasks=list(self.tasks.values()),
            zones=list(self.warehouse.zones.values()),
            obstacles=list(self.warehouse.obstacles),
            metrics=self.metrics.snapshot(active, failed),
            logs=list(self.logs),
            tick=self.tick,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────────
    def _log(self, level: str, source: str, message: str) -> None:
        entry = LogEntry(level=level, source=source, message=message)  # type: ignore[arg-type]
        self.logs.append(entry)
        log.log(
            {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(
                level, logging.INFO
            ),
            "[%s] %s",
            source,
            message,
        )
