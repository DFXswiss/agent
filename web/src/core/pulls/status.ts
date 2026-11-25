// A fresh read of pull-request states (KOR-48, ADR-084 section 2): repositories, numbers and states are confidential
// and tokens never leave a device, so the browser asks a device with the request kind `github.pull_status`.
//
// - `checkPulls`: the pull requests a view shows are marked `checking` and queued per organization and workgroup;
//   one request runs at a time (the runner keeps one flow per user and kind – a second request would replace the
//   first), and the queue batches every waiting pull request of a workgroup into it (at most `PULLS_MAX`). The request
//   names the task when every pull request of the batch belongs to the same one (the device then reads with that
//   task's GitHub identity, ADR-078). A pull request that has a status is not read again unless `force` ("Check
//   again").
// - The device: the task's device when the task's main session names one the verified logs let the user address,
//   else a device assigned to the workgroup (online ones first); none is `device_not_assigned`.
// - The answer: `done` gives each pull request its `pull_state` (one the answer leaves out is `unavailable`); `failed`
//   gives every pull request of the batch the request's code (`github_not_connected`: the view offers "Connect
//   GitHub", E-05); a request the hub ended without an answer is `timeout` (expired) or `request_failed`; no end
//   within `REQUEST_MS` is `timeout` as well.
// - `mergeable` is computed by GitHub asynchronously: an open pull request read with `mergeable: null` ("being
//   checked") is read once more after `RECHECK_MS` by itself; after that only "Check again" reads it (which allows one
//   more automatic read).
// - `refreshAfterFix`: after "Conflicts fixed, branch updated" (`fixed.ts`) the pull request is read once more when the
//   read state's head is not yet the merge commit (ADR-084 section 5: followed by a fresh read).
//
// Results that arrive after a sign-out write nothing.
import { REQUEST_FAILED } from '../requests/bodies';
import { PULLS_MAX, type PullState } from './checks';
import {
  deviceKeys,
  eligibleDevices,
  latestResponse,
  prepareRequest,
  RequestError,
  requestErrorCode,
  type RequestView,
  submitRequest,
} from '../requests/client';
import { ended, watchRequest } from '../requests/watch';
import { pullStatus, type PullStatus, setStatuses, statusKey } from './state';
import type { Store } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import { type PullRef, pullKey } from './address';

/** The automatic second read of a pull request whose `mergeable` is still unknown. */
export const RECHECK_MS = 10_000;
/** A request without an end after this long counts as `timeout`. */
export const REQUEST_MS = 120_000;

/** Where a view's pull requests belong: the organization, the workgroup and, when known, the task. */
export interface StatusTarget {
  readonly org: string;
  readonly wg: string;
  readonly task: string | null;
}

export interface Timer {
  readonly start: (tick: () => void, ms: number) => unknown;
  readonly stop: (handle: unknown) => void;
}

const TIMER: Timer = {
  start: (tick, ms) => setTimeout(tick, ms),
  stop: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

interface Group {
  readonly org: string;
  readonly wg: string;
  readonly tasks: Set<string | null>;
  readonly refs: Map<string, PullRef>;
}

interface Queue {
  readonly generation: number;
  readonly groups: Map<string, Group>;
  running: boolean;
  /** Status keys that had their automatic re-check since the last asked read. */
  readonly rechecked: Set<string>;
  /** Merge commits a fresh read was asked for after the fix. */
  readonly fixes: Set<string>;
}

const queues = new WeakMap<Store, Queue>();

function queueOf(store: Store): Queue {
  const known = queues.get(store);
  if (known !== undefined && known.generation === store.generation) return known;
  const queue: Queue = {
    generation: store.generation,
    groups: new Map(),
    running: false,
    rechecked: new Set(),
    fixes: new Set(),
  };
  queues.set(store, queue);
  return queue;
}

/** The device a request goes to (module comment). */
export async function pickDevice(context: TasksContext, target: StatusTarget): Promise<string> {
  const { store } = context;
  const state = store.getState();
  const main = target.task === null ? null : (state.tasks.tasks[target.task]?.main_session_id ?? null);
  const own = main === null ? null : (state.tasks.sessions[main]?.row.device_id ?? null);
  if (own !== null && deviceKeys(store, own) !== null) return own;
  const candidates = (await eligibleDevices(context))
    .filter((item) => item.org_id === target.org && item.wg_ids.includes(target.wg))
    .filter((item) => deviceKeys(store, item.device_id) !== null)
    .sort((a, b) => Number(b.online) - Number(a.online));
  const found = candidates[0];
  if (found === undefined) throw new RequestError('device_not_assigned');
  return found.device_id;
}

/** Waits for the end of a request: the final view, or null after `REQUEST_MS`. */
function untilEnded(context: TasksContext, prepared: Parameters<typeof watchRequest>[1], timer: Timer) {
  return new Promise<RequestView | null>((resolve) => {
    // views arrive asynchronously (after a read), so the deadline is set before the first one
    const deadline: { handle?: unknown } = {};
    const stop = watchRequest(context, prepared, {
      onView: (view) => {
        if (!ended(view)) return;
        timer.stop(deadline.handle);
        resolve(view);
      },
    });
    deadline.handle = timer.start(() => {
      stop();
      resolve(null);
    }, REQUEST_MS);
  });
}

/** One `github.pull_status` request for `refs` (at most `PULLS_MAX`); the states by `pullKey`, or a failure code. */
export async function readStatuses(
  context: TasksContext,
  target: StatusTarget,
  refs: readonly PullRef[],
  timer: Timer = TIMER,
): Promise<Map<string, PullState>> {
  const device = await pickDevice(context, target);
  const body = {
    org_id: target.org,
    wg_id: target.wg,
    ...(target.task !== null && { task_id: target.task }),
    pulls: refs.map((ref) => ({ repo: ref.repo, number: ref.number })),
  };
  const prepared = await prepareRequest(context, { device, kind: 'github.pull_status', body });
  await submitRequest(context, prepared);
  const view = await untilEnded(context, prepared, timer);
  if (view === null) throw new RequestError('timeout');
  const answer = latestResponse(view)?.body;
  if (answer?.step === 'failed') throw new RequestError(answer.code);
  if (answer?.step !== 'done') throw new RequestError(view.state === 'expired' ? 'timeout' : REQUEST_FAILED);
  const wanted = new Set(refs.map(pullKey));
  return new Map(answer.pulls!.filter((item) => wanted.has(pullKey(item))).map((item) => [pullKey(item), item]));
}

/** An open pull request whose `mergeable` GitHub has not computed yet. */
export const beingChecked = (state: PullState): boolean =>
  state.found && state.state === 'open' && !state.merged && state.mergeable === null;

const groupKey = (target: StatusTarget): string => `${target.org} ${target.wg}`;

/** Queues the pull requests of a view (module comment); `recheck` marks the automatic second read. */
export function checkPulls(
  context: TasksContext,
  target: StatusTarget,
  refs: readonly PullRef[],
  options: { readonly force?: boolean; readonly timer?: Timer; readonly recheck?: boolean } = {},
): void {
  const { store } = context;
  const queue = queueOf(store);
  const pulls = store.getState().pulls;
  const fresh = refs.filter((ref) => {
    const status = pullStatus(pulls, target.wg, pullKey(ref));
    return status === null || (options.force === true && status.status !== 'checking');
  });
  if (fresh.length === 0) return;
  const key = groupKey(target);
  const group: Group = queue.groups.get(key) ?? {
    org: target.org,
    wg: target.wg,
    tasks: new Set(),
    refs: new Map(),
  };
  queue.groups.set(key, group);
  group.tasks.add(target.task);
  for (const ref of fresh) {
    const id = pullKey(ref);
    group.refs.set(id, ref);
    // an asked read allows one more automatic re-check; the re-check itself does not
    if (options.recheck !== true) queue.rechecked.delete(statusKey(target.wg, id));
  }
  setStatuses(
    store,
    target.wg,
    fresh.map((ref) => [pullKey(ref), { status: 'checking' }]),
  );
  void drain(context, queue, options.timer ?? TIMER);
}

async function drain(context: TasksContext, queue: Queue, timer: Timer): Promise<void> {
  // let the views of the same render queue their pull requests first, so one request carries them all
  await Promise.resolve();
  if (queue.running) return;
  queue.running = true;
  const { store } = context;
  try {
    for (;;) {
      const next = queue.groups.entries().next();
      if (next.done === true || store.generation !== queue.generation) return;
      const [key, group] = next.value;
      const batch = [...group.refs.entries()].slice(0, PULLS_MAX);
      for (const [id] of batch) group.refs.delete(id);
      if (group.refs.size === 0) queue.groups.delete(key);
      const tasks = [...group.tasks];
      const target = { org: group.org, wg: group.wg, task: tasks.length === 1 ? tasks[0]! : null };
      const refs = batch.map(([, ref]) => ref);
      let results: PullStatus[];
      try {
        const states = await readStatuses(context, target, refs, timer);
        const at = context.now();
        results = refs.map((ref): PullStatus => {
          const state = states.get(pullKey(ref));
          return state === undefined ? { status: 'failed', code: 'unavailable' } : { status: 'ready', state, at };
        });
      } catch (error) {
        const code = requestErrorCode(error);
        results = refs.map(() => ({ status: 'failed', code }));
      }
      if (store.generation !== queue.generation) return;
      setStatuses(
        store,
        group.wg,
        refs.map((ref, index) => [pullKey(ref), results[index]!]),
      );
      const again = refs.filter((ref, index) => {
        const result = results[index]!;
        const id = statusKey(group.wg, pullKey(ref));
        const due = result.status === 'ready' && beingChecked(result.state) && !queue.rechecked.has(id);
        if (due) queue.rechecked.add(id);
        return due;
      });
      if (again.length > 0)
        timer.start(() => checkPulls(context, target, again, { force: true, timer, recheck: true }), RECHECK_MS);
    }
  } finally {
    queue.running = false;
  }
}

/** Reads `ref` once more after a fix whose merge commit `commit` the read state does not show yet (module comment). */
export function refreshAfterFix(
  context: TasksContext,
  target: StatusTarget,
  ref: PullRef,
  commit: string,
  timer: Timer = TIMER,
): void {
  const queue = queueOf(context.store);
  const status = pullStatus(context.store.getState().pulls, target.wg, pullKey(ref));
  if (status?.status !== 'ready' || !status.state.found || status.state.head_sha === commit) return;
  const id = `${target.wg} ${commit}`;
  if (queue.fixes.has(id)) return;
  queue.fixes.add(id);
  checkPulls(context, target, [ref], { force: true, timer });
}
