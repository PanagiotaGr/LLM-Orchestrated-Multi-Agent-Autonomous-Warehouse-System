"""A* path planning on the warehouse grid.

The planner returns a list of `(x, y)` cells from start to goal, inclusive.
It accepts an optional set of *dynamic blockers* (cells currently occupied
by other robots) so that paths can be re-planned around live traffic
without having to mutate the warehouse itself.

The grid is 4-connected with uniform step cost = 1. Manhattan distance is
the chosen heuristic — admissible for 4-connected grids — and a tie-break
on h(n) keeps the expansion order stable and visually clean.
"""

from __future__ import annotations

import heapq
from typing import Iterable

from .warehouse import Warehouse


def manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _reconstruct(
    came_from: dict[tuple[int, int], tuple[int, int]],
    current: tuple[int, int],
) -> list[tuple[int, int]]:
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path


def astar(
    warehouse: Warehouse,
    start: tuple[int, int],
    goal: tuple[int, int],
    dynamic_blockers: Iterable[tuple[int, int]] | None = None,
    max_expansions: int = 5000,
) -> list[tuple[int, int]] | None:
    """Plan a path from `start` to `goal`.

    Args:
        warehouse: the static map (shelves, zones, current obstacles).
        start: source cell.
        goal: destination cell.
        dynamic_blockers: extra cells to treat as blocked (e.g. other robots).
            The start cell itself is always considered passable.
        max_expansions: safety cap — returns None if the search blows past
            this. Prevents pathological cases from stalling the sim loop.

    Returns:
        A list `[start, …, goal]`, or `None` if no path exists.
    """
    if start == goal:
        return [start]

    blockers: set[tuple[int, int]] = set(dynamic_blockers or ())
    blockers.discard(start)
    # Always allow stepping into the goal — otherwise tasks targeting a
    # zone/storage cell that's currently the destination of another robot
    # would become unreachable.
    blockers.discard(goal)

    if warehouse.is_blocked(*goal):
        return None

    open_heap: list[tuple[int, int, int, tuple[int, int]]] = []
    # (f, h, tie_breaker, node)
    counter = 0
    h0 = manhattan(start, goal)
    heapq.heappush(open_heap, (h0, h0, counter, start))

    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score: dict[tuple[int, int], int] = {start: 0}

    expansions = 0
    while open_heap:
        expansions += 1
        if expansions > max_expansions:
            return None

        _, _, _, current = heapq.heappop(open_heap)
        if current == goal:
            return _reconstruct(came_from, current)

        for nxt in warehouse.neighbors(*current):
            if nxt in blockers:
                continue
            tentative = g_score[current] + 1
            if tentative < g_score.get(nxt, float("inf")):
                came_from[nxt] = current
                g_score[nxt] = tentative
                h = manhattan(nxt, goal)
                f = tentative + h
                counter += 1
                heapq.heappush(open_heap, (f, h, counter, nxt))

    return None


def path_blocked_by(
    path: list[tuple[int, int]],
    warehouse: Warehouse,
    dynamic_blockers: Iterable[tuple[int, int]] | None = None,
) -> tuple[int, int] | None:
    """Check whether `path` is still walkable.

    Returns the first blocked cell encountered, or None if the path is clear.
    Used by the simulation loop to decide whether to trigger a replan.
    """
    blockers = set(dynamic_blockers or ())
    for cell in path:
        if warehouse.is_blocked(*cell) or cell in blockers:
            return cell
    return None
