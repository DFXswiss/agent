// Job 1 of 06-web.md section 11: log verification on sign-in, on SSE `log` frames and on events whose `lh` is beyond
// the verified head. First the platform log (P1-P4 against the compiled root key, extending the stored platform head),
// then the org's membership log: the first verification in this page load fetches the whole log and checks that it
// extends the stored head (L9, `log_rollback`) and verifies it from `seq` 0 against the pinned founder (L1-L8);
// later runs fetch only the entries after the verified head and extend the in-memory log. Any refusal puts the org
// into the blocking error state with the evidence (heads); a network or hub error only fails the run.
//
// Founder pinning without a stored pin (first sign-in on this browser): the founder key is taken from `genesis`, and
// the verified log must contain the unlocked identity of the user as a principal with the same signing key. The hub
// cannot forge the user's own signed `genesis`/`accept` entry, and every later entry is chained to it (L2), so a log
// the hub invented is refused (`log_genesis`) – see docs/platform/decisions/WEB-02.md. A user who entered by `admit`
// (ADR-060) has no own entry, but needs no extra step here: its principal carries its own keys (the check above
// holds), and rule L5 of `admit` already proves the admitting admin's identity through the root-signed platform log
// (`by_platform_seq`, `by_id_sig`), so a hub cannot invent an `admit` in the name of a real, proven account.
// A refusal for an unknown entry type (`log_type`) blocks the org as `outdated` (a newer version wrote it).
import { b64uEncode } from '../encoding/b64u';
import { CryptoError, fail } from '../crypto/errors';
import { checkExtends, type Head } from '../log/entries';
import { appendPlatform, refreshPlatformLog, type VerifiedPlatformLog } from '../log/platform';
import { extendLog, refreshLog, type VerifiedLog } from '../log/verify';
import { blockOrg, orgEntry, setLog, setPlatform } from '../store/slices/orgs';
import type { Store } from '../store/store';
import { HubError } from '../net/signedFetch';
import type { JobContext, JobRegistration, JobRun } from './index';

/** One page of a log read (`GET /v1/orgs/{org}/log`, `GET /v1/platform/log`). */
interface LogPage {
  readonly entries: { readonly seq: number; readonly entry: unknown }[];
  readonly head: Head | null;
  readonly next_after: number | null;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const isHead = (value: unknown): value is Head => isRecord(value) && isSeq(value.seq) && typeof value.hash === 'string';

function isPage(value: unknown): value is LogPage {
  return (
    isRecord(value) &&
    Array.isArray(value.entries) &&
    value.entries.every((item) => isRecord(item) && isSeq(item.seq) && 'entry' in item) &&
    (value.head === null || isHead(value.head)) &&
    (value.next_after === null || isSeq(value.next_after))
  );
}

/** All entries after `after` (from `seq` 0 for null) and the head of the last page. */
export async function fetchLog(
  context: JobContext,
  path: string,
  after: number | null,
): Promise<{ entries: unknown[]; head: Head | null }> {
  const entries: unknown[] = [];
  let cursor = after;
  for (;;) {
    const page = await context.client.request('GET', cursor === null ? path : `${path}?after=${cursor}`);
    if (!isPage(page)) throw new HubError('unavailable', 200);
    entries.push(...page.entries.map((item) => item.entry));
    if (page.next_after === null) return { entries, head: page.head };
    cursor = page.next_after;
  }
}

/** The served head must be the head of what was verified (the hub's claim and the chain agree), else `code`. */
function requireHead(verified: Head, served: Head | null, code: 'log_chain' | 'log_rollback'): void {
  if (served === null || verified.seq !== served.seq || verified.hash !== served.hash) fail(code);
}

const platformLocks = new WeakMap<Store, Promise<unknown>>();

/**
 * Verifies the platform log (incrementally after the first run) and stores the new head. Serialized per store: the
 * queues of different orgs run in parallel, and an older read finishing last must not replace a newer platform log.
 * Writes nothing if the store was reset (sign-out) since `generation`.
 */
export function refreshPlatform(
  context: JobContext,
  generation: number = context.store.generation,
): Promise<VerifiedPlatformLog | null> {
  const previous = platformLocks.get(context.store) ?? Promise.resolve();
  const next = previous.then(
    () => refreshPlatformNow(context, generation),
    () => refreshPlatformNow(context, generation),
  );
  platformLocks.set(context.store, next);
  return next;
}

async function refreshPlatformNow(context: JobContext, generation: number): Promise<VerifiedPlatformLog | null> {
  const { store, persist } = context;
  const current = store.getState().orgs.platform;
  const read = await fetchLog(context, '/v1/platform/log', current === null ? null : current.state.seq);
  if (current !== null && read.entries.length === 0) {
    requireHead({ seq: current.state.seq, hash: current.state.head }, read.head, 'log_rollback');
    return current;
  }
  const stored = await persist.loadPlatformHead();
  if (current === null && read.entries.length === 0) {
    if (stored !== null) fail('log_rollback');
    return null;
  }
  // (statements, not a conditional expression with `await`s: V8 miscounts the code after such an expression)
  let verified: VerifiedPlatformLog;
  if (current === null) verified = await refreshPlatformLog(stored, read.entries, context.rootKey);
  else verified = await appendPlatform(current, read.entries, context.rootKey);
  const head = { seq: verified.state.seq, hash: verified.state.head };
  requireHead(head, read.head, 'log_chain');
  if (store.generation !== generation) return verified;
  setPlatform(store, verified);
  await persist.savePlatformHead(head);
  return verified;
}

/** The founder for a first verification: the stored pin, or the key of `genesis` (checked by `anchor`). */
function founderOf(entries: readonly unknown[]): string {
  const body = isRecord(entries[0]) && isRecord(entries[0].body) ? entries[0].body : {};
  const pub = isRecord(body.pub) ? body.pub : {};
  return typeof pub.sig === 'string' ? pub.sig : '';
}

/** Without a stored pin the log must contain the user's own unlocked signing key (`log_genesis` otherwise). */
function anchor(store: Store, log: VerifiedLog): void {
  const { userId, identity } = store.getState().me;
  const principal = userId === null ? undefined : log.state.principals.get(userId);
  if (identity === null || principal?.sig !== b64uEncode(identity.sigPublic())) fail('log_genesis');
}

interface Evidence {
  served: Head | null;
}

async function verifyOrg(
  context: JobContext,
  org: string,
  platform: VerifiedPlatformLog | null,
  evidence: Evidence,
): Promise<VerifiedLog> {
  const { store, persist } = context;
  const path = `/v1/orgs/${encodeURIComponent(org)}/log`;
  const current = orgEntry(store.getState().orgs, org).log;
  const platformState = platform?.state ?? null;
  if (current !== null) {
    const read = await fetchLog(context, path, current.head.seq);
    evidence.served = read.head;
    if (read.entries.length === 0) {
      requireHead(current.head, read.head, 'log_rollback');
      return current;
    }
    const log = await extendLog(current, read.entries, platformState);
    requireHead(log.head, read.head, 'log_chain');
    return log;
  }
  const pinned = await persist.loadPinned(org);
  const read = await fetchLog(context, path, null);
  evidence.served = read.head;
  const log = await refreshLog(
    { founder: pinned?.founder ?? founderOf(read.entries), head: pinned?.head ?? null },
    read.entries,
    platformState,
  );
  if (pinned === null) anchor(store, log);
  requireHead(log.head, read.head, 'log_chain');
  return log;
}

/**
 * The stored pin must not go back (L9 across tabs): a pin at or below the new head must lie on it (`log_rollback`);
 * a pin beyond it (another tab verified more) is kept. Returns whether the new head may be stored.
 */
async function pinMayAdvance(context: JobContext, org: string, log: VerifiedLog): Promise<boolean> {
  const stored = (await context.persist.loadPinned(org))?.head ?? null;
  if (stored !== null && stored.seq > log.head.seq) return false;
  await checkExtends(stored, log.entries);
  return true;
}

/**
 * Runs the verification of one org; refusals block the org (with the evidence) and are rethrown. Writes nothing if
 * the store was reset (sign-out) since `generation`.
 */
export async function verifyOrgLog(
  context: JobContext,
  org: string,
  generation: number = context.store.generation,
): Promise<void> {
  const { store, persist } = context;
  if (store.getState().me.identity === null) return;
  const before = orgEntry(store.getState().orgs, org).log?.head ?? (await persist.loadPinned(org))?.head ?? null;
  const platformBefore = store.getState().orgs.platform;
  const evidence: Evidence = { served: null };
  let log: VerifiedLog;
  let advance: boolean;
  try {
    log = await verifyOrg(context, org, await refreshPlatform(context, generation), evidence);
    advance = await pinMayAdvance(context, org, log);
  } catch (error) {
    if (error instanceof CryptoError && store.generation === generation) {
      blockOrg(store, org, { code: error.code, stored: before, served: evidence.served, lh: null });
    }
    throw error;
  }
  if (store.generation !== generation) return;
  if (orgEntry(store.getState().orgs, org).log === log) {
    // only the platform log changed (a member's first run after accepting): events waiting for it are read now
    if (store.getState().orgs.platform !== platformBefore) context.changed?.(org);
    return;
  }
  setLog(store, org, log);
  if (advance) await persist.savePinned(org, { founder: log.state.founder, head: log.head });
  context.changed?.(org);
}

/**
 * L10 for an event of `org` with head `lh`: if `lh` is beyond the verified head after the refresh, the hub withheld
 * entries the author saw – `log_fork` blocks the org. Returns false in that case.
 */
export function checkLh(store: Store, org: string, lh: number): boolean {
  const head = orgEntry(store.getState().orgs, org).log?.head ?? null;
  if (head === null || lh <= head.seq) return true;
  blockOrg(store, org, { code: 'log_fork', stored: head, served: null, lh });
  return false;
}

export const job: JobRegistration = {
  name: 'log-verify',
  order: 10,
  triggers: ['login', 'log', 'lh'],
  run: (context: JobContext, run: JobRun) => verifyOrgLog(context, run.org, run.generation),
  // a network or hub failure leaves the org unverified ("being checked"): try again with backoff (KOR-05, A-22)
  retry: true,
};
