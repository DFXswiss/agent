// Pull-request links of a workgroup (KOR-48, ADR-084 section 1, 03-api.md section 4, 04-crypto.md section 4.11a):
//
// - `loadLinks`: `GET /v1/workgroups/{wg}/pr-links?task=&cursor=&limit=` (newest first, `LIST_LIMIT` per page, the
//   next page with the answer's `next_cursor`) into the `pulls` slice; failures are recorded in the list, never
//   thrown. `ensureLinks` loads every page of a workgroup (at most `MAX_PAGES`) once per store generation: the
//   situation view needs to know which of its tasks have links. A hub must not slip another task's link into a task's
//   list: rows of other tasks are left out.
// - `openLink`: a row as the browser shows it. The sealed body is opened with the workgroup key of the row's epoch,
//   bound to the row's workgroup, epoch, tag, task and origin (`device:<session_id>` or `user:<linked_by>`) and its
//   tag recomputed (`openPrLink`): a hub can move a link to no other task, workgroup, origin or pull request. A row
//   without a body (an older runner: `legacy`), without the epoch's key or whose body does not open keeps its
//   metadata and shows "details unavailable" with its task.
// - `findLinks`: the links of a pasted pull request under every epoch of the workgroup the browser holds (the tag of
//   each epoch, `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`; `404` is none) – a pull request linked already is
//   shown with its task, never moved silently (idea 40).
// - `linkPull`: when no link exists, `POST /v1/workgroups/{wg}/pr-links {epoch, tag, sealed, task_id}` under the
//   workgroup's current epoch with the origin `user:<me>` (`wg:write`); `201 {created:true, link}` or, for a tag
//   linked meanwhile, `200 {created:false, link}`.
// - `unlinkPull`: `DELETE /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` of a person's link (`404` counts as done; the
//   hub keeps a device's link, `409 id_conflict`).
import { openPrLink, prLinkBodyKey, prLinkKey, prLinkTag, sealPrLink } from '../crypto/context';
import type { Secret } from '../crypto/secret';
import { isUuid7 } from '../encoding/ids';
import { epochKey, WG } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { requestErrorCode } from '../requests/client';
import { orgEntry } from '../store/slices/orgs';
import { addLink, linkList, listFailed, listKey, listLoading, listPage, type PullLink, removeLink } from './state';
import type { Store } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import { sendErrorCode } from '../tasks/send';
import type { PullRef } from './address';

/** Links per page of a list (the hub's default; at most 200). */
export const LIST_LIMIT = 100;
/** `ensureLinks` reads pages of this size … */
export const ALL_LIMIT = 200;
/** … and at most this many of them. */
export const MAX_PAGES = 10;

/** A refused link step with a catalog code. */
export class LinkError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = 'LinkError';
    this.code = code;
  }
}

function refuse(code: string): never {
  throw new LinkError(code);
}

/** `pr_link_row` of `contracts/api/objects.json` (`epoch` and `tag` may be absent from the answer of one tag). */
interface LinkRow {
  readonly epoch?: number;
  readonly tag?: string;
  readonly sealed: string | null;
  readonly task_id: string;
  readonly session_id: string | null;
  readonly linked_by: string | null;
  readonly created_at: string;
}

const nullableUuid = (value: unknown): boolean => value === null || value === undefined || isUuid7(value);

/** A row of the contracts: exactly one of `session_id` and `linked_by` (an older hub's row has no `linked_by`). */
export function isLinkRow(value: unknown): value is LinkRow {
  return (
    isRecord(value) &&
    (value.epoch === undefined || (Number.isSafeInteger(value.epoch) && (value.epoch as number) >= 1)) &&
    (value.tag === undefined || typeof value.tag === 'string') &&
    (value.sealed === undefined || value.sealed === null || typeof value.sealed === 'string') &&
    isUuid7(value.task_id) &&
    nullableUuid(value.session_id) &&
    nullableUuid(value.linked_by) &&
    ((value.session_id ?? null) !== null) !== ((value.linked_by ?? null) !== null) &&
    typeof value.created_at === 'string'
  );
}

/** The workgroup key of `epoch`, or null when the browser holds none. */
function keyOf(store: Store, org: string, wg: string, epoch: number): Secret | null {
  return orgEntry(store.getState().orgs, org).keys.get(epochKey(WG, wg, epoch)) ?? null;
}

/** The origin of a link in the body's AAD. */
export const originOf = (row: { readonly session_id: string | null; readonly linked_by: string | null }): string =>
  row.linked_by !== null ? `user:${row.linked_by}` : `device:${row.session_id}`;

/** A row as the browser shows it (module comment); `epoch` and `tag` are those the row was asked under. */
export async function openLink(
  store: Store,
  org: string,
  wg: string,
  epoch: number,
  tag: string,
  row: LinkRow,
): Promise<PullLink> {
  const sessionId = row.session_id ?? null;
  const linkedBy = row.linked_by ?? null;
  const base = { wg, epoch, tag, taskId: row.task_id, sessionId, linkedBy, createdAt: row.created_at };
  const sealed = row.sealed ?? null;
  if (sealed === null) return { ...base, pull: null, code: 'legacy' };
  const key = keyOf(store, org, wg, epoch);
  if (key === null) return { ...base, pull: null, code: 'epoch_stale' };
  try {
    const binding = {
      wgId: wg,
      epoch,
      tag,
      taskId: row.task_id,
      origin: originOf({ session_id: sessionId, linked_by: linkedBy }),
    };
    const pull = await openPrLink(await prLinkBodyKey(key), await prLinkKey(key), binding, sealed);
    return { ...base, pull: { repo: pull.repo, number: pull.number }, code: null };
  } catch (error) {
    return { ...base, pull: null, code: requestErrorCode(error) };
  }
}

function listQuery(task: string | null, cursor: string | null, limit: number): string {
  const params = new URLSearchParams();
  if (task !== null) params.set('task', task);
  if (cursor !== null) params.set('cursor', cursor);
  params.set('limit', String(limit));
  return params.toString();
}

interface Page {
  readonly items: readonly PullLink[];
  readonly nextCursor: string | null;
}

async function readPage(
  context: TasksContext,
  org: string,
  wg: string,
  task: string | null,
  cursor: string | null,
  limit: number,
): Promise<Page> {
  const query = listQuery(task, cursor, limit);
  const body = await context.client.request('GET', `/v1/workgroups/${encodeURIComponent(wg)}/pr-links?${query}`);
  if (
    !isRecord(body) ||
    !Array.isArray(body.items) ||
    !body.items.every((item) => isLinkRow(item) && item.epoch !== undefined && item.tag !== undefined) ||
    !(body.next_cursor === null || typeof body.next_cursor === 'string')
  ) {
    throw new HubError('unavailable', 200);
  }
  const rows = (body.items as LinkRow[]).filter((row) => task === null || row.task_id === task);
  const items = await Promise.all(rows.map((row) => openLink(context.store, org, wg, row.epoch!, row.tag!, row)));
  return { items, nextCursor: body.next_cursor as string | null };
}

/**
 * The first page (`more` false) or the next page of the links of `wg` (of one task with `task`); a list that is
 * loading or has no further page is left alone (module comment).
 */
export async function loadLinks(
  context: TasksContext,
  org: string,
  wg: string,
  task: string | null,
  more = false,
): Promise<void> {
  const { store } = context;
  const key = listKey(wg, task);
  const known = store.getState().pulls.lists[key];
  if (more && (known === undefined || known.status === 'loading' || known.nextCursor === null)) return;
  const generation = store.generation;
  listLoading(store, key);
  try {
    const page = await readPage(context, org, wg, task, more ? known!.nextCursor : null, LIST_LIMIT);
    if (store.generation === generation) listPage(store, key, page.items, page.nextCursor, !more);
  } catch (error) {
    if (store.generation === generation) listFailed(store, key, sendErrorCode(error));
  }
}

const shared = new WeakMap<object, Map<string, Promise<void>>>();

/** Every page of the links of `wg` once per store generation (module comment); a failure is forgotten. */
export function ensureLinks(context: TasksContext, org: string, wg: string): Promise<void> {
  const { store } = context;
  const loads = shared.get(store) ?? new Map<string, Promise<void>>();
  shared.set(store, loads);
  const id = `${store.generation} ${wg}`;
  const known = loads.get(id);
  if (known !== undefined) return known;
  const key = listKey(wg, null);
  const generation = store.generation;
  const load = (async () => {
    listLoading(store, key);
    try {
      let cursor: string | null = null;
      for (let page = 0; page < MAX_PAGES; page += 1) {
        const read = await readPage(context, org, wg, null, cursor, ALL_LIMIT);
        if (store.generation !== generation) return;
        listPage(store, key, read.items, read.nextCursor, page === 0);
        cursor = read.nextCursor;
        if (cursor === null) return;
      }
    } catch (error) {
      loads.delete(id);
      if (store.generation === generation) listFailed(store, key, sendErrorCode(error));
    }
  })();
  loads.set(id, load);
  return load;
}

/** The links of `task` among the loaded links of its workgroup. */
export function taskLinks(store: Store, wg: string, task: string): PullLink[] {
  const state = store.getState().pulls;
  return linkList(state, listKey(wg, null))
    .ids.map((id) => state.links[id]!)
    .filter((link) => link.taskId === task);
}

/** The epochs of `wg` whose key the browser holds, newest first. */
export function heldEpochs(store: Store, org: string, wg: string): number[] {
  const prefix = epochKey(WG, wg, 0).slice(0, -1);
  return [...orgEntry(store.getState().orgs, org).keys.keys()]
    .filter((key) => key.startsWith(prefix))
    .map((key) => Number(key.slice(prefix.length)))
    .sort((a, b) => b - a);
}

/** The links of `ref` under every epoch the browser holds (module comment). */
export async function findLinks(context: TasksContext, org: string, wg: string, ref: PullRef): Promise<PullLink[]> {
  const found: PullLink[] = [];
  for (const epoch of heldEpochs(context.store, org, wg)) {
    const key = keyOf(context.store, org, wg, epoch)!;
    const tag = await prLinkTag(await prLinkKey(key), ref.repo, ref.number);
    let row: unknown;
    try {
      row = await context.client.request(
        'GET',
        `/v1/workgroups/${encodeURIComponent(wg)}/pr-links/${epoch}/${encodeURIComponent(tag)}`,
      );
    } catch (error) {
      if (error instanceof HubError && error.code === 'not_found') continue;
      throw error;
    }
    if (!isLinkRow(row)) throw new HubError('unavailable', 200);
    found.push(await openLink(context.store, org, wg, epoch, tag, row));
  }
  return found;
}

export type LinkOutcome =
  | { readonly status: 'created'; readonly link: PullLink }
  /** The pull request is linked already (to this or another task); nothing was changed. */
  | { readonly status: 'exists'; readonly links: readonly PullLink[] };

/** The current epoch of `wg` and its key (the log job is asked once when the key is not in memory yet). */
async function currentKey(context: TasksContext, org: string, wg: string): Promise<[number, Secret]> {
  const lookup = (): [number, Secret] | null => {
    const epoch = orgEntry(context.store.getState().orgs, org).log!.state.epoch(WG, wg);
    const key = keyOf(context.store, org, wg, epoch);
    return key === null ? null : [epoch, key];
  };
  const known = lookup();
  if (known !== null) return known;
  await context.refreshLog(org);
  return lookup() ?? refuse('epoch_stale');
}

/** Links `ref` to `task` of `wg` unless it is linked already (module comment). */
export async function linkPull(
  context: TasksContext,
  org: string,
  wg: string,
  task: string,
  ref: PullRef,
): Promise<LinkOutcome> {
  const { store } = context;
  const userId = store.getState().me.userId;
  const state = orgEntry(store.getState().orgs, org).log?.state;
  if (userId === null || state === undefined || !state.canWriteWg(userId, wg)) refuse('forbidden');
  const existing = await findLinks(context, org, wg, ref);
  if (existing.length > 0) return { status: 'exists', links: existing };
  const [epoch, key] = await currentKey(context, org, wg);
  const tag = await prLinkTag(await prLinkKey(key), ref.repo, ref.number);
  const sealed = await sealPrLink(await prLinkBodyKey(key), {
    wgId: wg,
    epoch,
    tag,
    taskId: task,
    origin: `user:${userId}`,
    repo: ref.repo,
    number: ref.number,
  });
  const answer = await context.client.request('POST', `/v1/workgroups/${encodeURIComponent(wg)}/pr-links`, {
    body: { epoch, tag, sealed, task_id: task },
  });
  if (!isRecord(answer) || typeof answer.created !== 'boolean' || !isLinkRow(answer.link)) {
    throw new HubError('unavailable', 200);
  }
  const link = await openLink(store, org, wg, epoch, tag, answer.link);
  if (answer.created) addLink(store, link);
  return answer.created ? { status: 'created', link } : { status: 'exists', links: [link] };
}

/** Removes a person's link (module comment). */
export async function unlinkPull(context: TasksContext, link: PullLink): Promise<void> {
  if (link.linkedBy === null) refuse('id_conflict');
  try {
    await context.client.request(
      'DELETE',
      `/v1/workgroups/${encodeURIComponent(link.wg)}/pr-links/${link.epoch}/${encodeURIComponent(link.tag)}`,
    );
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'not_found') throw error;
  }
  removeLink(context.store, link);
}

/** The catalog code of a failed link step. */
export function linkErrorCode(error: unknown): string {
  return error instanceof LinkError ? error.code : sendErrorCode(error);
}
