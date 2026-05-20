# Warehouse AI: LLM-Orchestrated Multi-Agent Autonomous Warehouse

**Research Summary**

---

## Abstract

We present **Warehouse AI**, a prototype multi-agent warehouse-robotics
system that combines classical motion planning, cost-based task
allocation, and a large-language-model (LLM) command-interpretation
layer. A team of mobile robots executes pickup-and-delivery tasks on a
shared 2D occupancy grid while a central coordinator assigns tasks using
a weighted nearest-cost heuristic that accounts for distance, battery
state, and task priority. Natural-language operator directives are
parsed by an LLM (or a rule-based fallback) into structured task
specifications, never into low-level motion commands — keeping the LLM
out of the safety-critical inner control loop. The system supports
dynamic obstacle insertion, runtime robot failure, automatic task
reassignment, and battery management with auto-recharge. We describe
the architecture, discuss design choices, and outline limitations and
future work, including a concrete migration path to ROS 2 with Nav2 and
Gazebo. This is an MVP-stage research prototype, not a deployment-ready
system.

---

## 1. Introduction

Modern fulfillment warehouses operate fleets of mobile robots in
shared spaces with strict throughput targets. Two complementary
challenges define the engineering problem: **task allocation** — deciding
which robot should do which job — and **motion planning** — getting
that robot there safely. A third, more recent challenge is the
**human-machine interface**: how should an operator command a fleet?
Drop-down forms and rigid templates work but scale poorly; natural
language is appealing but introduces a new failure mode (the LLM
misunderstanding the request and triggering unsafe actions).

Warehouse AI is a small, complete prototype that demonstrates one
defensible approach to all three challenges:

1. **Allocation** as a weighted-cost greedy heuristic with explicit,
   loggable per-decision rationale.
2. **Motion** via A* on a 4-connected occupancy grid with dynamic
   replanning.
3. **HMI** via an LLM that *only* produces structured task
   specifications, which a deterministic backend then executes (or
   refuses to execute).

The contribution is not algorithmic novelty; it is a clean,
runnable, end-to-end integration of these layers in a system that fits
on a laptop and can be inspected at every interface.

---

## 2. System Architecture

The system is split into a Python (FastAPI) backend and a TypeScript
(React) dashboard, communicating via REST for state-changing actions
and a WebSocket for continuous world-state broadcast.

The backend runs a single asyncio event loop that ticks the
simulation at 4 Hz. Each tick performs:

1. **Task assignment** — newly-pending tasks are matched to eligible
   robots by the coordinator.
2. **Motion** — each non-failed, non-idle robot advances one cell
   along its planned path.
3. **Battery accounting** — moving drains battery; parked-on-charger
   robots gain battery.
4. **Auto-charging dispatch** — idle robots below the
   low-battery threshold receive a path to the nearest charger.
5. **State broadcast** — the full world snapshot is pushed to every
   connected dashboard.

A single `asyncio.Lock` serializes external mutators (REST handlers,
parsed-command effects) with the tick coroutine, eliminating
concurrent-write races without resorting to threads.

---

## 3. Multi-Agent Coordination

### 3.1 Task Allocation as Weighted-Cost MRTA

The coordinator implements a single-task, single-robot, instantaneous-
assignment (ST-SR-IA) variant of multi-robot task allocation
[Gerkey & Matarić, 2004], a deliberately simple baseline. For each
pending task $t$ and each eligible robot $r$, the cost is:

$$
C(r, t) \;=\; W_d \cdot d(r, t) \;+\; W_b \cdot (1 - b_r) \;-\; W_p \cdot \pi(t)
$$

where $d(r, t)$ is the Manhattan distance from $r$'s current cell to
$t$'s pickup cell *plus* pickup-to-dropoff distance, $b_r \in [0, 1]$
is $r$'s normalised battery state, and $\pi(t) \in \{0, 1, 2, 3\}$
encodes priority (low / normal / high / urgent). Weights are tuned
empirically: $W_d = 1$, $W_b = 20$, $W_p = 10$ —
priority dominates short-distance ties, but a critically low battery
will rule a robot out before priority can pull it in. Robots that
have previously failed the same task are filtered out, preventing
infinite-retry loops.

Tasks are processed in priority order so that, e.g., an urgent task
always sees the full available fleet rather than the leftover after
normal-priority tasks have been satisfied first.

### 3.2 Pre-Assignment Path Verification

Before committing a robot to a task, the coordinator pre-plans a path
to the pickup. If A* fails (e.g. the goal is currently boxed in by
obstacles or other robots), the task is left pending and re-evaluated
next tick. This avoids the failure mode where a chosen robot gets
"locked" to a task it cannot physically begin, blocking the queue.

### 3.3 Explainability

Every assignment produces a one-sentence rationale of the form
*"Picked Atlas (score 14.2, battery 76%). Next-best Boomer scored 19.7
(margin 5.5)."* This is appended to the system log and surfaced in the
dashboard. Explainability is treated as a first-class requirement,
not an afterthought: when an operator sees an unexpected dispatch,
they can immediately read *why*.

---

## 4. Path Planning

A* with a Manhattan heuristic is used as the motion planner. The
heuristic is admissible on 4-connected uniform-cost grids, guaranteeing
optimal path length. A stable secondary key on $h(n)$ keeps the
expansion order deterministic across runs, which matters for
reproducibility when debugging the dashboard. Diagonal moves are
disallowed to prevent corner-clipping artefacts; this trades ~30%
path-length efficiency in open spaces for visibly cleaner robot
trajectories.

Dynamic obstacles — other robots' current cells — are passed to A* as
an additional blocker set at query time. The start cell is always
considered passable to prevent the planner from refusing to move a
robot whose start happens to coincide with another robot's cell during
a transient overlap.

The planner is wrapped with a `max_expansions` safety cap (5,000 by
default). A bounded search prevents pathological queries from stalling
the tick loop; an over-budget search returns `None`, treated as "no
path", which the coordinator handles gracefully.

---

## 5. LLM-Based Orchestration

A common failure mode in current LLM-controlled robotics is to let the
model emit low-level actuation commands. We deliberately avoid this.
The LLM is constrained to a strict role: convert one free-text user
utterance into one structured `ParsedIntent`. The intent schema (see
`models.py`) has seven actions; only seven things can happen. The
deterministic backend then validates the intent and either executes it
or refuses.

This layering has three consequences worth noting:

1. **Bounded blast radius** — at worst, the LLM creates a task to an
   already-existing zone; it cannot, for example, instruct a robot to
   ignore a charger.
2. **Hot-swappable parser** — Anthropic Claude, OpenAI GPT, and a
   regex-based local parser share the same output schema. The system
   is fully usable offline.
3. **Graceful degradation** — on any parser exception (API timeout,
   malformed JSON, validation failure) the system falls back to the
   local parser. The dashboard never sees a 500.

The system prompt is short, explicit about the action enum, and
explicit about emitting JSON only. Empirically this is sufficient on
Claude-class and GPT-4-class models; we observed correct parses on all
ten demo commands in our internal sanity tests with both providers.

---

## 6. Failure Recovery

Two failure modes are simulated: **operator-triggered robot failure**
(a UI button or "fail robot 2" command) and **battery exhaustion**.
Both follow the same recovery flow:

1. The robot transitions to `failed` (or just `idle`, for battery
   exhaustion).
2. Its in-flight task — if any — is returned to `pending`, with the
   failed robot recorded in `previous_robots`.
3. The metric counter `tasks_reassigned` is incremented.
4. The coordinator excludes that robot on the next assignment cycle.
5. Some other robot picks up the task on the next tick.

This is the same recovery flow used when a path becomes unwalkable
after `REPLAN_RETRY_LIMIT` consecutive failed replans, providing a
single coherent failure-handling story across causes.

---

## 7. Metrics

The simulation tracks:

- `tasks_completed` — total tasks successfully delivered.
- `tasks_failed` — total tasks abandoned permanently (none in current
  implementation; tasks always re-queue).
- `tasks_reassigned` — total task-to-robot reassignments.
- `replanning_events` — number of A* re-invocations due to blocked
  paths.
- `average_completion_time_s` — mean wall-clock from task creation to
  delivery, in seconds.
- `total_distance` — cumulative robot-cells travelled across the fleet.
- per-robot: `tasks_completed`, `distance_travelled`, `replans`.

These are sufficient for the qualitative dashboard, but not — by any
stretch — a research-grade evaluation. See limitations.

---

## 8. Limitations

We are deliberately direct about what this prototype is *not*:

1. **No perception layer.** Robots have a perfect global view of the
   warehouse. There is no SLAM, no obstacle detection from sensors,
   no localization noise. Real warehouse robots spend most of their
   complexity budget here; we spend none.
2. **Idealised kinematics.** Robots teleport one cell per tick.
   There are no holonomic constraints, no acceleration limits, no
   wheel models.
3. **No formal optimality guarantees on allocation.** Greedy
   weighted-cost is a good baseline, not a competitive solver. We do
   not claim optimality. In particular, the allocation is myopic: it
   does not anticipate future tasks or future robot availability.
4. **No formal evaluation.** We report no throughput-vs-fleet-size
   sweeps, no comparison against baselines, no statistical analysis.
5. **Simulated failure modes only.** Failures are stochastically
   inserted by the operator. We do not model realistic failure
   distributions (sensor drift, wheel slip, motor stalls, communication
   loss).
6. **LLM parser is not adversarially tested.** A determined operator
   could likely produce a phrasing that confuses the rule-based
   parser. The intent schema's bounded output limits the worst-case
   consequence, but we do not characterize the failure surface.

---

## 9. Future Work

In rough priority order:

1. **ROS 2 migration.** The module boundaries were designed with this
   in mind. Concrete plan in
   [`docs/ros2_upgrade_plan.md`](ros2_upgrade_plan.md): robots become
   ROS nodes, A* is replaced by Nav2, the coordinator and dashboard
   remain essentially unchanged.
2. **Gazebo physics.** Replace the 4-connected grid with a continuous
   2D world using TurtleBot3 in Gazebo Classic, then port the
   coordinator unchanged.
3. **Conflict-Based Search.** Add a multi-agent path-finding (MAPF)
   solver such as CBS [Sharon et al., 2015] to handle dense fleets
   where greedy A* + dynamic blockers degrades into livelock.
4. **Task-aware LLM dialogue.** Currently the parser is stateless.
   A short conversation loop ("which Zone?" / "how urgent?") would
   handle under-specified commands more gracefully.
5. **Quantitative evaluation.** Throughput-vs-fleet-size, allocation-
   policy ablation, robustness-to-failure-rate. None of this exists yet.
6. **Real hardware integration.** TurtleBot3 or similar, via the same
   ROS bridge planned for the Gazebo migration.

---

## 10. Acknowledgments

This system was built as a portfolio / research prototype. Design
choices were guided by published literature on multi-robot task
allocation [Gerkey & Matarić, 2004], grid-based MAPF [Sharon et al.,
2015], and recent surveys on LLM-as-orchestrator architectures.

---

## References

- Gerkey, B. P., & Matarić, M. J. (2004). *A formal analysis and
  taxonomy of task allocation in multi-robot systems.* The
  International Journal of Robotics Research, 23(9), 939–954.
- Sharon, G., Stern, R., Felner, A., & Sturtevant, N. R. (2015).
  *Conflict-based search for optimal multi-agent pathfinding.*
  Artificial Intelligence, 219, 40–66.
- Hart, P. E., Nilsson, N. J., & Raphael, B. (1968). *A formal basis
  for the heuristic determination of minimum cost paths.* IEEE
  Transactions on Systems Science and Cybernetics, 4(2), 100–107.
