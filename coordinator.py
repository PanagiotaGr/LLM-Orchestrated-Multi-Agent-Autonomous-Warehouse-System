"""Multi-robot task allocation.

Picks the best available robot for each pending task using a weighted cost
function. Lower cost is better:

    score = manhattan_to_pickup
          + battery_penalty
          + priority_penalty
          - preferred_robot_bonus

Robots that have previously failed this task, or are themselves in a
failed/charging state, are excluded.

This is the classical "greedy nearest-with-cost" baseline for multi-robot
task allocation (MRTA). It scales linearly in robots × open-tasks and is
deterministic, which makes its decisions easy to log and explain.
"""

from __future__ import annotations

import logging

from .models import Priority, Robot, RobotStatus, Task, TaskStatus

log = logging.getLogger("coordinator")


# ─── Cost weights ──────────────────────────────────────────────────────────
W_DISTANCE = 1.0
W_BATTERY = 20.0   # full empty robot is heavily penalised
W_PRIORITY = 10.0  # higher priority subtracts cost
W_PREFERRED = 50.0


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def score_robot_for_task(robot: Robot, task: Task) -> float | None:
    """Return the cost of assigning `robot` to `task`, or None if ineligible.

    A robot is ineligible when it is failed, currently busy, charging, or
    has previously failed this exact task.
    """
    if robot.failed or robot.status not in (RobotStatus.IDLE,):
        return None
    if robot.battery <= 0.10:
        return None
    if robot.id in task.previous_robots:
        return None

    distance = _manhattan((robot.x, robot.y), task.pickup_cell)
    distance += _manhattan(task.pickup_cell, task.dropoff_cell)

    battery_pen = (1.0 - robot.battery) * W_BATTERY
    priority_bonus = task.priority.weight * W_PRIORITY
    return (W_DISTANCE * distance) + battery_pen - priority_bonus


def pick_robot(robots: list[Robot], task: Task) -> tuple[Robot | None, str]:
    """Choose the best robot for `task`.

    Returns `(robot, rationale)`. `robot` is None if no robot is eligible;
    the rationale string is logged and surfaced in the dashboard.
    """
    scored: list[tuple[float, Robot]] = []
    for r in robots:
        s = score_robot_for_task(r, task)
        if s is None:
            continue
        scored.append((s, r))

    if not scored:
        return None, "No eligible robots (all busy, failed, or low battery)."

    scored.sort(key=lambda t: t[0])
    winner_score, winner = scored[0]

    if len(scored) > 1:
        runner_score, runner = scored[1]
        margin = runner_score - winner_score
        rationale = (
            f"Picked {winner.name} (score {winner_score:.1f}, "
            f"battery {winner.battery:.0%}). "
            f"Next-best {runner.name} scored {runner_score:.1f} "
            f"(margin {margin:.1f})."
        )
    else:
        rationale = (
            f"Picked {winner.name} (score {winner_score:.1f}, "
            f"battery {winner.battery:.0%}). Only eligible robot."
        )

    return winner, rationale


def sort_pending_tasks(tasks: list[Task]) -> list[Task]:
    """Highest-priority first, FIFO within a priority tier."""
    return sorted(
        (t for t in tasks if t.status == TaskStatus.PENDING),
        key=lambda t: (-t.priority.weight, t.created_at),
    )
