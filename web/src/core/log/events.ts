// Event rules E1-E9 (04-crypto.md sections 4.3, 5.12, 6.4) and the kind matrix (CON-03). TypeScript twin of
// `agent_hub.log.events`. `KIND_RULES` is the table of section 4.3 and equal to `contracts/enums/event-kinds.json`
// (a test compares them). E1, E2, E4 and E8 are the format checks of `crypto/event`; this module adds the rest.
// Check order (every implementation; the first failing check gives the code):
// 1. E1/E2 object and header format (`header_invalid`, `non_ascii`), header `org` equals the log's org
//    (`header_invalid`);
// 2. L10 `lh` within the verified log (`log_fork`; readers call `resolveLh` and refresh first);
// 3. author: an active principal of the org at `lh`, not excluded (`author_not_writer`);
// 4. E3 `(kind, scope, author kind)` allowed (`kind_not_allowed`), then the kind's header rules (`header_invalid`);
// 5. E8 size (`size_exceeded`) and E4 author signature under the author's key at `lh` (`signature_invalid`);
// 6. E5 the kind's right at `lh` (`author_not_writer`; no event is written into an archived workgroup);
// 7. hub only: E5 again at receipt, E6 epoch (`epoch_stale`), E7 the session runs on the author device. Readers
//    accept any epoch they hold a key for. E9 (`event_conflict`) is `checkReplay`.
// Every function takes the clock as a parameter (`now`, `rotatedAt`, unix ms).
// An agent author (ADR-082) is a user for E3 and has its user's rights for E5 (`OrgState.acting`); its
// `member.profile` is its user's (`ref` = the acting user).
// Support (ADR-096): an author that is no principal of the org but a person of the `support` roster passed in
// (readers: `PlatformState.supportAuthors`, the hub: `supportRoster`) has the author kind `support`; E3 treats it as
// a user for the workgroup kinds of `SUPPORT_KINDS` only (`prompt`, workgroup whiteboard; `kind_not_allowed`
// otherwise), E5 needs a `write` release of the workgroup at `lh` (and at the hub at receipt). A member of the org
// that is also a support person writes if either its member right or the release allows it (the org log first, then
// the roster). `support.info` (the names a support person needs, sealed under the workgroup key) is written by a
// principal that may release the workgroup (`wg_release`).
import { canonical } from '../encoding/json';
import { equalBytes } from '../encoding/bytes';
import { isUuid7 } from '../encoding/ids';
import { b64uDecode } from '../encoding/b64u';
import { fail } from '../crypto/errors';
import * as eventFormat from '../crypto/event';
import * as F from '../crypto/fields';
import type { Secret } from '../crypto/secret';
import { AGENT, DEVICE, NO_ROSTER, type OrgState, PLAIN, type Principal, type Roster, SUPPORT, USER } from './state';
import type { VerifiedLog } from './verify';

/** E6: the previous epoch is accepted this long (ms) after the rotation (not for removed authors). */
export const GRACE_MS = 600_000;

export const ORG_ADMIN = 'org_admin';
export const ORG_MEMBER = 'org_member';
export const ORG_MEMBER_SELF = 'org_member_self';
export const WG_WRITE = 'wg_write';
export const DEVICE_ASSIGNED_WG = 'device_assigned_wg';
export const DEVICE_ASSIGNED_WG_SESSION = 'device_assigned_wg_session';
export const WG_RELEASE = 'wg_release';
export const RIGHTS: Readonly<Record<string, string>> = {
  [ORG_ADMIN]: 'author is an admin of the org',
  [ORG_MEMBER]: 'author is a member of the org',
  [ORG_MEMBER_SELF]: 'author is an org member and ref == author',
  [WG_WRITE]: 'author has write access to the workgroup',
  [DEVICE_ASSIGNED_WG]: 'author device is assigned to the workgroup',
  [DEVICE_ASSIGNED_WG_SESSION]: 'author device is assigned to the workgroup and the session runs on it',
  [WG_RELEASE]: 'author is an admin of the org or has write access to the workgroup, which is not archived',
};
export const NOTIFY_ALLOWED: readonly string[] = ['whiteboard.entry'];
/** What a support person writes with a `write` release (ADR-096 section 4): prompts into existing tasks and the
 *  workgroup whiteboard - no task, no workgroup settings. */
export const SUPPORT_KINDS: ReadonlySet<string> = new Set([
  'prompt',
  'whiteboard.entry',
  'whiteboard.edit',
  'whiteboard.delete',
]);
export const SIZE_LIMITS: Readonly<Record<string, number>> = {
  default_ct_bytes: eventFormat.MAX_CT_BYTES,
  'session.terminal': eventFormat.MAX_TERMINAL_CT_BYTES,
};
const EVENT_KEYS = ['header', 'ct', 'sig'];

/**
 * One row of the matrix: `kind` in `scope`, written by one of `authors`, needs `right`; `header` constrains header
 * fields (`empty` = "", `set` = a UUID, `scope_id` = equals scope_id, `target` = a workgroup of the org, `author` =
 * equals the author, `device`/`entry`/`operation` = a UUID, `read_target` = not empty, `subject` = a user of the org
 * or a user invitation of its log at `lh`).
 */
export interface KindRule {
  readonly kind: string;
  readonly scope: string;
  readonly authors: readonly string[];
  readonly right: string;
  readonly header: Readonly<Record<string, string>>;
  readonly note: string;
}

const rule = (
  kind: string,
  scope: string,
  authors: string[],
  right: string,
  header: Record<string, string> = {},
  note = '',
): KindRule => ({ kind, scope, authors, right, header, note });

const ORG_ENTRY = { wg: 'empty', task: 'empty', ref: 'entry' };
const WG_ENTRY = { wg: 'scope_id', ref: 'entry' };
const WG_TASK = { wg: 'scope_id', task: 'set', session: 'empty', ref: 'empty' };
const SESSION = { wg: 'scope_id', task: 'set', session: 'set' };
const ORG_LEVEL = 'org level';
const WG_LEVEL = 'wg level: task empty; task level: task set';

export const KIND_RULES: readonly KindRule[] = [
  rule('org.profile', 'org', [USER], ORG_ADMIN, { wg: 'empty' }),
  rule('wg.profile', 'org', [USER], ORG_ADMIN, { wg: 'target' }),
  rule('member.profile', 'org', [USER], ORG_MEMBER_SELF, { wg: 'empty', ref: 'author' }),
  rule('device.profile', 'org', [USER], ORG_ADMIN, { wg: 'empty', ref: 'device' }),
  // an admin-given name of a member or of an invitation's person (ADR-073)
  rule('member.name', 'org', [USER], ORG_ADMIN, { wg: 'empty', ref: 'subject' }),
  // a topic of captured tasks (ADR-100 section 3): any member writes it, `ref` = the topic ID
  rule('topic.profile', 'org', [USER], ORG_MEMBER, { wg: 'empty', task: 'empty', ref: 'set' }),
  ...['whiteboard.entry', 'whiteboard.edit', 'whiteboard.delete'].flatMap((kind) => [
    rule(kind, 'org', [USER], ORG_ADMIN, ORG_ENTRY, ORG_LEVEL),
    rule(kind, 'wg', [USER], WG_WRITE, WG_ENTRY, WG_LEVEL),
  ]),
  rule('settings.workgroup', 'wg', [USER], WG_WRITE, { wg: 'scope_id' }),
  // the names a support person needs (org, workgroup, members), sealed under the workgroup key (ADR-096)
  rule(
    'support.info',
    'wg',
    [USER],
    WG_RELEASE,
    { wg: 'scope_id', task: 'empty', session: 'empty', ref: 'empty' },
    'written by the releasing client with a support release',
  ),
  rule('task.meta', 'wg', [USER], WG_WRITE, WG_TASK),
  rule('task.meta', 'wg', [DEVICE], DEVICE_ASSIGNED_WG, WG_TASK, 'only as result of the operation task.set_title'),
  // the name a person gives the main agent of a task (ADR-095 section 2)
  rule('task.agent_name', 'wg', [USER], WG_WRITE, WG_TASK),
  rule('prompt', 'wg', [USER], WG_WRITE, WG_TASK),
  ...['output', 'tool', 'hook', 'file', 'terminal', 'status', 'error'].map((name) =>
    rule(`session.${name}`, 'wg', [DEVICE], DEVICE_ASSIGNED_WG_SESSION, SESSION),
  ),
  rule('operation.params', 'wg', [DEVICE], DEVICE_ASSIGNED_WG, { wg: 'scope_id', ref: 'operation' }),
  rule('operation.result', 'wg', [DEVICE], DEVICE_ASSIGNED_WG, { wg: 'scope_id', ref: 'operation' }),
  rule('platform.read', 'wg', [DEVICE], DEVICE_ASSIGNED_WG, { wg: 'scope_id', ref: 'read_target' }),
  rule('workvm.run', 'wg', [DEVICE], DEVICE_ASSIGNED_WG_SESSION, SESSION),
  rule('workvm.state', 'wg', [DEVICE], DEVICE_ASSIGNED_WG_SESSION, SESSION),
  rule('test.run', 'wg', [DEVICE], DEVICE_ASSIGNED_WG_SESSION, SESSION),
  rule('browser.action', 'wg', [DEVICE], DEVICE_ASSIGNED_WG_SESSION, SESSION),
  rule('browser.state', 'wg', [DEVICE], DEVICE_ASSIGNED_WG_SESSION, SESSION),
];
const BY_KEY = new Map(KIND_RULES.flatMap((r) => r.authors.map((author) => [`${r.kind} ${r.scope} ${author}`, r])));

/** The matrix in the form of `contracts/enums/event-kinds.json` (without its description). */
export function matrix(): Record<string, unknown> {
  return {
    rights: { ...RIGHTS },
    notify_allowed: [...NOTIFY_ALLOWED],
    size_limits: { ...SIZE_LIMITS },
    kinds: KIND_RULES.map((r) => ({
      kind: r.kind,
      scope: r.scope,
      authors: [...r.authors],
      right: r.right,
      header: { ...r.header },
      ...(r.note === '' ? {} : { note: r.note }),
    })),
  };
}

/** E3: the rule of an allowed combination (`kind_not_allowed` otherwise). */
export function kindRule(kind: string, scope: string, authorKind: string): KindRule {
  return BY_KEY.get(`${kind} ${scope} ${authorKind}`) ?? fail('kind_not_allowed');
}

/** A user of the org (active or not) or a user invitation of its log, in the state at `lh`. */
function isSubject(value: unknown, state: OrgState): boolean {
  const id = value as string;
  return state.principals.get(id)?.kind === USER || state.invites.get(id)?.kind === USER;
}

function fieldValid(check: string, value: unknown, header: F.Fields, state: OrgState): boolean {
  switch (check) {
    case 'empty':
      return value === '';
    case 'scope_id':
      return value === header.scope_id;
    case 'target':
      return state.workgroups.has(value as string);
    case 'author':
      return value === state.acting(header.author as string);
    case 'read_target':
      return value !== '';
    case 'subject':
      return isSubject(value, state);
    default: // set, device, entry, operation: a UUID
      return isUuid7(value);
  }
}

/** The kind's header field rules (`header_invalid`). */
export function checkHeaderRules(kind: KindRule, header: F.Fields, state: OrgState): void {
  if (!Object.entries(kind.header).every(([name, check]) => fieldValid(check, header[name], header, state))) {
    fail('header_invalid');
  }
}

/** E5: the right of the rule for the header's author in `state`; `support` the roster of support authors. */
export function hasRight(kind: KindRule, state: OrgState, header: F.Fields, support: Roster = NO_ROSTER): boolean {
  const author = header.author as string;
  const scopeId = header.scope_id as string;
  switch (kind.right) {
    case ORG_ADMIN:
      return state.isAdmin(author);
    case ORG_MEMBER:
      return state.member(author) !== null;
    case ORG_MEMBER_SELF:
      return state.member(author) !== null && header.ref === state.acting(author);
    case WG_WRITE:
      // the member right first, then the support release (a member that is also support, ADR-096)
      return (
        state.canWriteWg(author, scopeId) ||
        (SUPPORT_KINDS.has(header.kind as string) && state.supportWrites(support, author, scopeId))
      );
    case WG_RELEASE:
      return state.writable(scopeId) && state.canRelease(author, scopeId);
    default: // device_assigned_wg, device_assigned_wg_session
      return state.assigned(author, scopeId) && state.writable(scopeId);
  }
}

function eventAuthor(state: OrgState, header: F.Fields, excluded: ReadonlySet<string>, support: Roster): Principal {
  const author = header.author as string;
  let found = state.member(author);
  const person = support.get(author);
  if (found === null && person !== undefined) {
    // a support person (ADR-096): no principal of the org, keys from the root certification
    found = {
      ...PLAIN,
      id: author,
      kind: SUPPORT,
      role: SUPPORT,
      kem: person.kem,
      sig: person.sig,
      joinedSeq: 0,
      removedSeq: null,
      platformSeq: null,
      consented: true,
    };
  }
  if (found === null || excluded.has(found.id)) fail('author_not_writer');
  return found;
}

/** E3 for the author's kind: an agent as a user; a support person as a user for workgroup writes only. */
function eventRule(header: F.Fields, author: Principal): KindRule {
  const kind = header.kind as string;
  const found = kindRule(
    kind,
    header.scope as string,
    author.kind === AGENT || author.kind === SUPPORT ? USER : author.kind,
  );
  if (author.kind === SUPPORT && (found.scope !== 'wg' || !SUPPORT_KINDS.has(kind))) fail('kind_not_allowed');
  return found;
}

/** A checked event: its header, the matrix row and the author's key at `lh`. */
export interface CheckedEvent {
  readonly header: F.Fields;
  readonly rule: KindRule;
  readonly authorSig: string;
}

/** Reader options: the device the header's session runs on (E7, when known), the excluded platform devices and the
 *  support authors of the reader's platform log (`PlatformState.supportAuthors`, ADR-096). */
export interface ReaderOptions {
  readonly sessionDevice?: string | null;
  readonly excluded?: Iterable<string>;
  readonly support?: Roster;
}

/** Reader checks (steps 1-6, and E7 when the reader knows the session's device). */
export async function checkEvent(event: unknown, log: VerifiedLog, options: ReaderOptions = {}): Promise<CheckedEvent> {
  const skip = new Set(options.excluded ?? []);
  const data = F.requireKeys(event, EVENT_KEYS);
  const header = eventFormat.checkHeader(data.header);
  if (header.org !== log.state.org) fail('header_invalid');
  const state = log.stateAt(header.lh as number);
  const support = options.support ?? NO_ROSTER;
  const author = eventAuthor(state, header, skip, support);
  const found = eventRule(header, author);
  checkHeaderRules(found, header, state);
  await eventFormat.verifyEvent(event, b64uDecode(author.sig));
  if (!hasRight(found, state, header, support)) fail('author_not_writer');
  const sessionDevice = options.sessionDevice ?? null;
  if (found.right === DEVICE_ASSIGNED_WG_SESSION && sessionDevice !== null && sessionDevice !== header.author) {
    fail('author_not_writer');
  }
  return { header, rule: found, authorSig: author.sig };
}

/** Reader checks, then decryption with the scope key of the header's epoch (the reader's full path, 5.12). */
export async function openCheckedEvent(
  event: unknown,
  log: VerifiedLog,
  scopeKey: Secret,
  options: ReaderOptions = {},
): Promise<{ readonly checked: CheckedEvent; readonly plaintext: Uint8Array }> {
  const checked = await checkEvent(event, log, options);
  const plaintext = await eventFormat.openEvent(scopeKey, event, b64uDecode(checked.authorSig));
  return { checked, plaintext };
}

/**
 * E6 (the grace rule): the current epoch, or the previous one within `GRACE_MS` after the rotation at `rotatedAt`
 * if the author is entitled to the new epoch as well and not excluded (`epoch_stale`; a support author: by the
 * release, ADR-096).
 */
export function checkEpoch(
  state: OrgState,
  header: F.Fields,
  now: number,
  rotatedAt: number | null,
  excluded: Iterable<string> = [],
  support: Roster = NO_ROSTER,
): void {
  const scope = header.scope as string;
  const scopeId = header.scope_id as string;
  const author = header.author as string;
  const current = state.epoch(scope, scopeId);
  const inGrace =
    header.epoch === current - 1 &&
    rotatedAt !== null &&
    now - rotatedAt <= GRACE_MS &&
    (state.canRead(author, scope, scopeId) || state.supportReads(support, author, scopeId)) &&
    !new Set(excluded).has(author);
  if (header.epoch !== current && !inGrace) fail('epoch_stale');
}

/**
 * The hub's checks: reader checks, E5 at receipt, E6, E7. `rotatedAt` is the receipt time of the entry that created
 * the scope's current epoch; `sessionDevice` the device the header's session runs on (null if unknown, which fails
 * E7 for session kinds); `support` the current support roster (ADR-096: a person that left support writes nothing
 * new). Clients use it to predict the hub's answer (e.g. before sending an outbox item).
 */
export async function checkEventHub(
  event: unknown,
  log: VerifiedLog,
  now: number,
  rotatedAt: number | null,
  sessionDevice: string | null,
  excluded: Iterable<string> = [],
  support: Roster = NO_ROSTER,
): Promise<F.Fields> {
  const skip = [...excluded];
  const { header, rule: found } = await checkEvent(event, log, { excluded: skip, support });
  const state = log.state;
  const author = header.author as string;
  if ((state.member(author) === null && !support.has(author)) || !hasRight(found, state, header, support))
    fail('author_not_writer');
  checkEpoch(state, header, now, rotatedAt, skip, support);
  if (found.right === DEVICE_ASSIGNED_WG_SESSION && sessionDevice !== header.author) fail('author_not_writer');
  return header;
}

/**
 * E9: true if `incoming` repeats the stored event with the same `id` byte for byte (idempotent success), false for a
 * new `id`; different content under a known `id` is `event_conflict`.
 */
export function checkReplay(stored: unknown, incoming: unknown): boolean {
  if (stored === null || stored === undefined) return false;
  if (!equalBytes(canonical(stored), canonical(incoming))) fail('event_conflict');
  return true;
}
