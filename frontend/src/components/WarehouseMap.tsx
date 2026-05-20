import { useMemo } from 'react';
import type { Robot, WorldState } from '../types';

interface Props {
  state: WorldState;
}

const ROBOT_COLORS = [
  '#22d3ee', // cyan
  '#f59e0b', // amber
  '#a855f7', // violet
  '#10b981', // emerald
  '#ec4899', // pink
  '#84cc16', // lime
];

// One responsive grid drawn as an SVG so we can place robots at sub-cell
// positions during transitions. The grid scales to fit its parent via
// preserveAspectRatio.
export function WarehouseMap({ state }: Props) {
  const { grid_width: W, grid_height: H, grid, robots, zones, tasks } = state;

  const CELL = 28;
  const VW = W * CELL;
  const VH = H * CELL;

  const robotById = useMemo(() => {
    const m = new Map<string, { robot: Robot; color: string }>();
    robots.forEach((r, i) => m.set(r.id, { robot: r, color: ROBOT_COLORS[i % ROBOT_COLORS.length] }));
    return m;
  }, [robots]);

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg bg-bg-deeper">
      <svg
        viewBox={`0 0 ${VW} ${VH}`}
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full"
      >
        {/* Background grid lines for visual grounding. */}
        <defs>
          <pattern id="cellgrid" width={CELL} height={CELL} patternUnits="userSpaceOnUse">
            <path
              d={`M ${CELL} 0 L 0 0 0 ${CELL}`}
              fill="none"
              stroke="rgba(255,255,255,0.04)"
              strokeWidth="1"
            />
          </pattern>
        </defs>
        <rect width={VW} height={VH} fill="url(#cellgrid)" />

        {/* Cells: shelves, obstacles, zones, storage, chargers. */}
        {grid.map((row, y) =>
          row.map((cell, x) => {
            if (cell === 'free') return null;
            const key = `${x}-${y}`;
            const cx = x * CELL;
            const cy = y * CELL;

            if (cell === 'shelf') {
              return (
                <rect
                  key={key}
                  x={cx + 2}
                  y={cy + 2}
                  width={CELL - 4}
                  height={CELL - 4}
                  rx="3"
                  fill="#1e293b"
                  stroke="rgba(255,255,255,0.06)"
                />
              );
            }
            if (cell === 'obstacle') {
              return (
                <g key={key}>
                  <rect
                    x={cx + 4}
                    y={cy + 4}
                    width={CELL - 8}
                    height={CELL - 8}
                    rx="2"
                    fill="#ef4444"
                    opacity="0.45"
                    stroke="#ef4444"
                  />
                  <line
                    x1={cx + 6}
                    y1={cy + 6}
                    x2={cx + CELL - 6}
                    y2={cy + CELL - 6}
                    stroke="#fee2e2"
                    strokeWidth="1.2"
                  />
                  <line
                    x1={cx + CELL - 6}
                    y1={cy + 6}
                    x2={cx + 6}
                    y2={cy + CELL - 6}
                    stroke="#fee2e2"
                    strokeWidth="1.2"
                  />
                </g>
              );
            }
            return null;
          }),
        )}

        {/* Zones / storage / charging — labelled tiles on top of grid. */}
        {zones.map((z) => {
          const [zx, zy] = z.cell;
          const cx = zx * CELL;
          const cy = zy * CELL;
          return (
            <g key={z.id}>
              <rect
                x={cx + 1}
                y={cy + 1}
                width={CELL - 2}
                height={CELL - 2}
                rx="4"
                fill={z.color}
                fillOpacity="0.18"
                stroke={z.color}
                strokeWidth="1.2"
              />
              <text
                x={cx + CELL / 2}
                y={cy + CELL / 2 + 3}
                textAnchor="middle"
                fontSize="9"
                fontWeight="600"
                fill={z.color}
              >
                {z.type === 'charging' ? '⚡' : z.type === 'storage' ? '📦' : z.name.replace('Zone ', '')}
              </text>
            </g>
          );
        })}

        {/* Active task overlays: pickup & dropoff pins. */}
        {tasks
          .filter((t) => t.status === 'pending' || t.status === 'assigned' || t.status === 'in_progress')
          .map((t) => {
            const [px, py] = t.pickup_cell;
            const [dx, dy] = t.dropoff_cell;
            const priorityColor =
              t.priority === 'urgent'
                ? '#ef4444'
                : t.priority === 'high'
                  ? '#f59e0b'
                  : '#94a3b8';
            return (
              <g key={`task-${t.id}`}>
                {t.status === 'pending' && (
                  <circle
                    cx={px * CELL + CELL / 2}
                    cy={py * CELL + CELL / 2}
                    r="4"
                    fill={priorityColor}
                    opacity="0.9"
                  >
                    <animate
                      attributeName="r"
                      values="4;7;4"
                      dur="1.4s"
                      repeatCount="indefinite"
                    />
                  </circle>
                )}
                <circle
                  cx={dx * CELL + CELL / 2}
                  cy={dy * CELL + CELL / 2}
                  r="2.5"
                  fill={priorityColor}
                />
              </g>
            );
          })}

        {/* Robot paths */}
        {robots
          .filter((r) => r.path && r.path.length > 0)
          .map((r) => {
            const color = robotById.get(r.id)?.color ?? '#22d3ee';
            const points: string[] = [];
            points.push(`${r.x * CELL + CELL / 2},${r.y * CELL + CELL / 2}`);
            for (const [px, py] of r.path) {
              points.push(`${px * CELL + CELL / 2},${py * CELL + CELL / 2}`);
            }
            return (
              <polyline
                key={`path-${r.id}`}
                points={points.join(' ')}
                fill="none"
                stroke={color}
                strokeWidth="1.5"
                strokeDasharray="4 4"
                opacity="0.55"
              />
            );
          })}

        {/* Robots */}
        {robots.map((r) => {
          const info = robotById.get(r.id);
          const color = info?.color ?? '#22d3ee';
          const cx = r.x * CELL + CELL / 2;
          const cy = r.y * CELL + CELL / 2;
          const radius = CELL * 0.36;
          const dim = r.failed || r.status === 'charging';
          return (
            <g
              key={r.id}
              style={{
                transition: 'transform 240ms linear',
                transform: `translate(0px, 0px)`,
              }}
            >
              {/* Glow halo */}
              <circle cx={cx} cy={cy} r={radius + 4} fill={color} opacity={dim ? 0.1 : 0.18} />
              {/* Robot body */}
              <circle
                cx={cx}
                cy={cy}
                r={radius}
                fill={r.failed ? '#7f1d1d' : color}
                opacity={dim ? 0.7 : 1}
                stroke={r.failed ? '#ef4444' : '#0b1020'}
                strokeWidth={r.failed ? 2 : 1.5}
              />
              {/* Direction indicator (just a dot facing the next step) */}
              {r.path[0] && !r.failed && (
                <line
                  x1={cx}
                  y1={cy}
                  x2={r.path[0][0] * CELL + CELL / 2}
                  y2={r.path[0][1] * CELL + CELL / 2}
                  stroke="white"
                  strokeWidth="1.2"
                  opacity="0.5"
                />
              )}
              {/* Carrying-package marker */}
              {r.carrying_package && (
                <rect
                  x={cx - 3}
                  y={cy - 3}
                  width="6"
                  height="6"
                  fill="#fef3c7"
                  stroke="#f59e0b"
                  strokeWidth="0.8"
                />
              )}
              {/* Robot label */}
              <text
                x={cx}
                y={cy + radius + 9}
                textAnchor="middle"
                fontSize="8"
                fontWeight="600"
                fill={color}
                style={{ textShadow: '0 0 4px rgba(0,0,0,0.8)' }}
              >
                {r.name}
              </text>
              {/* Battery mini-bar above robot */}
              <rect
                x={cx - radius}
                y={cy - radius - 6}
                width={radius * 2}
                height="2"
                fill="rgba(255,255,255,0.15)"
              />
              <rect
                x={cx - radius}
                y={cy - radius - 6}
                width={radius * 2 * r.battery}
                height="2"
                fill={r.battery < 0.2 ? '#ef4444' : r.battery < 0.5 ? '#f59e0b' : '#10b981'}
              />
              {/* Failed overlay */}
              {r.failed && (
                <text
                  x={cx}
                  y={cy + 3}
                  textAnchor="middle"
                  fontSize="11"
                  fontWeight="700"
                  fill="#fee2e2"
                >
                  ✕
                </text>
              )}
            </g>
          );
        })}
      </svg>

      {/* Map legend */}
      <div className="pointer-events-none absolute bottom-2 left-2 flex flex-wrap items-center gap-2 rounded-md bg-bg-deeper/80 px-2 py-1 text-[10px] text-slate-400 backdrop-blur">
        <LegendDot color="#1e293b" label="Shelf" />
        <LegendDot color="#10b981" label="Zone" />
        <LegendDot color="#f59e0b" label="Storage" />
        <LegendDot color="#a855f7" label="Charger" />
        <LegendDot color="#ef4444" label="Obstacle" />
      </div>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1">
      <span
        className="inline-block h-2.5 w-2.5 rounded-sm"
        style={{ backgroundColor: color, opacity: 0.7 }}
      />
      {label}
    </span>
  );
}
