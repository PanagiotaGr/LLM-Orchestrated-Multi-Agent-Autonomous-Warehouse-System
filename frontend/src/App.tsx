import { useCallback, useEffect, useMemo, useState } from 'react';
import { addObstacle, connectWebSocket, resetWorld } from './api';
import { CommandConsole } from './components/CommandConsole';
import { LogsPanel } from './components/LogsPanel';
import { MetricsPanel } from './components/MetricsPanel';
import { RobotCard } from './components/RobotCard';
import { TaskPanel } from './components/TaskPanel';
import { WarehouseMap } from './components/WarehouseMap';
import type { WorldState } from './types';

const ROBOT_COLORS = ['#22d3ee', '#f59e0b', '#a855f7', '#10b981', '#ec4899', '#84cc16'];

export default function App() {
  const [state, setState] = useState<WorldState | null>(null);
  const [wsStatus, setWsStatus] = useState<'connecting' | 'open' | 'closed'>('connecting');

  useEffect(() => {
    const disconnect = connectWebSocket(
      (s) => setState(s),
      (status) => setWsStatus(status),
    );
    return () => disconnect();
  }, []);

  const taskById = useMemo(() => {
    const m = new Map<string, NonNullable<WorldState>['tasks'][number]>();
    state?.tasks.forEach((t) => m.set(t.id, t));
    return m;
  }, [state]);

  const handleAddObstacle = useCallback(() => {
    addObstacle().catch(() => {});
  }, []);

  const handleReset = useCallback(() => {
    if (confirm('Reset the warehouse? All tasks and robot positions will be cleared.')) {
      resetWorld().catch(() => {});
    }
  }, []);

  if (!state) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-center">
          <div className="mx-auto h-12 w-12 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          <div className="mt-4 font-mono text-xs uppercase tracking-widest text-slate-400">
            {wsStatus === 'open' ? 'awaiting first state…' : 'connecting to backend…'}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col p-4 gap-4">
      {/* Header */}
      <header className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent/15 text-accent shadow-glow">
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor">
              <path d="M12 2 2 7v10l10 5 10-5V7L12 2zm0 2.236L19.764 8 12 11.764 4.236 8 12 4.236zM4 9.618l7 3.5v7.764l-7-3.5V9.618zm9 11.264v-7.764l7-3.5v7.764l-7 3.5z" />
            </svg>
          </div>
          <div>
            <h1 className="text-lg font-bold leading-tight text-slate-100">
              Warehouse AI <span className="text-accent">·</span> Multi-Agent Console
            </h1>
            <div className="text-[10px] font-mono text-slate-500">
              LLM-orchestrated autonomous robotics — research prototype
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn" onClick={handleAddObstacle}>
            + Obstacle
          </button>
          <button
            className="btn hover:!border-bad/40 hover:!text-bad hover:!bg-bad/10"
            onClick={handleReset}
          >
            Reset World
          </button>
        </div>
      </header>

      {/* Main layout: 3-column grid */}
      <div className="grid flex-1 grid-cols-12 gap-4 min-h-0">
        {/* Left column: robots */}
        <aside className="col-span-12 md:col-span-3 flex flex-col gap-3 overflow-y-auto pr-1">
          <MetricsPanel
            metrics={state.metrics}
            tick={state.tick}
            wsStatus={wsStatus}
          />
          <div className="panel flex flex-col">
            <div className="panel-header">
              <span className="panel-title">Fleet · {state.robots.length}</span>
            </div>
            <div className="flex flex-col gap-2 p-2">
              {state.robots.map((r, idx) => (
                <RobotCard
                  key={r.id}
                  robot={r}
                  task={r.current_task_id ? taskById.get(r.current_task_id) ?? null : null}
                  color={ROBOT_COLORS[idx % ROBOT_COLORS.length]}
                />
              ))}
            </div>
          </div>
        </aside>

        {/* Center: warehouse map + logs */}
        <section className="col-span-12 md:col-span-6 flex flex-col gap-4 min-h-0">
          <div className="panel flex-1 overflow-hidden min-h-0">
            <div className="panel-header">
              <span className="panel-title">Warehouse Floor</span>
              <span className="text-[10px] font-mono text-slate-500">
                {state.grid_width} × {state.grid_height} grid · {state.obstacles.length}{' '}
                obstacle{state.obstacles.length === 1 ? '' : 's'}
              </span>
            </div>
            <div className="p-2 h-[calc(100%-2.5rem)]">
              <WarehouseMap state={state} />
            </div>
          </div>
          <div className="h-44 flex-shrink-0">
            <LogsPanel logs={state.logs} />
          </div>
        </section>

        {/* Right column: command + tasks */}
        <aside className="col-span-12 md:col-span-3 flex flex-col gap-4 min-h-0">
          <div className="h-1/2 min-h-0">
            <CommandConsole />
          </div>
          <div className="h-1/2 min-h-0">
            <TaskPanel tasks={state.tasks} robots={state.robots} />
          </div>
        </aside>
      </div>
    </div>
  );
}
