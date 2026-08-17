import { Link } from "react-router-dom";

import type { ScopedDependencyEdge, TaskRead } from "../api/types";
import { DependencyState } from "./DependencyState";

function taskPath(projectId: string, taskId: string) {
  return `/p/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}`;
}

function finiteLayers(children: Array<TaskRead>, edges: Array<ScopedDependencyEdge>) {
  const childIds = new Set(children.map((task) => task.id));
  const incoming = new Map(children.map((task) => [task.id, 0]));
  const outgoing = new Map(children.map((task) => [task.id, [] as Array<string>]));
  for (const edge of edges) {
    if (!childIds.has(edge.source) || !childIds.has(edge.target)) continue;
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
    outgoing.get(edge.source)?.push(edge.target);
  }

  const remaining = new Set(children.map((task) => task.id));
  const layers: Array<Array<string>> = [];
  while (remaining.size > 0) {
    const ready = [...remaining].filter((id) => (incoming.get(id) ?? 0) === 0).sort();
    if (ready.length === 0) {
      layers.push([...remaining].sort());
      break;
    }
    layers.push(ready);
    for (const id of ready) {
      remaining.delete(id);
      for (const target of outgoing.get(id) ?? []) {
        incoming.set(target, (incoming.get(target) ?? 0) - 1);
      }
    }
  }
  return layers;
}

export function DependencyGraph({
  children,
  edges,
  projectId,
  umbrellaTitle,
}: {
  children: Array<TaskRead>;
  edges: Array<ScopedDependencyEdge>;
  projectId: string;
  umbrellaTitle: string;
}) {
  if (children.length === 0) return null;
  const byId = new Map(children.map((task) => [task.id, task]));
  const layers = finiteLayers(children, edges);
  const external = [...new Set(edges.flatMap((edge) => [
    ...(edge.source_contained ? [] : [edge.source]),
    ...(edge.target_contained ? [] : [edge.target]),
  ]))];
  const hasCycle = children.some((task) => (task.needs_cycles?.length ?? 0) > 0);

  return (
    <section className="rounded-lg border border-dark-border bg-dark-surface p-4 min-[820px]:p-6" aria-label="Umbrella dependency graph">
      <h2 className="text-lg font-semibold">Sequence inside {umbrellaTitle}</h2>
      <p className="mt-1 text-xs text-dark-muted">The dashed frame means “contained by this umbrella.” Arrows mean execution order.</p>
      {hasCycle && <p role="alert" className="mt-3 rounded border border-amber-600 bg-amber-950/40 p-3 text-sm text-amber-200">Dependency data error: this sequence contains a cycle. Every task is still shown.</p>}

      <div className="mt-4 overflow-x-auto rounded-lg border-2 border-dashed border-purple-500/50 bg-dark-bg/50 p-4" aria-label="Contained tasks">
        <div className="flex min-w-max items-stretch gap-3">
          {layers.map((layer, index) => (
            <div className="flex items-center gap-3" key={layer.join("|")}>
              {index > 0 && <span className="text-2xl text-blue-400" aria-hidden="true">→</span>}
              <div className="grid w-64 gap-3">
                {layer.map((id) => {
                  const task = byId.get(id);
                  if (!task) return null;
                  return (
                    <article className="rounded-lg border border-dark-border bg-dark-surface p-3" key={id}>
                      <Link to={taskPath(projectId, id)} className="font-mono text-xs text-blue-300 hover:underline">{id}</Link>
                      <p className="mt-1 text-sm font-medium">{task.title}</p>
                      <div className="mt-2"><DependencyState task={task} compact /></div>
                    </article>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>

      {edges.length > 0 && (
        <div className="mt-4">
          <h3 className="text-sm font-semibold">Sequence arrows</h3>
          <ul className="mt-2 flex flex-wrap gap-2 text-xs">
            {edges.map((edge, index) => (
              <li className="rounded border border-blue-800 bg-blue-950/30 px-2 py-1" key={`${edge.source}-${edge.target}-${index}`}>
                <span className={!edge.source_exists ? "text-red-300" : ""}>{edge.source}</span>
                <span className="px-2 text-blue-300">→</span>
                <span className={!edge.target_exists ? "text-red-300" : ""}>{edge.target}</span>
                {edge.note && <span className="ml-2 text-dark-muted">{edge.note}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {external.length > 0 && (
        <p className="mt-3 text-xs text-dark-muted">
          Outside this umbrella: {external.map((id) => {
            const edge = edges.find((candidate) => candidate.source === id || candidate.target === id);
            const exists = edge?.source === id ? edge.source_exists : edge?.target_exists;
            return <span className={exists ? "ml-2" : "ml-2 text-red-300"} key={id}>{id}{exists ? "" : " (missing)"}</span>;
          })}
        </p>
      )}
    </section>
  );
}
