// Slice `usage` (06-web.md section 5.2 usage strip, ADR-010): the usage counter of every session as the hub reports it
// (`GET /v1/tasks/{task}` lists `usage` per session – `loadTask` of `core/tasks/send.ts` hands them to `putTaskUsage`,
// so the task is read once; a `session` frame carries the counter after each `usage` outbox item, HUB-14) and the task
// totals of the strip:
// - tokens: input, output and cache (read + write) summed over the task's sessions (main session and subagents);
// - cost: the sum of `cost_micro_usd`, with its basis – `provider` (OpenRouter's reported cost), `list_price` (API key,
//   catalog prices) or `subscription_equivalent` (subscription, list-price equivalent); sessions of different bases
//   name every basis;
// - context: the main session's share of its context window (last request's input + cache tokens / window).
// A counter older than the stored one (`updated_at`) is ignored (frames and answers may cross).
//
// The slice also exports `frames` (the `session` frame, `core/net/frames.ts`).
import { isRecord } from '../../registry';
import type { Slice, Store } from '../store';

export const COST_BASES = ['provider', 'list_price', 'subscription_equivalent'] as const;
export type CostBasis = (typeof COST_BASES)[number];

/** `usage` of the contracts (02-data-model.md section 2.8). */
export interface SessionUsage {
  readonly requests: number;
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly cache_read_tokens: number;
  readonly cache_write_tokens: number;
  readonly thinking_tokens: number;
  readonly cost_micro_usd: number;
  readonly cost_basis: CostBasis;
  readonly context_used_tokens: number;
  readonly context_window_tokens: number;
  readonly last_provider: string | null;
  readonly updated_at: string;
}

export interface UsageEntry {
  readonly task: string;
  readonly usage: SessionUsage;
}

export interface UsageState {
  readonly bySession: Readonly<Record<string, UsageEntry>>;
}

declare module '../store' {
  interface StoreState {
    usage: UsageState;
  }
}

export const slice: Slice<'usage'> = { name: 'usage', initial: () => ({ bySession: {} }) };

const COUNTS = [
  'requests',
  'input_tokens',
  'output_tokens',
  'cache_read_tokens',
  'cache_write_tokens',
  'thinking_tokens',
  'cost_micro_usd',
  'context_used_tokens',
  'context_window_tokens',
] as const;

const isCount = (value: unknown): boolean => Number.isSafeInteger(value) && (value as number) >= 0;

export function isSessionUsage(value: unknown): value is SessionUsage {
  return (
    isRecord(value) &&
    COUNTS.every((field) => isCount(value[field])) &&
    (COST_BASES as readonly unknown[]).includes(value.cost_basis) &&
    (value.last_provider === null || typeof value.last_provider === 'string') &&
    typeof value.updated_at === 'string' &&
    Number.isFinite(Date.parse(value.updated_at))
  );
}

/** Stores the counter of `session` (of `task`) unless the stored one is newer. */
export function putUsage(store: Store, task: string, session: string, usage: SessionUsage): void {
  store.update('usage', (state) => {
    const known = state.bySession[session];
    if (known !== undefined && Date.parse(known.usage.updated_at) > Date.parse(usage.updated_at)) return state;
    return { bySession: { ...state.bySession, [session]: { task, usage } } };
  });
}

const sessionIds = (value: unknown): { session: string; task: string } | null =>
  isRecord(value) && typeof value.session_id === 'string' && typeof value.task_id === 'string'
    ? { session: value.session_id, task: value.task_id }
    : null;

/** The counters of the `sessions` items of `GET /v1/tasks/{task}` (malformed items and other tasks are skipped). */
export function putTaskUsage(store: Store, task: string, sessions: readonly unknown[]): void {
  for (const item of sessions) {
    const ids = isRecord(item) ? sessionIds(item.session) : null;
    if (ids === null || ids.task !== task || !isSessionUsage((item as Record<string, unknown>).usage)) continue;
    putUsage(store, task, ids.session, (item as Record<string, unknown>).usage as SessionUsage);
  }
}

export interface TaskUsage {
  readonly sessions: number;
  readonly input: number;
  readonly output: number;
  readonly cache: number;
  readonly costUsd: number;
  /** The bases of the summed costs, in the order of `COST_BASES`. */
  readonly bases: readonly CostBasis[];
  /** The main session's share of its context window (0…1), null without a counter or window. */
  readonly context: number | null;
}

/** The totals of `task` (module comment), or null while no session of it has a counter. */
export function taskUsage(state: UsageState, task: string, mainSession: string | null): TaskUsage | null {
  const entries = Object.values(state.bySession).filter((entry) => entry.task === task);
  if (entries.length === 0) return null;
  const sum = (pick: (usage: SessionUsage) => number): number =>
    entries.reduce((total, entry) => total + pick(entry.usage), 0);
  const bases = COST_BASES.filter((basis) => entries.some((entry) => entry.usage.cost_basis === basis));
  const main = mainSession === null ? undefined : state.bySession[mainSession]?.usage;
  const context =
    main === undefined || main.context_window_tokens === 0
      ? null
      : main.context_used_tokens / main.context_window_tokens;
  return {
    sessions: entries.length,
    input: sum((usage) => usage.input_tokens),
    output: sum((usage) => usage.output_tokens),
    cache: sum((usage) => usage.cache_read_tokens + usage.cache_write_tokens),
    costUsd: sum((usage) => usage.cost_micro_usd) / 1_000_000,
    bases,
    context,
  };
}

/** The frames this slice applies (`core/net/frames.ts`): `session` with a usage counter. */
export const frames: Readonly<Record<string, (store: Store, data: unknown) => void>> = {
  session: (store, data) => {
    if (!isRecord(data) || !isSessionUsage(data.usage)) return;
    const ids = sessionIds(data.session);
    if (ids !== null) putUsage(store, ids.task, ids.session, data.usage);
  },
};
