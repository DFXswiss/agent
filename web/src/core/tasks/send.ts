// The send sequence of a prompt (06-web.md section 4.3, 03-api.md section 4.6.1): every send creates a task.
//
// 1. `prepareTask`: the title (`title.ts`), `task.meta` and `prompt` sealed with the workgroup's current key at
//    `lh` = the verified head, a fresh `task_id`. The prepared body is kept by the caller: a retry sends the identical
//    request (same IDs, same bytes), which the hub answers `replayed` (03-api.md section 1). An optional GitHub
//    identity chosen for the task goes along as `github_identity` (ADR-078 section 3; without it: automatic). An
//    analysis task (ADR-097, `core/analysis/`) brings its fixed `title` instead of the derived one and its plaintext
//    `analysis` (scope and period) for the body.
// 2. `ensureDevice` (step 1 of the first-task flow): if the workgroup has no assigned device, an admin of an org with
//    platform access (ADR-026; in a private org the single member is the admin) assigns the first platform candidate
//    that the verified platform log certifies with the same keys (`GET …/device-candidates`, compiled root key): one
//    `POST /v1/orgs/{org}/log` batch `device_assign` + the wraps of the current `WK` and `OK` for the device (built by
//    `buildBatch` from the verified log, keys from the platform log, ADR-007), then the wrap of the user's own `CK`
//    for the device (`POST /v1/wraps`: the hub stores `cred` wraps only there, decision WEB-04). Anyone else, or no
//    candidate: nothing is assigned and the task is created anyway – it waits for a device (ADR-075 section 7) and the
//    countdown offers "Assign a device" (`assignFirstCandidate`, which refuses with `no_device_available`).
// 3. `sendTask`: `POST /v1/tasks` with the chosen `placement` (device and optionally VM; without one the workgroup's
//    default applies, ADR-075 section 6). The answer's rows enter the `tasks` slice, the sealed events the reader
//    (title and prompt are readable at once), and the launch is recorded for the countdown.
//
// `sendFollowUp` seals another `prompt` of a task (`POST /v1/tasks/{task}/prompts`, section 4.6.3); `retryPrompt` is
// the countdown's "Retry" after `failed`: the task's newest prompt again as a follow-up. A prompt names the files
// attached to it (ADR-102, `core/attachments/transfer.ts`) in its `attachments`; they are committed at the hub before
// the prompt is sealed, and a retry names the same objects again.
import { b64uDecode } from '../encoding/b64u';
import { bodyJson } from '../encoding/json';
import { uuid7 } from '../encoding/ids';
import { formatTime } from '../encoding/time';
import { type AttachmentRef, checkRefs } from '../crypto/attachment';
import { CryptoError } from '../crypto/errors';
import { newHeader, sealEvent } from '../crypto/event';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import { signWrap, unwrapKey, verifyWrap, wrapKey } from '../crypto/wrap';
import type { PlatformState } from '../log/platform';
import { buildBatch } from '../log/rotation';
import { epochKey, ORG, type OrgState, WG } from '../log/state';
import type { VerifiedLog } from '../log/verify';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { findEvents } from '../store/slices/events';
import { orgEntry, setLog } from '../store/slices/orgs';
import { putTaskUsage } from '../store/slices/usage';
import {
  isSessionRow,
  isTaskRow,
  isTransition,
  putLaunch,
  putSession,
  putTasks,
  type TaskRow,
} from '../store/slices/tasks';
import type { Store } from '../store/store';
import type { TasksContext } from './pull';
import { pullEvents } from './pull';
import type { Placement } from './placement';
import { deriveTitle } from './title';

/** Prompts are 1–65 536 characters (`contracts/events/prompt.json`). */
export const PROMPT_MAX = 65536;
/** Attempts of a log batch that meets `seq_conflict` (the log is re-read and the batch rebuilt each time). */
export const ASSIGN_ATTEMPTS = 3;

/** A refused send with a catalog code (`forbidden`, `epoch_stale`, hub and crypto codes; `no_device_available` of
 *  `assignFirstCandidate`). */
export class SendError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = 'SendError';
    this.code = code;
  }
}

/** The catalog code of any failure of a send. */
export function sendErrorCode(error: unknown): string {
  if (error instanceof SendError || error instanceof HubError || error instanceof CryptoError) return error.code;
  return 'unavailable';
}

interface Writer {
  readonly userId: string;
  readonly identity: Identity;
  readonly log: VerifiedLog;
}

/** The signed-in user, the unlocked identity and the verified log of `org` (a blocked org refuses every write). */
function writer(store: Store, org: string): Writer {
  const { userId, identity } = store.getState().me;
  const entry = orgEntry(store.getState().orgs, org);
  if (userId === null || identity === null) throw new SendError('unauthorized');
  if (entry.blocked !== null) throw new SendError(entry.blocked.code);
  if (entry.log === null) throw new SendError('log_empty');
  return { userId, identity, log: entry.log };
}

/** Workgroups in which the user may create tasks (`wg:write`), the default workgroup first, then by ID. */
export function writableWorkgroups(state: OrgState | null, userId: string | null): string[] {
  if (state === null || userId === null) return [];
  return [...state.workgroups.keys()]
    .filter((wg) => state.canWriteWg(userId, wg))
    .sort((a, b) => Number(b === state.defaultWg) - Number(a === state.defaultWg) || Number(a > b) - Number(a < b));
}

/** A prompt the composer may send: not only whitespace and at most `PROMPT_MAX` characters. */
export function sendable(prompt: string): boolean {
  return deriveTitle(prompt) !== '' && prompt.length <= PROMPT_MAX;
}

/** The current key of a workgroup; asks the log and wrap jobs once if it is not in memory yet. */
export async function workgroupKey(context: TasksContext, org: string, wg: string): Promise<[number, Secret]> {
  const lookup = (): [number, Secret] | null => {
    const entry = orgEntry(context.store.getState().orgs, org);
    const epoch = entry.log!.state.epoch(WG, wg);
    const key = entry.keys.get(epochKey(WG, wg, epoch));
    return key === undefined ? null : [epoch, key];
  };
  const known = lookup();
  if (known !== null) return known;
  await context.refreshLog(org);
  return lookup() ?? fail('epoch_stale');
}

function fail(code: string): never {
  throw new SendError(code);
}

/** A sealed task-level event of the user (`task.meta`, `prompt`, `task.agent_name`) of a task in workgroup `wg`. */
export async function sealWg(
  context: TasksContext,
  org: string,
  wg: string,
  task: string,
  kind: 'task.meta' | 'prompt' | 'task.agent_name',
  body: unknown,
): Promise<unknown> {
  const { userId, identity, log } = writer(context.store, org);
  const [epoch, key] = await workgroupKey(context, org, wg);
  const now = context.now();
  const header = newHeader({
    id: uuid7(now),
    org,
    scope: WG,
    scope_id: wg,
    epoch,
    author: userId,
    kind,
    ts: formatTime(now),
    lh: log.head.seq,
    wg,
    task,
  });
  return sealEvent(key, header, bodyJson(body), identity.sig.privateKey);
}

/** The plaintext `analysis` of `POST /v1/tasks` (ADR-097 section 1). */
export interface TaskAnalysis {
  readonly scope: 'org' | 'wg';
  readonly since: string;
  readonly until: string;
}

/** A task ready to send; keep it to retry with the same IDs. */
export interface PreparedTask {
  readonly org: string;
  readonly wg: string;
  readonly taskId: string;
  readonly prompt: string;
  /** The files the prompt names (ADR-102; committed at the hub before the prompt was sealed). */
  readonly attachments: readonly AttachmentRef[];
  readonly title: string;
  /** The chosen device and VM (null: the workgroup default, else automatic). */
  readonly placement: Placement | null;
  readonly body: {
    readonly task_id: string;
    readonly wg_id: string;
    readonly events: readonly unknown[];
    readonly placement?: { readonly device_id: string; readonly vm_id?: string };
    /** The GitHub identity chosen for the task (ADR-078 section 3); absent for "automatic". */
    readonly github_identity?: string;
    /** Scope and period of an analysis task (ADR-097 section 1); absent for every other task. */
    readonly analysis?: TaskAnalysis;
  };
}

/**
 * Step 1: title, sealed `task.meta` and `prompt`, a fresh task ID (`forbidden` without `wg:write`); `placement` is the
 * chosen device and VM (null or absent: the workgroup default, else automatic); `title` replaces the derived title
 * (an analysis task's fixed title) and `analysis` goes into the body.
 */
export async function prepareTask(
  context: TasksContext,
  input: {
    readonly org: string;
    readonly wg: string;
    readonly prompt: string;
    readonly attachments?: readonly AttachmentRef[];
    readonly placement?: Placement | null;
    readonly githubIdentity?: string;
    readonly title?: string;
    readonly analysis?: TaskAnalysis;
  },
): Promise<PreparedTask> {
  const { userId, log } = writer(context.store, input.org);
  if (!log.state.canWriteWg(userId, input.wg)) fail('forbidden');
  if (!sendable(input.prompt)) fail(input.prompt.length > PROMPT_MAX ? 'size_exceeded' : 'request_invalid');
  const attachments = input.attachments ?? [];
  const title = input.title ?? deriveTitle(input.prompt);
  const taskId = uuid7(context.now());
  const meta = await sealWg(context, input.org, input.wg, taskId, 'task.meta', { title });
  const prompt = await sealWg(context, input.org, input.wg, taskId, 'prompt', { text: input.prompt, attachments });
  return {
    org: input.org,
    wg: input.wg,
    taskId,
    prompt: input.prompt,
    attachments,
    title,
    placement: input.placement ?? null,
    body: {
      task_id: taskId,
      wg_id: input.wg,
      events: [meta, prompt],
      ...(input.placement && {
        placement: {
          device_id: input.placement.device_id,
          ...(input.placement.vm_id !== null && { vm_id: input.placement.vm_id }),
        },
      }),
      ...(input.githubIdentity !== undefined && { github_identity: input.githubIdentity }),
      ...(input.analysis !== undefined && { analysis: input.analysis }),
    },
  };
}

// --- step 2: the execution device ---

/** A platform candidate (`GET /v1/workgroups/{wg}/device-candidates`). */
interface Candidate {
  readonly device_id: string;
  readonly platform_seq: number;
  readonly pub: { readonly kem: string; readonly sig: string };
}

const isCandidate = (value: unknown): value is Candidate =>
  isRecord(value) &&
  typeof value.device_id === 'string' &&
  Number.isSafeInteger(value.platform_seq) &&
  isRecord(value.pub) &&
  typeof value.pub.kem === 'string' &&
  typeof value.pub.sig === 'string';

/** Whether the verified platform log certifies the candidate, still valid and with the keys the hub listed. */
export function certified(platform: PlatformState, candidate: Candidate): boolean {
  const found = platform.certification(candidate.platform_seq);
  return (
    found !== null &&
    found.device === candidate.device_id &&
    platform.valid(candidate.platform_seq) &&
    found.kem === candidate.pub.kem &&
    found.sig === candidate.pub.sig
  );
}

async function assignedDevices(context: TasksContext, wg: string): Promise<number> {
  const body = await context.client.request('GET', `/v1/workgroups/${encodeURIComponent(wg)}`);
  if (!isRecord(body) || !Array.isArray(body.devices)) throw new HubError('unavailable', 200);
  return body.devices.length;
}

/**
 * Wraps the user's current `CK` for a newly assigned device (best effort: the device needs it only for the user's own
 * credentials, and catch-up wrapping offers the slot again later). Uses the newest own `cred` wrap that the user made
 * and that opens.
 */
export async function wrapCredentialKey(
  context: TasksContext,
  writerInfo: { readonly userId: string; readonly identity: Identity },
  device: { readonly id: string; readonly kem: string },
): Promise<boolean> {
  const { userId, identity } = writerInfo;
  try {
    const query = `/v1/wraps?scope=cred&scope_id=${encodeURIComponent(userId)}`;
    const body = await context.client.request('GET', query);
    const wraps = isRecord(body) && Array.isArray(body.wraps) ? body.wraps.filter(isRecord) : [];
    const own = wraps
      .filter(
        (w) => w.scope === 'cred' && w.scope_id === userId && w.recipient_id === userId && w.wrapped_by === userId,
      )
      .filter((w) => Number.isSafeInteger(w.epoch) && typeof w.wrap === 'string')
      .sort((a, b) => (b.epoch as number) - (a.epoch as number));
    for (const found of own) {
      const slot = { scope: 'cred', scope_id: userId, epoch: found.epoch as number, recipient: userId };
      try {
        await verifyWrap(identity.sigPublic(), slot, found.wrap as string, found.sig);
        const key = await unwrapKey(identity.kem, slot, found.wrap as string);
        const target = { ...slot, recipient: device.id };
        const wrap = await wrapKey(b64uDecode(device.kem), target, key);
        const sig = await signWrap(identity.sig.privateKey, target, wrap);
        const record = { scope: 'cred', scope_id: userId, epoch: slot.epoch, recipient_id: device.id, wrap, sig };
        await context.client.request('POST', '/v1/wraps', { body: { wraps: [record] } });
        return true;
      } catch (error) {
        if (error instanceof HubError) return false;
      }
    }
    return false;
  } catch {
    return false;
  }
}

/** The keys the user holds in `org` (for the wraps of the batch). */
function keyRing(store: Store, org: string) {
  const keys = orgEntry(store.getState().orgs, org).keys;
  return (scope: string, scopeId: string, epoch: number) => keys.get(epochKey(scope, scopeId, epoch)) ?? null;
}

/**
 * Step 2: makes sure the workgroup has an assigned device where the user may assign one. Returns null if one was
 * assigned already or none can be assigned (the task then waits for a device, ADR-075 section 7), else the time the
 * automatic assignment took (ms). `onAssigning` is called when the assignment starts (the countdown's first step
 * "Assigning execution device").
 */
export async function ensureDevice(
  context: TasksContext,
  org: string,
  wg: string,
  onAssigning?: () => void,
): Promise<number | null> {
  if ((await assignedDevices(context, wg)) > 0) return null;
  try {
    return await assignFirstCandidate(context, org, wg, onAssigning);
  } catch (error) {
    if (error instanceof SendError && error.code === 'no_device_available') return null;
    throw error;
  }
}

/**
 * Assigns the first platform candidate that the verified platform log certifies to the workgroup (step 2 of the send
 * sequence; "Assign a device" of a task that waits with `no_device`). Returns the time it took (ms);
 * `no_device_available` when the user is no admin, the org has no platform access or there is no candidate.
 */
export async function assignFirstCandidate(
  context: TasksContext,
  org: string,
  wg: string,
  onAssigning?: () => void,
): Promise<number> {
  const started = context.now();
  const { store } = context;
  // the platform log is verified by the log job: a new org has not run it yet, a newly certified device needs a
  // fresh run – once per assignment
  let refreshed = false;
  const platformState = async (): Promise<PlatformState | null> => {
    const known = store.getState().orgs.platform?.state ?? null;
    if (known !== null || refreshed) return known;
    refreshed = true;
    await context.refreshLog(org);
    return store.getState().orgs.platform?.state ?? null;
  };
  for (let attempt = 1; ; attempt += 1) {
    const admin = writer(store, org);
    if (!admin.log.state.isAdmin(admin.userId)) fail('no_device_available');
    if (attempt === 1) onAssigning?.();
    let platform = await platformState();
    if (platform === null || !platform.platformAccess(org, admin.log.state.private)) fail('no_device_available');
    const body = await context.client.request('GET', `/v1/workgroups/${encodeURIComponent(wg)}/device-candidates`);
    const offered = isRecord(body) && Array.isArray(body.candidates) ? body.candidates.filter(isCandidate) : [];
    let candidate = offered.find((item) => certified(platform!, item));
    if (candidate === undefined && !refreshed) {
      refreshed = true;
      await context.refreshLog(org);
      platform = store.getState().orgs.platform!.state;
      candidate = offered.find((item) => certified(platform!, item));
    }
    if (candidate === undefined) fail('no_device_available');
    const { userId, identity, log } = writer(store, org);
    // the device gets the current WK and OK from this batch (ADR-007): the user must hold both
    const held = keyRing(store, org);
    const state = log.state;
    if (held(ORG, org, state.epoch(ORG, org)) === null || held(WG, wg, state.epoch(WG, wg)) === null) {
      fail('epoch_stale');
    }
    const built = await buildBatch({
      log,
      author: { id: userId, identity },
      entries: [{ type: 'device_assign', device: candidate.device_id, wg, platform_seq: candidate.platform_seq }],
      keys: held,
      platform,
      ts: formatTime(context.now()),
    });
    try {
      const answer = await context.client.request('POST', `/v1/orgs/${encodeURIComponent(org)}/log`, {
        body: built.body,
      });
      const head = isRecord(answer) && isRecord(answer.head) ? answer.head : null;
      if (head?.seq !== built.log.head.seq || head.hash !== built.log.head.hash) throw new HubError('unavailable', 200);
    } catch (error) {
      const code = error instanceof HubError ? error.code : null;
      if (code === 'already_assigned' && (await assignedDevices(context, wg)) > 0) return context.now() - started;
      if (code !== 'seq_conflict' || attempt >= ASSIGN_ATTEMPTS) throw error;
      await context.refreshLog(org);
      if ((await assignedDevices(context, wg)) > 0) return context.now() - started;
      continue;
    }
    setLog(store, org, built.log);
    const kem = platform.certification(candidate.platform_seq)!.kem;
    await wrapCredentialKey(context, { userId, identity }, { id: candidate.device_id, kem });
    return context.now() - started;
  }
}

// --- step 3: the task ---

/** What `POST /v1/tasks` created. */
export interface SentTask {
  readonly task: TaskRow;
  readonly sessionId: string;
  readonly estimateMs: number | null;
  readonly replayed: boolean;
}

interface EventResult {
  readonly id: string;
  readonly scope_id: string;
  readonly scope_seq: number;
}

const isEventResult = (value: unknown): value is EventResult =>
  isRecord(value) &&
  typeof value.id === 'string' &&
  typeof value.scope_id === 'string' &&
  Number.isSafeInteger(value.scope_seq);

/** Hands own sealed events with the positions the hub gave them to the reader. */
async function readOwn(
  context: TasksContext,
  org: string,
  events: readonly unknown[],
  results: readonly unknown[],
): Promise<void> {
  const byScope = new Map<string, { scope_seq: number; received_at: null; event: unknown }[]>();
  events.forEach((event, index) => {
    const result = results[index];
    if (!isEventResult(result)) return;
    const items = byScope.get(result.scope_id) ?? [];
    items.push({ scope_seq: result.scope_seq, received_at: null, event });
    byScope.set(result.scope_id, items);
  });
  for (const [scope, items] of byScope) await context.ingest(org, scope, items);
}

async function postTask(context: TasksContext, prepared: PreparedTask): Promise<Record<string, unknown>> {
  const answer = await context.client.request('POST', '/v1/tasks', { body: prepared.body });
  if (!isRecord(answer) || !isTaskRow(answer.task) || !isRecord(answer.session)) throw new HubError('unavailable', 200);
  return answer;
}

/** Steps 2 and 3 for a prepared task (send it again unchanged to retry). */
export async function sendTask(
  context: TasksContext,
  prepared: PreparedTask,
  options: { readonly onAssigning?: () => void } = {},
): Promise<SentTask> {
  const assignedMs = await ensureDevice(context, prepared.org, prepared.wg, options.onAssigning);
  const answer = await postTask(context, prepared);
  const task = answer.task as TaskRow;
  const session = answer.session as Record<string, unknown>;
  if (task.task_id !== prepared.taskId || typeof session.session_id !== 'string') {
    throw new HubError('unavailable', 200);
  }
  const estimateMs = Number.isSafeInteger(session.estimate_ms) ? (session.estimate_ms as number) : null;
  putTasks(context.store, [task]);
  putLaunch(context.store, task.task_id, {
    sessionId: session.session_id,
    localAt: context.now(),
    estimateMs,
    assignedMs,
  });
  await readOwn(context, prepared.org, prepared.body.events, Array.isArray(answer.events) ? answer.events : []);
  return { task, sessionId: session.session_id, estimateMs, replayed: answer.replayed === true };
}

/** Loads a task with its sessions and transitions (`GET /v1/tasks/{task}`) into the `tasks` slice. */
export async function loadTask(context: TasksContext, task: string): Promise<TaskRow> {
  const generation = context.store.generation;
  const body = await context.client.request('GET', `/v1/tasks/${encodeURIComponent(task)}`);
  if (!isRecord(body) || !isTaskRow(body.task) || !Array.isArray(body.sessions)) throw new HubError('unavailable', 200);
  // an answer after a sign-out writes nothing into the next user's store (KOR-11: reloads run after every reconnect)
  if (context.store.generation !== generation) return body.task;
  putTasks(context.store, [body.task]);
  putTaskUsage(context.store, body.task.task_id, body.sessions);
  for (const item of body.sessions) {
    if (!isRecord(item) || !isSessionRow(item.session)) continue;
    const transitions = Array.isArray(item.transitions) ? item.transitions.filter(isTransition) : [];
    putSession(context.store, item.session, transitions, context.now());
  }
  return body.task;
}

// --- follow-ups ---

/** Seals `text` with the files it names (ADR-102) as a further `prompt` of `task`. */
export async function prepareFollowUp(
  context: TasksContext,
  task: TaskRow,
  text: string,
  attachments: readonly AttachmentRef[] = [],
): Promise<unknown> {
  if (!sendable(text)) fail(text.length > PROMPT_MAX ? 'size_exceeded' : 'request_invalid');
  return sealWg(context, task.org_id, task.wg_id, task.task_id, 'prompt', { text, attachments });
}

/**
 * Sends a further prompt of `task` (section 4.6.3; `event` from `prepareFollowUp`, sent again unchanged to retry); the
 * countdown follows the session the hub names.
 */
export async function sendFollowUp(context: TasksContext, task: TaskRow, event: unknown): Promise<string> {
  const answer = await context.client.request('POST', `/v1/tasks/${encodeURIComponent(task.task_id)}/prompts`, {
    body: { event },
  });
  if (!isRecord(answer) || typeof answer.session_id !== 'string' || !Number.isSafeInteger(answer.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  putLaunch(context.store, task.task_id, {
    sessionId: answer.session_id,
    localAt: context.now(),
    estimateMs: null,
    assignedMs: null,
  });
  await context.ingest(task.org_id, task.wg_id, [{ scope_seq: answer.scope_seq as number, received_at: null, event }]);
  return answer.session_id;
}

/** The newest verified prompt of a task in the store (bodies without text are skipped), or null: its text and the
 *  files it names (none when its `attachments` are not valid references, ADR-102). */
export function latestPromptBody(
  store: Store,
  task: TaskRow,
): { readonly text: string; readonly attachments: readonly AttachmentRef[] } | null {
  const texts = findEvents(
    store.getState().events,
    (entry) =>
      entry.scopeId === task.wg_id &&
      entry.status === 'verified' &&
      entry.header!.kind === 'prompt' &&
      entry.header!.task === task.task_id &&
      isRecord(entry.body) &&
      typeof entry.body.text === 'string',
  ).sort((a, b) => b.scopeSeq - a.scopeSeq);
  if (texts.length === 0) return null;
  const body = texts[0]!.body as { text: string; attachments?: unknown };
  return { text: body.text, attachments: promptAttachments(body) };
}

/** The text of the newest verified prompt of a task in the store, or null (`latestPromptBody`). */
export function latestPrompt(store: Store, task: TaskRow): string | null {
  return latestPromptBody(store, task)?.text ?? null;
}

/** The checked attachment references of a prompt body; none when they are missing or not valid. */
export function promptAttachments(body: unknown): readonly AttachmentRef[] {
  if (!isRecord(body)) return [];
  try {
    return checkRefs(body.attachments);
  } catch {
    return [];
  }
}

const retries = new WeakMap<Store, Map<string, { readonly generation: number; readonly event: unknown }>>();

/**
 * "Retry" after a failed start: the task's newest prompt once more as a follow-up (pulled if not read yet). The sealed
 * event is kept until the hub took it, so a retry after a lost answer re-sends the same event (same ID, 06-web.md
 * section 12.5).
 */
export async function retryPrompt(context: TasksContext, task: TaskRow): Promise<string> {
  const { store } = context;
  const pending = retries.get(store) ?? new Map<string, { generation: number; event: unknown }>();
  retries.set(store, pending);
  let kept = pending.get(task.task_id);
  if (kept === undefined || kept.generation !== store.generation) {
    let latest = latestPromptBody(store, task);
    if (latest === null) {
      await pullEvents(context, task.org_id, task.wg_id, { kinds: ['prompt'], task: task.task_id });
      latest = latestPromptBody(store, task) ?? fail('decrypt_failed');
    }
    kept = {
      generation: store.generation,
      event: await prepareFollowUp(context, task, latest.text, latest.attachments),
    };
    pending.set(task.task_id, kept);
  }
  const session = await sendFollowUp(context, task, kept.event);
  pending.delete(task.task_id);
  return session;
}
