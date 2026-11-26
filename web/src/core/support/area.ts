// The support area of a DFX support person (ADR-096 section 4; route `/support`, shown only for a person of the
// roster of the browser's verified platform log). Support never holds the org key and never opens the org's live
// channel: everything is read by request and refreshed by polling.
//
// - `loadReleases`: the active releases (`GET /v1/support/releases`; `404` = the hub does not know the user as support).
// - `openOrg`: the organization's membership log (`GET /v1/orgs/{org}/log`, allowed for a person with a release in that
//   org) verified from `seq` 0 against the verified platform log – the founder from `genesis`, as on a first visit of a
//   member (the person holds no pin); later calls extend it – and the workgroup wraps addressed to the person
//   (`GET /v1/wraps?scope=wg`) checked by W2 with the roster of the platform log (`checkWrap`) and opened.
// - `pullSupport`: the newest page of a query of a released workgroup (`GET /v1/scopes/{wg}/events`), each event
//   checked like the member's reader does (`checkEvent` with the support authors of the platform log, decertified
//   platform devices excluded) and opened with the person's keys; an `lh` beyond the verified head re-reads the log
//   first. The entries go to the `events` slice, so the chat timeline and the titles of the task screens read them.
// - `selectInfo`: the names of the newest verified `support.info` of a workgroup (org, workgroup, members).
// - `sendSupportPrompt`: a `prompt` into an existing task, signed by the person's identity (`author` = its own ID),
//   with a `write` release (`OrgState.supportWrites` with the roster, the hub checks again).
import { b64uDecode } from '../encoding/b64u';
import { fromUtf8 } from '../encoding/bytes';
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { CryptoError, fail } from '../crypto/errors';
import { newHeader, openEvent, sealEvent } from '../crypto/event';
import type { Secret } from '../crypto/secret';
import { unwrapKey } from '../crypto/wrap';
import { type Head, refusalCode } from '../log/entries';
import { checkEvent } from '../log/events';
import { excludedFor, type PlatformState } from '../log/platform';
import { epochKey, WG } from '../log/state';
import { extendLog, verifyLog } from '../log/verify';
import { checkWrap, WRAP_KEYS } from '../log/wraps';
import { fetchLog } from '../jobs/log-verify';
import { eventsBefore, NEWEST, PAGE_MAX } from '../net/pages';
import { HubError } from '../net/signedFetch';
import type { StoredEvent } from '../net/sse';
import { isRecord } from '../registry';
import { refuse } from '../org/batch';
import { type EventEntry, putEvents, type ScopeEvents } from '../store/slices/events';
import { type Access, type OpenedOrg, setArea, setOpened, type SupportRelease } from '../store/slices/support';
import type { StoreState } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import { PROMPT_MAX, sendable, sendErrorCode } from '../tasks/send';
import { authorsOf, rosterOf } from './names';
import { INFO_KIND } from './release';

/** How often an open support view reads again (ms): support has no live channel. */
export const POLL_MS = 4000;

type Context = Pick<TasksContext, 'store' | 'client' | 'now'>;

const isRelease = (value: unknown): value is SupportRelease =>
  isRecord(value) &&
  typeof value.org_id === 'string' &&
  typeof value.wg_id === 'string' &&
  (value.access === 'read' || value.access === 'write') &&
  Number.isSafeInteger(value.granted_seq) &&
  typeof value.granted_at === 'string';

/** Loads the active releases into `support.area`; failures are recorded, never thrown. */
export async function loadReleases(context: Context): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const known = store.getState().support.area;
  if (known.status === 'idle') setArea(store, { status: 'loading', code: null, releases: [] });
  try {
    const body = await context.client.request('GET', '/v1/support/releases');
    if (!isRecord(body) || !Array.isArray(body.releases) || !body.releases.every(isRelease))
      throw new HubError('unavailable', 200);
    if (store.generation === generation) setArea(store, { status: 'ready', code: null, releases: body.releases });
  } catch (error) {
    if (store.generation !== generation) return;
    const none = error instanceof HubError && error.code === 'not_found';
    setArea(store, { status: none ? 'none' : 'error', code: sendErrorCode(error), releases: known.releases });
  }
}

/** The verified platform state, required to verify anything of a foreign organization. */
function platformOf(context: Context): PlatformState {
  return context.store.getState().orgs.platform?.state ?? refuse('unavailable');
}

/** The signing key of `genesis` (the founder of a log the person sees for the first time). */
function founderOf(entries: readonly unknown[]): string {
  const body = isRecord(entries[0]) && isRecord(entries[0].body) ? entries[0].body : {};
  return isRecord(body.pub) && typeof body.pub.sig === 'string' ? body.pub.sig : '';
}

/** The served head must be the head of what was verified. */
function requireHead(verified: Head, served: Head | null): void {
  if (served === null || served.seq !== verified.seq || served.hash !== verified.hash) fail('log_chain');
}

const isStoredWrap = (
  value: unknown,
): value is {
  scope: string;
  scope_id: string;
  epoch: number;
  recipient_id: string;
  wrap: string;
  sig: string;
  wrapped_by: string;
} =>
  isRecord(value) &&
  value.scope === WG &&
  typeof value.scope_id === 'string' &&
  Number.isSafeInteger(value.epoch) &&
  typeof value.recipient_id === 'string' &&
  typeof value.wrap === 'string' &&
  typeof value.sig === 'string' &&
  typeof value.wrapped_by === 'string';

/**
 * Verifies (first time) or extends the membership log of `org` and opens the workgroup wraps addressed to the person
 * (module comment). Refusals are thrown (`CryptoError` codes); a wrap that does not verify or open is left out.
 */
export async function openOrg(context: Context, org: string): Promise<OpenedOrg> {
  const { store } = context;
  const { userId, identity } = store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const platform = platformOf(context);
  const generation = store.generation;
  const path = `/v1/orgs/${encodeURIComponent(org)}/log`;
  const known = store.getState().support.opened[org];
  let log;
  if (known === undefined) {
    const read = await fetchLog(context, path, null);
    log = await verifyLog(read.entries, founderOf(read.entries), platform);
    requireHead(log.head, read.head);
  } else {
    const read = await fetchLog(context, path, known.log.head.seq);
    log = read.entries.length === 0 ? known.log : await extendLog(known.log, read.entries, platform);
    requireHead(log.head, read.head);
  }
  if (log.state.org !== org) fail('log_genesis');
  const body = await context.client.request('GET', '/v1/wraps?scope=wg');
  const wraps = isRecord(body) && Array.isArray(body.wraps) ? body.wraps.filter(isStoredWrap) : [];
  const roster = rosterOf(platform);
  const excluded = excludedFor(platform, log.state);
  const keys = new Map<string, Secret>(known?.keys ?? []);
  for (const found of wraps) {
    if (found.recipient_id !== userId || !log.state.workgroups.has(found.scope_id)) continue;
    const key = epochKey(WG, found.scope_id, found.epoch);
    if (keys.has(key)) continue;
    try {
      // the wrap object of the signature (`GET /v1/wraps` adds `wrapped_by` and `created_at`)
      const wrap = Object.fromEntries(WRAP_KEYS.map((key) => [key, found[key as keyof typeof found]]));
      const slot = await checkWrap(wrap, found.wrapped_by, log, excluded, false, roster);
      keys.set(key, await unwrapKey(identity.kem, slot, found.wrap));
    } catch {
      // a wrap the verified logs do not allow, or that does not open, is never used
    }
  }
  const opened = { log, keys };
  if (store.generation === generation) setOpened(store, org, opened);
  return opened;
}

const unverifiable = (entry: EventEntry, code: string): EventEntry => ({
  ...entry,
  status: 'unverifiable',
  code,
  header: null,
  body: null,
});

/** The reader's verdict on stored events of `wg` of `org` (module comment). */
export async function readEntries(
  opened: OpenedOrg,
  platform: PlatformState,
  org: string,
  wg: string,
  items: readonly StoredEvent[],
): Promise<EventEntry[]> {
  const out: EventEntry[] = [];
  const options = { excluded: excludedFor(platform, opened.log.state), support: authorsOf(platform) };
  for (const item of items) {
    const entry: EventEntry = {
      org,
      scopeId: wg,
      scopeSeq: item.scope_seq,
      receivedAt: item.received_at,
      event: item.event,
      status: 'pending',
      code: null,
      header: null,
      body: null,
    };
    const raw = isRecord(item.event) && isRecord(item.event.header) ? item.event.header : {};
    if (raw.org !== org || raw.scope_id !== wg) {
      out.push(unverifiable(entry, 'header_invalid'));
      continue;
    }
    try {
      const { header, authorSig } = await checkEvent(item.event, opened.log, options);
      const key = opened.keys.get(epochKey(WG, wg, header.epoch as number));
      if (key === undefined) {
        out.push({ ...entry, status: 'locked', header });
        continue;
      }
      const plaintext = await openEvent(key, item.event, b64uDecode(authorSig));
      const body = parseBody(plaintext);
      out.push({ ...entry, status: 'verified', header, body, bodyBytes: plaintext.length });
    } catch (error) {
      out.push(unverifiable(entry, refusalCode(error)));
    }
  }
  return out;
}

function parseBody(plaintext: Uint8Array): unknown {
  try {
    return JSON.parse(fromUtf8(plaintext)) as unknown;
  } catch {
    throw new CryptoError('header_invalid');
  }
}

/** A query of a released workgroup: its kinds, optionally of one task. */
export interface SupportQuery {
  readonly kinds: readonly string[];
  readonly task?: string;
}

const lhOf = (item: StoredEvent): number => {
  const header = isRecord(item.event) && isRecord(item.event.header) ? item.event.header : {};
  return Number.isSafeInteger(header.lh) ? (header.lh as number) : -1;
};

/** Reads the newest page of the query into the `events` slice; returns the number of events read. */
export async function pullSupport(context: Context, org: string, wg: string, query: SupportQuery): Promise<number> {
  const { store } = context;
  const generation = store.generation;
  const platform = platformOf(context);
  const filter = { kinds: query.kinds, ...(query.task !== undefined && { task: query.task }) };
  const page = await eventsBefore(context.client, wg, filter, NEWEST, PAGE_MAX);
  let opened = store.getState().support.opened[org] ?? (await openOrg(context, org));
  if (page.events.some((item) => lhOf(item) > opened.log.head.seq)) opened = await openOrg(context, org);
  const entries = await readEntries(opened, platform, org, wg, page.events);
  if (store.generation === generation) putEvents(store, wg, entries);
  return entries.length;
}

/** The names of a released workgroup from its newest verified `support.info`. */
export interface SupportInfo {
  readonly orgName: string;
  readonly wgName: string;
  /** User ID → name. */
  readonly members: ReadonlyMap<string, string>;
  /** The user that wrote it (a member with the release right). */
  readonly author: string;
}

const text = (value: unknown, max: number): value is string =>
  typeof value === 'string' && value.length > 0 && [...value].length <= max;

function infoOf(entry: EventEntry): SupportInfo | null {
  const body = entry.body;
  if (!isRecord(body) || !text(body.org_name, 200) || !text(body.wg_name, 200) || !isRecord(body.members)) return null;
  const members = Object.entries(body.members).filter((pair): pair is [string, string] => text(pair[1], 100));
  return {
    orgName: body.org_name,
    wgName: body.wg_name,
    members: new Map(members),
    author: entry.header!.author as string,
  };
}

const infos = new WeakMap<ScopeEvents, SupportInfo | null>();

/** Selector: the newest valid `support.info` of `wg` (null if none was read). */
export const selectInfo =
  (wg: string) =>
  (state: StoreState): SupportInfo | null => {
    const scope = state.events[wg];
    if (scope === undefined) return null;
    if (infos.has(scope)) return infos.get(scope)!;
    let found: SupportInfo | null = null;
    let seq = -1;
    for (const entry of scope.entries.values()) {
      if (entry.status !== 'verified' || entry.header!.kind !== INFO_KIND || entry.scopeSeq < seq) continue;
      const info = infoOf(entry);
      if (info === null) continue;
      found = info;
      seq = entry.scopeSeq;
    }
    infos.set(scope, found);
    return found;
  };

/** The access the person has on `wg` by the verified log and its roster (null: no release, or not in the roster). */
export function accessOf(state: StoreState, org: string, wg: string): Access | null {
  const opened = state.support.opened[org];
  const user = state.me.userId;
  const roster = rosterOf(state.orgs.platform?.state ?? null);
  if (opened === undefined || user === null || !opened.log.state.supportReads(roster, user, wg)) return null;
  return opened.log.state.supportWrites(roster, user, wg) ? 'write' : 'read';
}

/** Sends `text` as a prompt of `task` in `wg` of `org`, signed by the support person; returns its `scope_seq`. */
export async function sendSupportPrompt(
  context: Context,
  org: string,
  wg: string,
  task: string,
  text: string,
): Promise<number> {
  if (!sendable(text)) refuse(text.length > PROMPT_MAX ? 'size_exceeded' : 'request_invalid');
  const { store } = context;
  const { userId, identity } = store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const opened = await openOrg(context, org);
  if (accessOf(store.getState(), org, wg) !== 'write') refuse('forbidden');
  const state = opened.log.state;
  const epoch = state.epoch(WG, wg);
  const key = opened.keys.get(epochKey(WG, wg, epoch)) ?? refuse('epoch_stale');
  const now = context.now();
  const header = newHeader({
    id: uuid7(now),
    org,
    scope: WG,
    scope_id: wg,
    epoch,
    author: userId,
    kind: 'prompt',
    ts: formatTime(now),
    lh: opened.log.head.seq,
    wg,
    task,
  });
  const event = await sealEvent(key, header, bodyJson({ text, attachments: [] }), identity.sig.privateKey);
  const answer = await context.client.request('POST', `/v1/tasks/${encodeURIComponent(task)}/prompts`, {
    body: { event },
  });
  if (!isRecord(answer) || !Number.isSafeInteger(answer.scope_seq)) throw new HubError('unavailable', 200);
  const seq = answer.scope_seq as number;
  const entries = await readEntries(opened, platformOf(context), org, wg, [
    { scope_seq: seq, received_at: null, event },
  ]);
  putEvents(store, wg, entries);
  return seq;
}
