// Live-channel frames for the store slices (03-api.md section 5): every slice file `src/core/store/slices/<area>.ts`
// may export `frames` = `{ <frame type>: (store, data, now) => void }` (ADR-027 section 4: discovered, no central
// list). `applyFrame` hands a frame of the org's channel (`task`, `session`, `operation`, `notification`, …, and
// `hello` after the channel processed it) to every slice that handles that type, in module order. A handler that
// throws is a bug of that slice; it must not stop the others or the channel, so it is isolated.
import type { ModuleMap } from '../registry';
import { isRecord } from '../registry';
import type { Store } from '../store/store';

export type FrameHandler = (store: Store, data: unknown, now: number) => void;
export type FrameHandlers = Readonly<Record<string, FrameHandler>>;

const sliceModules: ModuleMap = import.meta.glob(['../store/slices/*.ts', '!../store/slices/index.ts'], {
  eager: true,
});

const isHandlers = (value: unknown): value is FrameHandlers =>
  isRecord(value) && Object.values(value).every((handler) => typeof handler === 'function');

/** The `frames` exports of the slice modules, sorted by module path (modules without one are skipped). */
export function frameHandlers(modules: ModuleMap = sliceModules): FrameHandlers[] {
  return Object.keys(modules)
    .sort()
    .flatMap((path) => {
      const namespace = modules[path];
      const handlers = isRecord(namespace) ? namespace.frames : undefined;
      if (handlers === undefined) return [];
      if (!isHandlers(handlers)) throw new Error(`${path}: invalid export "frames"`);
      return [handlers];
    });
}

let discovered: FrameHandlers[] | null = null;

/** Applies one frame to the store; returns how many handlers took it. */
export function applyFrame(
  store: Store,
  type: string,
  data: unknown,
  now: number,
  handlers?: readonly FrameHandlers[],
): number {
  let applied = 0;
  for (const set of handlers ?? (discovered ??= frameHandlers())) {
    const handler = Object.hasOwn(set, type) ? set[type] : undefined;
    if (handler === undefined) continue;
    applied += 1;
    try {
      handler(store, data, now);
    } catch {
      // isolated (module comment)
    }
  }
  return applied;
}
