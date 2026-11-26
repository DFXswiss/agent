// What the mounted views show (ADR-086 section 1, 06-web.md section 2.3): a reference-counted registry of the tasks
// and sessions an open view displays. The event reader asks it before it decrypts a live event of a bulk kind
// (`BULK_KINDS`): a bulk event of a task or session no view shows is neither decrypted nor stored – only the live
// channel's cursor moves – and the registry counts per task and session what was skipped, so that the view that
// opens it later reads its newest page again (`skipMark`).
//
// A view registers with `show(store, {scope, task?, session?, key?, refresh?})` while it is mounted and calls the
// returned function when it unmounts; registrations of the same target add up. `key` names its paged list (the event
// store keeps what a paged-back list looks at, `shownKeys`). `refresh` is called after the live channel's `resync` (the
// hub dropped frames): the view reads its newest page again (`refreshShown`).
//
// The registry is kept per store and is not part of the store's state (a registration does not re-render anything);
// a sign-out (`store.reset`) keeps the registrations of still mounted views but forgets what was skipped.
import type { Store } from './store';

/** Event kinds of a session that can arrive in masses (ADR-086 section 1: the bulk class of the event store). */
export const BULK_KINDS: ReadonlySet<string> = new Set([
  'session.output',
  'session.tool',
  'session.file',
  'session.hook',
  'session.terminal',
  'usage',
  // the jobs of a session's work VM (KOR-29): one per command, written at its start and end
  'workvm.run',
  // the test runs of a session (KOR-43): one per run, written at its start and end
  'test.run',
  // every action of a session's browser (ADR-085): read by the record view, never pulled
  'browser.action',
]);

/** Every other event kind (contracts/enums/event-kinds.json without `BULK_KINDS`): what `resync` pulls. */
export const OTHER_KINDS: readonly string[] = [
  // the state of a session's browser (ADR-085): one per open and close, never in masses
  'browser.state',
  'device.profile',
  // an admin-given name of a member (ADR-073): one per naming, never in masses
  'member.name',
  'member.profile',
  'operation.params',
  'operation.result',
  'org.profile',
  'platform.read',
  'prompt',
  // a session's failure: one per failure, never in masses
  'session.error',
  'session.status',
  'settings.workgroup',
  // the name a person gives a main agent (ADR-095 section 2): one per renaming, never in masses
  'task.agent_name',
  // the names a support person needs (ADR-096): one per release or change, never in masses
  'support.info',
  'task.meta',
  'wg.profile',
  'whiteboard.delete',
  'whiteboard.edit',
  'whiteboard.entry',
  'workvm.state',
];

export interface FocusTarget {
  /** The scope the view reads (the task's workgroup). */
  readonly scope: string;
  readonly task?: string;
  readonly session?: string;
  /** The key of the view's paged list (`core/tasks/pages.ts` `pagesKey`): the event store keeps what it looks at. */
  readonly key?: string;
  /** Reads the view's newest page again (after `resync`). */
  readonly refresh?: () => unknown;
}

interface Registration {
  readonly target: FocusTarget;
}

class Focus {
  readonly registrations = new Set<Registration>();
  readonly tasks = new Map<string, number>();
  readonly sessions = new Map<string, number>();
  /** Skipped bulk events by task and session ID (prefixed `t:`/`s:`), in the current store generation. */
  skipped = new Map<string, number>();
  generation = 0;
}

const registries = new WeakMap<Store, Focus>();

function focusOf(store: Store): Focus {
  let focus = registries.get(store);
  if (focus === undefined) {
    focus = new Focus();
    focus.generation = store.generation;
    registries.set(store, focus);
  }
  if (focus.generation !== store.generation) {
    focus.generation = store.generation;
    focus.skipped = new Map();
  }
  return focus;
}

const count = (map: Map<string, number>, key: string | undefined, delta: number): void => {
  if (key === undefined) return;
  const next = (map.get(key) ?? 0) + delta;
  if (next > 0) map.set(key, next);
  else map.delete(key);
};

/** Registers a mounted view (module comment); the returned function ends this registration (once). */
export function show(store: Store, target: FocusTarget): () => void {
  const focus = focusOf(store);
  const registration: Registration = { target };
  focus.registrations.add(registration);
  count(focus.tasks, target.task, 1);
  count(focus.sessions, target.session, 1);
  return () => {
    if (!focus.registrations.delete(registration)) return;
    count(focus.tasks, target.task, -1);
    count(focus.sessions, target.session, -1);
  };
}

/** Whether a view shows the task or the session (an event names both; either is enough). */
export function isShown(store: Store, task: unknown, session: unknown): boolean {
  const focus = focusOf(store);
  return (
    (typeof task === 'string' && focus.tasks.has(task)) || (typeof session === 'string' && focus.sessions.has(session))
  );
}

/** Whether a live event of `kind` for this task/session is skipped (a bulk kind no view shows). */
export function skips(store: Store, kind: unknown, task: unknown, session: unknown): boolean {
  return typeof kind === 'string' && BULK_KINDS.has(kind) && !isShown(store, task, session);
}

/** Remembers that a bulk event of the task/session was skipped. */
export function noteSkipped(store: Store, task: unknown, session: unknown): void {
  const focus = focusOf(store);
  for (const key of [
    typeof task === 'string' ? `t:${task}` : null,
    typeof session === 'string' ? `s:${session}` : null,
  ]) {
    if (key !== null) focus.skipped.set(key, (focus.skipped.get(key) ?? 0) + 1);
  }
}

/**
 * A mark of what was skipped of the task or session: it changes whenever a bulk event of them is skipped, so a view
 * that kept the mark of its last newest page knows whether to read it again.
 */
export function skipMark(store: Store, target: { readonly task?: string; readonly session?: string }): string {
  const focus = focusOf(store);
  const task = target.task === undefined ? 0 : (focus.skipped.get(`t:${target.task}`) ?? 0);
  const session = target.session === undefined ? 0 : (focus.skipped.get(`s:${target.session}`) ?? 0);
  return `${focus.generation}:${task}:${session}`;
}

/** The scopes the mounted views read. */
export function shownScopes(store: Store): Set<string> {
  return new Set([...focusOf(store).registrations].map((registration) => registration.target.scope));
}

/** The keys of the paged lists the mounted views show. */
export function shownKeys(store: Store): Set<string> {
  const keys = new Set<string>();
  for (const registration of focusOf(store).registrations) {
    if (registration.target.key !== undefined) keys.add(registration.target.key);
  }
  return keys;
}

/**
 * After `resync`: the bulk events of the shown tasks and sessions were not pulled, so they count as skipped, and
 * `refresh` of every mounted view reads its newest page; failures are the views' business.
 */
export function refreshShown(store: Store): void {
  for (const registration of [...focusOf(store).registrations]) {
    noteSkipped(store, registration.target.task, registration.target.session);
    try {
      void Promise.resolve(registration.target.refresh?.()).catch(() => undefined);
    } catch {
      // a throwing refresh must not stop the others
    }
  }
}
