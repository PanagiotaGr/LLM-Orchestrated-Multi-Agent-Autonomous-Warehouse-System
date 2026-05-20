# ROS 2 / Gazebo Upgrade Plan

This document describes how to migrate **Warehouse AI** from its current
local-simulation MVP onto a real robotics stack — ROS 2 Humble (or newer),
Nav2 for navigation, Gazebo Classic / Ignition for physics, and
SLAM Toolbox for mapping. The MVP was designed with this migration in
mind: module boundaries were chosen so that the coordinator and
dashboard survive the transition essentially unchanged.

---

## 1. What changes, what stays the same

| Layer                         | MVP today                       | ROS 2 target                         | Effort  |
|-------------------------------|----------------------------------|--------------------------------------|---------|
| World physics                 | 2D grid, teleport-per-tick      | Gazebo Classic + TurtleBot3 models   | High    |
| Per-robot motion              | A* on grid                      | Nav2 (BT planner + DWB controller)   | Medium  |
| Inter-robot communication     | Python objects in one process   | ROS 2 topics + DDS                   | Medium  |
| Mapping                       | Hardcoded grid                  | SLAM Toolbox or Cartographer         | Medium  |
| **Coordinator**               | `coordinator.py` (greedy MRTA)  | **Same code**, bridged to ROS topics | Low     |
| **LLM parser**                | `llm_parser.py`                 | **Unchanged**                        | None    |
| **Dashboard**                 | React + WebSocket               | **Unchanged**; same payload schema   | None    |
| Backend transport             | FastAPI + asyncio               | FastAPI + `rclpy` node + rosbridge   | Low     |

The take-away: **two thirds of the codebase doesn't move.** Only the
simulation engine itself is replaced.

---

## 2. ROS 2 system topology

```text
       ┌─────────────────────────────────────────────────────────────┐
       │                    React Dashboard (unchanged)              │
       └───────────────────────────────┬─────────────────────────────┘
                                       │ WebSocket (same payload)
       ┌───────────────────────────────▼─────────────────────────────┐
       │      Warehouse AI Backend (FastAPI + rclpy ROS 2 node)      │
       │                                                              │
       │   Coordinator (unchanged)   LLM Parser (unchanged)           │
       │                │                                             │
       │                ▼                                             │
       │       Nav2 client (per robot) — sends NavigateToPose goals   │
       └─────────────┬───────────────────────────────────────────────┘
                     │ DDS
       ┌─────────────┴──────────────────────────────────────────────┐
       │                       ROS 2 Graph                          │
       │                                                            │
       │   /robot_0/pose   /robot_0/cmd_vel   /robot_0/status       │
       │   /robot_0/battery_state                                   │
       │   /robot_1/pose   ...                                      │
       │   /tf, /tf_static                                          │
       │   /map  (occupancy grid)                                   │
       │                                                            │
       │   Nav2 stack per robot:                                    │
       │     • planner_server (BT navigator)                        │
       │     • controller_server (DWB)                              │
       │     • behavior_server (recovery)                           │
       └────────────────────────────┬───────────────────────────────┘
                                    │
       ┌────────────────────────────▼───────────────────────────────┐
       │              Gazebo Classic (or Ignition)                  │
       │                                                            │
       │   warehouse.world           TurtleBot3 robots × N          │
       │   Aisle shelves             Differential drive plugin      │
       │   Charging stations         LIDAR / odometry plugins       │
       └────────────────────────────────────────────────────────────┘
```

---

## 3. Migration plan, step by step

### Step 1 — Stand up ROS 2 + Gazebo

```bash
# Ubuntu 22.04 + ROS 2 Humble
sudo apt install ros-humble-desktop ros-humble-nav2-bringup \
                 ros-humble-turtlebot3 ros-humble-turtlebot3-gazebo \
                 ros-humble-slam-toolbox ros-humble-rosbridge-server
```

Smoke test:

```bash
source /opt/ros/humble/setup.bash
ros2 launch turtlebot3_gazebo empty_world.launch.py
```

### Step 2 — Build the warehouse world

Create a Gazebo `.world` file with the same logical layout as
`warehouse.py`:

- shelves as `box` collision objects in three double rows
- delivery zones as flat coloured planes
- charging-station markers at the corners

Save as `gazebo/worlds/warehouse.world` and launch with a
`TURTLEBOT3_MODEL=waffle` argument.

### Step 3 — Spawn multiple robots

`gazebo/launch/multi_robot.launch.py` should spawn N TurtleBots, each
namespaced (`/robot_0/...`, `/robot_1/...`). Each namespace gets its
own `robot_state_publisher`, AMCL (or SLAM Toolbox), and Nav2 stack.

This is well-trodden ground — see the `nav2_bringup` multi-robot
example.

### Step 4 — Replace `pathfinding.py` with a Nav2 client

The MVP's A* lives behind a single function:

```python
astar(warehouse, start, goal, dynamic_blockers) -> list[(x, y)] | None
```

Replace it with a `Nav2Client`:

```python
async def go_to(self, robot_ns: str, goal: PoseStamped) -> bool:
    action_client = self._navigate_to_pose_clients[robot_ns]
    future = action_client.send_goal_async(NavigateToPose.Goal(pose=goal))
    ...
```

The coordinator no longer needs cell-level paths; it just sends goals
to Nav2 and awaits the result code. Reassignment-on-failure logic in
`simulation.py` becomes: if Nav2 returns `ABORTED`, treat it the same
way the MVP treats `REPLAN_RETRY_LIMIT` exhaustion.

### Step 5 — Replace the tick loop with ROS subscriptions

The simulation engine becomes an `rclpy.Node` that subscribes to:

- `/robot_N/odom` → derives `(x, y)` for the dashboard payload.
- `/robot_N/battery_state` → updates the `battery` field.
- `/robot_N/status` (custom msg) → updates `RobotStatus`.

It still owns the task queue, the metrics tracker, and the WebSocket
broadcaster. The broadcaster runs at a slower rate now (say 5 Hz on a
ROS timer) to avoid overwhelming the dashboard with high-rate odometry.

### Step 6 — Map and obstacles

Replace the runtime obstacle set with a `/map` topic produced by
SLAM Toolbox (in mapping mode) or simply hand-authored
(`map.yaml + map.pgm`). Dynamic obstacles become published via Nav2's
local costmap layers (`obstacle_layer` from LIDAR).

The "Add obstacle" UI button becomes a service call that publishes a
short-lived object into `/global_costmap/inflated_layer`.

### Step 7 — LLM parser

**No changes.** The parser already produces structured intents. The
only difference is that `create_task` now resolves to a `PoseStamped`
goal instead of a grid cell, but that resolution already lives in
`Warehouse.resolve_location()`; just port it to look up `geometry_msgs`
poses instead of `(x, y)`.

### Step 8 — Dashboard

**No changes.** The `WorldState` schema already contains symbolic
fields (status, battery, current task, position) that work the same
whether the source is a grid simulation or a Gazebo physics engine.

---

## 4. Mapping the MVP module-by-module

| MVP module              | ROS 2 equivalent                                     |
|-------------------------|------------------------------------------------------|
| `warehouse.py`          | Gazebo world + static map yaml + zone-pose lookup    |
| `pathfinding.py`        | Nav2 (planner_server + controller_server)            |
| `coordinator.py`        | **Same code**; emits Nav2 goals instead of paths     |
| `simulation.py`         | `rclpy.Node` subscribing to robot topics             |
| `models.py`             | Same Pydantic models; add ROS msg ↔ model converters |
| `metrics.py`            | **Unchanged**                                        |
| `llm_parser.py`         | **Unchanged**                                        |
| `websocket_manager.py`  | **Unchanged**                                        |
| `main.py`               | Same FastAPI app, now also runs `rclpy.spin()`       |

---

## 5. Long-term: NVIDIA Isaac Sim

For high-fidelity simulation with photorealistic rendering and
synthetic-data generation:

1. Port the Gazebo `.world` to USD via Isaac Sim's importer.
2. Use Isaac ROS bridges (`isaac_ros_visual_slam`, `isaac_ros_nvblox`)
   to keep the ROS-side topology unchanged.
3. Add Isaac's GPU-accelerated lidar / RGB-D sensors to the robot
   URDFs.
4. Run the same coordinator and the same dashboard.

This is a future direction, not a near-term plan. It's worth flagging
because the architectural decisions made today — symbolic intents,
strict module boundaries, full-snapshot broadcasts — survive the jump
to Isaac without rework.

---

## 6. Risks & open questions

- **Greedy MRTA may not scale.** At ~20+ robots, a coordinator that
  picks one robot at a time without considering future tasks
  underperforms. Plan: swap in a Conflict-Based Search variant for
  the MAPF layer; keep the greedy auction at the dispatch layer.
- **Battery in Gazebo is fake.** TurtleBot3 has no battery model out
  of the box. We'll need a custom plugin or an out-of-band counter
  driven by motion commands.
- **Topic namespacing in multi-robot Nav2** is fiddly and the
  TurtleBot3 launch files don't all respect namespaces cleanly.
  Expect a few days of YAML wrangling.
- **rosbridge for the dashboard** adds a hop. Native WebSocket
  publishing from the FastAPI node, as the MVP already does, is
  likely simpler.

---

## 7. Estimated effort

- Standing up the multi-robot Gazebo world: **2–3 days**
- Wiring Nav2 + topics into the existing backend: **3–5 days**
- Battery / failure / charging glue: **1–2 days**
- Testing + polishing: **3–5 days**

Total: roughly **2–3 weeks of focused work** to reach a Gazebo demo at
the same fidelity (or better) as today's MVP, with the dashboard and
LLM layer unchanged.
