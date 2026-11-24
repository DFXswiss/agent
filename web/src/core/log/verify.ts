// Membership-log verification L1-L10 and the state transition of every entry type (04-crypto.md sections 4.7,
// 6.1). TypeScript twin of `agent_hub.log.verify`. Each entry is checked against the state before it, in this order
// (the same in every implementation):
// 1. format (`checkLogEntry`: `header_invalid`, `log_type`, `non_ascii`);
// 2. L1 `seq` gapless from 0 (`log_chain`; the first entry of a hub append: `seq_conflict`), `org` unchanged
//    (`log_chain`); L2 `prev` = hash of the previous entry (`log_chain`);
// 3. L3 `genesis` exactly at `seq` 0 (`log_genesis`), `subject == by`, `pub.sig` = the pinned founder key and `sig`
//    valid under it (`log_genesis`);
// 4. `accept`: the invitation exists (`invite_not_in_log`), is not revoked (`invite_revoked`), L7 not yet accepted
//    (`invite_consumed`), not expired at hub receipt (`invite_expired`, only with `now`), then `by == subject`, kind
//    as invited, subject not an active member (a former member of the same kind may rejoin; a user with its earlier
//    identity keys), `sig` under `pub.sig` and `inv_sig` under `pk_inv` (`log_accept`);
// 5. every other type: L4 `by` known and `sig` valid under its key as known before the entry (`log_signature`);
//    L8 `by` not removed (`log_rights`);
// 6. L5 the type rule of section 4.7 (`log_rights`; `last_admin`; `invite_*` for `invite_revoke`);
//    L6 `rotate.epoch == current + 1` and `wg_create.epoch == 1` (`log_epoch`). `admit` (ADR-060): by an admin,
//    every workgroup writable and not the default one (`log_rights`), then the subject not an active principal (a
//    former user with the same identity keys may be admitted again), the subject's identity anchored at
//    `platform_seq` with `id_sig` and the admitting admin's identity (its keys in the state before the entry) at
//    `by_platform_seq` with `by_id_sig` (`PlatformState.identityAnchored`; without platform state refused)
//    (`log_admit`). The admitted user has not consented (`Principal.consented`): devices of this org are no
//    recipients of its credential key.
//    `agent_add` (ADR-082 sections 1 and 9): L4 with `by == subject` checks `sig` under `body.pub.sig` (the agent is
//    not yet a principal; L8 does not apply), with `by == user` under the user's key. L5 (`log_agent`): `user` an
//    active user member, `by` the user or the agent, the subject never a principal (except the same agent coming
//    back with its own `gen` and keys after it was removed together with its user), `cert` valid under the user's key,
//    `gen` above the generation of every other agent of the user in the log. An active agent of the user with a lower
//    generation is replaced: removed at the same `seq`. The `agent` member of `genesis`/`accept` (only for a user)
//    adds the agent by the same rule; the entry's `sig` is then made by the agent's key.
//    Every right of an agent author resolves through `OrgState.acting`; "self" rules (leaving, self-demotion) compare
//    `acting(by)` with the subject. `remove` of a user removes its active agent at the same `seq`.
// `now` (hub receipt time, unix ms) switches on the checks only the hub can make at append time (invitation expiry,
// platform access and a still valid certification for `device_assign` of a platform device). A reader replaying
// the log checks neither. L9 is `checkExtends` (stored heads, `refreshLog`), L10 `needsRefresh`/`requireLh`.
import { parseTime } from '../encoding/time';
import { fail } from '../crypto/errors';
import {
  type Body,
  checkExtends,
  checkLogEntry,
  type Entry,
  entryHash,
  type Head,
  logText,
  signatureValid,
  succeeds,
} from './entries';
import type { PlatformState } from './platform';
import { verifyCert } from '../crypto/agent';
import { b64uDecode } from '../encoding/b64u';
import type { AgentMember } from './entries';
import {
  ADMIN,
  AGENT,
  DEVICE,
  epochKey,
  type Invite,
  ORG,
  OrgState,
  lost,
  pairKey,
  PLAIN,
  type Principal,
  requiredSet,
  selfOwed,
  USER,
  WG,
  WRITE,
} from './state';

/** Options of `apply`/`appendEntries`: the platform state and the hub's append mode. */
export interface ApplyOptions {
  /** The verified platform-log state (needed for `device_assign` with `platform_seq`). */
  readonly platform?: PlatformState | null;
  /** Hub receipt time (unix ms); switches on the append-mode checks. */
  readonly now?: number | null;
  /** The org's platform access flag (ADR-026; only read in append mode, fail-closed). */
  readonly platformAccess?: boolean;
}

interface Context {
  readonly platform: PlatformState | null;
  readonly now: number | null;
  readonly platformAccess: boolean;
}

const rights = (allowed: boolean): void => {
  if (!allowed) fail('log_rights');
};

/** The agent an entry adds: `agent_add`, or the `agent` member of `genesis`/`accept`. */
interface NewAgent {
  readonly id: string;
  readonly user: string;
  readonly pub: { kem: string; sig: string };
  readonly gen: number;
  readonly cert: string;
}

const memberOf = (body: Body): AgentMember | undefined => body.agent as AgentMember | undefined;

const certValid = (userSig: string, agent: NewAgent): Promise<boolean> =>
  succeeds(() => verifyCert(b64uDecode(userSig), agent.cert, { ...agent, agent: agent.id }));

/**
 * L5 of a new agent (`log_agent`) and the state with it: the user is an active user member, the subject is new (or
 * the same agent returning after its user rejoined), the certificate holds and the generation is higher than that of
 * every other agent of the user; an active agent of the user is replaced (removed at this `seq`).
 */
async function addAgent(state: OrgState, agent: NewAgent, seq: number): Promise<OrgState> {
  const user = state.member(agent.user);
  const former = state.principals.get(agent.id);
  const returning =
    former === undefined ||
    (former.parent === agent.user &&
      former.removedWithUser &&
      former.gen === agent.gen &&
      former.kem === agent.pub.kem &&
      former.sig === agent.pub.sig);
  const others = state.agentsOf(agent.user).filter((other) => other.id !== agent.id);
  const valid =
    user !== null &&
    user.kind === USER &&
    returning &&
    others.every((other) => agent.gen > other.gen!) &&
    (await certValid(user.sig, agent));
  if (!valid) fail('log_agent');
  const principals = new Map(state.principals);
  const replaced = state.activeAgent(agent.user);
  if (replaced !== null) principals.set(replaced.id, { ...replaced, removedSeq: seq });
  principals.set(agent.id, {
    ...PLAIN,
    id: agent.id,
    kind: AGENT,
    role: AGENT,
    kem: agent.pub.kem,
    sig: agent.pub.sig,
    joinedSeq: seq,
    removedSeq: null,
    platformSeq: null,
    consented: true,
    parent: agent.user,
    gen: agent.gen,
  });
  const after = state.with({ principals });
  return replaced === null ? after : owe(after, requiredSet(state, after, replaced.id), agent.user);
}

/** The rotation of `scopes` is owed by `user`, who removed or replaced its agent (ADR-082 section 4). */
function owe(state: OrgState, scopes: Iterable<string>, user: string): OrgState {
  const agentOwed = new Map(state.agentOwed);
  for (const scope of scopes) agentOwed.set(scope, user);
  return state.with({ agentOwed });
}

/** `agent_add`: by the user or by the agent itself (nobody else holds a certificate for this key). */
function agentAdd(state: OrgState, body: Body, seq: number): Promise<OrgState> {
  if (body.by !== body.user && body.by !== body.subject) fail('log_agent');
  const agent: NewAgent = {
    id: body.subject as string,
    user: body.user as string,
    pub: body.pub as { kem: string; sig: string },
    gen: body.gen as number,
    cert: body.cert as string,
  };
  return addAgent(state, agent, seq);
}

/** The agent of the `agent` member of a user's `genesis`/`accept`. */
const joiningAgent = (body: Body, member: AgentMember): NewAgent => ({ ...member, user: body.subject as string });

async function genesis(body: Body, entry: Body, founder: string): Promise<OrgState> {
  const pub = body.pub as { kem: string; sig: string };
  const member = memberOf(body);
  const signer = member?.pub.sig ?? founder;
  const valid =
    body.subject === body.by && pub.sig === founder && (await signatureValid(signer, entry.sig, logText(body)));
  if (!valid) fail('log_genesis');
  const org = body.org as string;
  const wg = body.default_wg as string;
  const user = body.by as string;
  const founderPrincipal: Principal = {
    id: user,
    kind: USER,
    role: ADMIN,
    kem: pub.kem,
    sig: pub.sig,
    joinedSeq: 0,
    removedSeq: null,
    platformSeq: null,
    consented: true,
    ...PLAIN,
    joinAgent: member?.id ?? null,
  };
  const state = new OrgState({
    org,
    founder,
    defaultWg: wg,
    private: body.private as boolean,
    seq: 0,
    head: '',
    principals: new Map([[user, founderPrincipal]]),
    invites: new Map(),
    workgroups: new Map([[wg, { id: wg, isDefault: true, createdSeq: 0, archivedSeq: null }]]),
    access: new Map(),
    assignments: new Map(),
    orgAssignments: new Map(),
    epochs: new Map([
      [pairKey(ORG, org), 1],
      [pairKey(WG, wg), 1],
    ]),
    epochAuthors: new Map([
      [epochKey(ORG, org, 1), user],
      [epochKey(WG, wg, 1), user],
    ]),
    pending: new Set(),
    agentOwed: new Map(),
  });
  return member === undefined ? state : addAgent(state, joiningAgent(body, member), 0);
}

async function accept(state: OrgState, body: Body, entry: Body, seq: number, now: number | null): Promise<OrgState> {
  const invite = state.invites.get(body.invite_id as string);
  if (invite === undefined) fail('invite_not_in_log');
  if (invite.revokedSeq !== null) fail('invite_revoked');
  if (invite.acceptedSeq !== null) fail('invite_consumed');
  if (now !== null && now >= parseTime(invite.expires)) fail('invite_expired');
  const subject = body.subject as string;
  const pub = body.pub as { kem: string; sig: string };
  const member = memberOf(body);
  const former = state.principals.get(subject);
  const message = logText(body);
  const rejoinValid =
    former === undefined ||
    (former.removedSeq !== null &&
      former.kind === invite.kind &&
      // a user's identity never changes (section 4.7: `pub` equals the registered identity)
      (former.kind === DEVICE || (former.kem === pub.kem && former.sig === pub.sig)));
  const valid =
    body.by === subject &&
    body.kind === invite.kind &&
    rejoinValid &&
    (await signatureValid(member?.pub.sig ?? pub.sig, entry.sig, message)) &&
    (await signatureValid(invite.pkInv, entry.inv_sig, message));
  if (!valid) fail('log_accept');
  const principal: Principal = {
    id: subject,
    kind: invite.kind,
    role: invite.kind === USER ? invite.role! : DEVICE,
    kem: pub.kem,
    sig: pub.sig,
    joinedSeq: seq,
    removedSeq: null,
    platformSeq: null,
    consented: true,
    ...PLAIN,
    joinAgent: member?.id ?? null,
  };
  const access = new Map(state.access);
  const assignments = new Map(state.assignments);
  for (const [wg, level] of Object.entries(invite.wgs)) {
    if (invite.kind === USER) access.set(pairKey(subject, wg), level);
    else assignments.set(pairKey(subject, wg), seq);
  }
  const after = state.with({
    principals: new Map(state.principals).set(subject, principal),
    invites: new Map(state.invites).set(invite.inviteId, { ...invite, acceptedSeq: seq }),
    access,
    assignments,
  });
  return member === undefined ? after : addAgent(after, joiningAgent(body, member), seq);
}

function inviteEntry(state: OrgState, body: Body, seq: number): OrgState {
  const wgs = body.wgs as Record<string, string>;
  const user = body.kind === USER;
  rights(
    !state.invites.has(body.invite_id as string) &&
      Object.keys(wgs).every((wg) => state.writable(wg) && !(user && wg === state.defaultWg)),
  );
  const invite: Invite = {
    inviteId: body.invite_id as string,
    pkInv: body.pk_inv as string,
    kind: body.kind as string,
    role: (body.role as string | undefined) ?? null,
    wgs: { ...wgs },
    expires: body.expires as string,
    seq,
    revokedSeq: null,
    acceptedSeq: null,
  };
  return state.with({ invites: new Map(state.invites).set(invite.inviteId, invite) });
}

function inviteRevoke(state: OrgState, body: Body, seq: number): OrgState {
  const invite = state.invites.get(body.invite_id as string);
  if (invite === undefined) fail('invite_not_in_log');
  if (invite.acceptedSeq !== null) fail('invite_consumed');
  if (invite.revokedSeq !== null) fail('invite_revoked');
  return state.with({ invites: new Map(state.invites).set(invite.inviteId, { ...invite, revokedSeq: seq }) });
}

function role(state: OrgState, body: Body): OrgState {
  const principal = state.member(body.subject as string);
  if (principal === null || principal.kind !== USER || principal.role === body.role) fail('log_rights');
  if (principal.role === ADMIN && state.admins().length === 1) fail('last_admin');
  let after = state.with({
    principals: new Map(state.principals).set(principal.id, { ...principal, role: body.role as string }),
  });
  if (selfOwed(state, body)) {
    // self-demotion (also through its agent): like leaving, the subject cannot rotate what it no longer reads
    // (ADR-058 section 3)
    after = after.with({ pending: new Set([...after.pending, ...lost(state, after, body)]) });
  }
  return after;
}

function wgCreate(state: OrgState, body: Body, seq: number): OrgState {
  const wg = body.wg as string;
  const by = body.by as string;
  rights(!state.workgroups.has(wg) && wg !== state.org);
  if (body.epoch !== 1) fail('log_epoch');
  return state.with({
    workgroups: new Map(state.workgroups).set(wg, { id: wg, isDefault: false, createdSeq: seq, archivedSeq: null }),
    access: new Map(state.access).set(pairKey(state.acting(by), wg), WRITE),
    epochs: new Map(state.epochs).set(pairKey(WG, wg), 1),
    epochAuthors: new Map(state.epochAuthors).set(epochKey(WG, wg, 1), by),
  });
}

function wgArchive(state: OrgState, body: Body, seq: number): OrgState {
  const wg = body.wg as string;
  rights(state.writable(wg) && wg !== state.defaultWg);
  const archived = { ...state.workgroups.get(wg)!, archivedSeq: seq };
  return state.with({ workgroups: new Map(state.workgroups).set(wg, archived) });
}

function grant(state: OrgState, body: Body): OrgState {
  const subject = body.subject as string;
  const wg = body.wg as string;
  const found = state.member(subject);
  rights(
    found !== null &&
      found.kind === USER &&
      state.writable(wg) &&
      wg !== state.defaultWg &&
      state.access.get(pairKey(subject, wg)) !== body.access,
  );
  return state.with({ access: new Map(state.access).set(pairKey(subject, wg), body.access as string) });
}

function ungrant(state: OrgState, body: Body): OrgState {
  const subject = body.subject as string;
  const key = pairKey(subject, body.wg as string);
  rights(state.member(subject) !== null && state.access.has(key));
  const access = new Map(state.access);
  access.delete(key);
  return state.with({ access });
}

function platformDevice(state: OrgState, body: Body, seq: number, ctx: Context): Principal {
  const device = body.device as string;
  const cert = ctx.platform?.certification(body.platform_seq as number) ?? null;
  if (cert === null || cert.device !== device) fail('log_rights');
  // at the hub: the org has platform access (ADR-026) and this certification is still valid
  rights(ctx.now === null || (ctx.platformAccess && ctx.platform!.valid(cert.seq)));
  const found = state.member(device);
  if (found !== null) {
    rights(found.platformSeq === cert.seq);
    return found;
  }
  return {
    id: device,
    kind: DEVICE,
    role: DEVICE,
    kem: cert.kem,
    sig: cert.sig,
    joinedSeq: seq,
    removedSeq: null,
    platformSeq: cert.seq,
    consented: true,
    ...PLAIN,
  };
}

/** The device of a `device_assign`/`device_assign_org`: a certified platform device (with `platform_seq`) or an
 * active org device. */
function assignable(state: OrgState, body: Body, seq: number, ctx: Context): Principal {
  if (Object.hasOwn(body, 'platform_seq')) return platformDevice(state, body, seq, ctx);
  const found = state.member(body.device as string);
  if (found === null || found.kind !== DEVICE || found.platformSeq !== null) fail('log_rights');
  return found;
}

function deviceAssign(state: OrgState, body: Body, seq: number, ctx: Context): OrgState {
  const device = body.device as string;
  const wg = body.wg as string;
  rights(state.writable(wg) && !state.assignments.has(pairKey(device, wg)) && !state.orgAssignments.has(device));
  const principal = assignable(state, body, seq, ctx);
  return state.with({
    principals: new Map(state.principals).set(device, principal),
    assignments: new Map(state.assignments).set(pairKey(device, wg), seq),
  });
}

function deviceUnassign(state: OrgState, body: Body): OrgState {
  const key = pairKey(body.device as string, body.wg as string);
  rights(state.member(body.device as string) !== null && state.assignments.has(key));
  const assignments = new Map(state.assignments);
  assignments.delete(key);
  return state.with({ assignments });
}

async function admit(state: OrgState, body: Body, seq: number, ctx: Context): Promise<OrgState> {
  const subject = body.subject as string;
  const by = body.by as string;
  const pub = body.pub as { kem: string; sig: string };
  const wgs = body.wgs as Record<string, string>;
  rights(Object.keys(wgs).every((wg) => state.writable(wg) && wg !== state.defaultWg));
  const former = state.principals.get(subject);
  const rejoinValid =
    former === undefined ||
    (former.removedSeq !== null && former.kind === USER && former.kem === pub.kem && former.sig === pub.sig);
  // an agent admits with its user's anchored identity
  const anchor = state.acting(by);
  const author = state.principals.get(anchor)!;
  const { platform } = ctx;
  const current = ctx.now !== null;
  const anchored =
    platform !== null &&
    (await platform.identityAnchored(body.platform_seq as number, subject, pub, body.id_sig, current)) &&
    (await platform.identityAnchored(
      body.by_platform_seq as number,
      anchor,
      { kem: author.kem, sig: author.sig },
      body.by_id_sig,
      current,
    ));
  if (!(rejoinValid && anchored)) fail('log_admit');
  const principal: Principal = {
    id: subject,
    kind: USER,
    role: body.role as string,
    kem: pub.kem,
    sig: pub.sig,
    joinedSeq: seq,
    removedSeq: null,
    platformSeq: null,
    consented: false,
    ...PLAIN,
  };
  const access = new Map(state.access);
  for (const [wg, level] of Object.entries(wgs)) access.set(pairKey(subject, wg), level);
  return state.with({ principals: new Map(state.principals).set(subject, principal), access });
}

function deviceAssignOrg(state: OrgState, body: Body, seq: number, ctx: Context): OrgState {
  const device = body.device as string;
  rights(!state.orgAssignments.has(device));
  const principal = assignable(state, body, seq, ctx);
  return state.with({
    principals: new Map(state.principals).set(device, principal),
    orgAssignments: new Map(state.orgAssignments).set(device, seq),
  });
}

function deviceUnassignOrg(state: OrgState, body: Body): OrgState {
  const device = body.device as string;
  rights(state.orgAssigned(device));
  const orgAssignments = new Map(state.orgAssignments);
  orgAssignments.delete(device);
  return state.with({ orgAssignments });
}

function without<T>(map: ReadonlyMap<string, T>, principal: string): Map<string, T> {
  return new Map([...map].filter(([key]) => !key.startsWith(`${principal} `)));
}

function remove(state: OrgState, body: Body, seq: number): OrgState {
  const subject = body.subject as string;
  const by = body.by as string;
  const found = state.member(subject);
  // the subject itself, the user through its agent, the user (or its agent) removing its agent, or an admin
  const acting = state.acting(by);
  if (found === null || !(acting === subject || acting === found.parent || state.isAdmin(by))) fail('log_rights');
  if (found.role === ADMIN && state.admins().length === 1) fail('last_admin');
  // an agent removed by its user, the user's agent or itself is revoked in every log (`wraps.revokedAgents`)
  const byOwner = found.parent !== null && acting === found.parent;
  const principals = new Map(state.principals).set(subject, { ...found, removedSeq: seq, removedByOwner: byOwner });
  // "with its user" (ADR-082 section 9.2): the agent may come back when the user rejoins
  const agent = state.activeAgent(subject);
  if (agent !== null) principals.set(agent.id, { ...agent, removedSeq: seq, removedWithUser: true });
  let after = state.with({
    principals,
    access: without(state.access, subject),
    assignments: without(state.assignments, subject),
    orgAssignments: new Map([...state.orgAssignments].filter(([device]) => device !== subject)),
  });
  if (selfOwed(state, body)) {
    // leaving: the leaver cannot rotate (L8), so the rotation is owed by the remaining holders
    after = after.with({ pending: new Set([...after.pending, ...lost(state, after, body)]) });
  } else if (byOwner) {
    // the user (or its agent) removes its agent: it may rotate what the agent read without admin or write right
    after = owe(after, lost(state, after, body), found.parent!);
  }
  return after;
}

function rotate(state: OrgState, body: Body): OrgState {
  const scope = body.scope as string;
  const scopeId = body.scope_id as string;
  const by = body.by as string;
  // the user that removed its agent (or its active agent) rotates what that agent read
  const owed = state.agentOwed.get(pairKey(scope, scopeId)) === state.acting(by);
  if (scope === ORG) rights(scopeId === state.org && (state.isAdmin(by) || owed));
  else rights(state.workgroups.has(scopeId) && (state.isAdmin(by) || owed || state.wgAccess(by, scopeId) === WRITE));
  const epoch = state.epoch(scope, scopeId) + 1;
  if (body.epoch !== epoch) fail('log_epoch');
  const pending = new Set(state.pending);
  pending.delete(pairKey(scope, scopeId));
  const agentOwed = new Map(state.agentOwed);
  agentOwed.delete(pairKey(scope, scopeId));
  return state.with({
    epochs: new Map(state.epochs).set(pairKey(scope, scopeId), epoch),
    epochAuthors: new Map(state.epochAuthors).set(epochKey(scope, scopeId, epoch), by),
    pending,
    agentOwed,
  });
}

type Transition = (state: OrgState, body: Body, seq: number, ctx: Context) => OrgState | Promise<OrgState>;
const ADMIN_ONLY: Record<string, Transition> = {
  invite: (state, body, seq) => inviteEntry(state, body, seq),
  invite_revoke: (state, body, seq) => inviteRevoke(state, body, seq),
  role: (state, body) => role(state, body),
  wg_create: (state, body, seq) => wgCreate(state, body, seq),
  wg_archive: (state, body, seq) => wgArchive(state, body, seq),
  grant: (state, body) => grant(state, body),
  ungrant: (state, body) => ungrant(state, body),
  device_assign: deviceAssign,
  device_unassign: (state, body) => deviceUnassign(state, body),
  admit,
  device_assign_org: deviceAssignOrg,
  device_unassign_org: (state, body) => deviceUnassignOrg(state, body),
};
const OTHERS: Record<string, Transition> = {
  remove: (state, body, seq) => remove(state, body, seq),
  rotate: (state, body) => rotate(state, body),
  agent_add: (state, body, seq) => agentAdd(state, body, seq),
};

function applyType(state: OrgState, body: Body, seq: number, ctx: Context): OrgState | Promise<OrgState> {
  const type = body.type as string;
  const adminOnly = Object.hasOwn(ADMIN_ONLY, type);
  rights(!adminOnly || state.isAdmin(body.by as string));
  return (adminOnly ? ADMIN_ONLY : OTHERS)[type]!(state, body, seq, ctx);
}

/**
 * The state after one more entry, or a refusal (checks in the order of the module comment). `founder` is the
 * pinned founder signing key (b64u) checked by `genesis`.
 */
export async function apply(
  state: OrgState | null,
  entry: unknown,
  founder: string,
  options: ApplyOptions = {},
  firstOfAppend = false,
): Promise<OrgState> {
  const body = checkLogEntry(entry);
  const data = entry as Body;
  const seq = state === null ? 0 : state.seq + 1;
  if (body.seq !== seq) fail(firstOfAppend ? 'seq_conflict' : 'log_chain');
  if (state !== null && body.org !== state.org) fail('log_chain');
  if (body.prev !== (state === null ? '' : state.head)) fail('log_chain');
  if ((state === null) !== (body.type === 'genesis')) fail('log_genesis');
  let after: OrgState;
  if (state === null) {
    after = await genesis(body, data, founder);
  } else if (body.type === 'accept') {
    after = await accept(state, body, data, seq, options.now ?? null);
  } else {
    // an agent adding itself is not a principal yet: its key is the one it brings (L8 does not apply)
    const self = body.type === 'agent_add' && body.by === body.subject;
    const key = self ? (body.pub as { sig: string }).sig : state.principals.get(body.by as string)?.sig;
    if (key === undefined || !(await signatureValid(key, data.sig, logText(body)))) fail('log_signature');
    rights(self || state.member(body.by as string) !== null);
    const ctx = {
      platform: options.platform ?? null,
      now: options.now ?? null,
      platformAccess: !!options.platformAccess,
    };
    after = await applyType(state, body, seq, ctx);
  }
  return after.with({ seq, head: await entryHash(data) });
}

/** A verified membership log: its entries and the state after each entry (`states[seq]`). */
export class VerifiedLog {
  constructor(
    readonly entries: readonly Entry[],
    readonly states: readonly OrgState[],
  ) {}

  /** The state at the head. */
  get state(): OrgState {
    return this.states[this.states.length - 1]!;
  }

  /** `(seq, hash)` of the last entry. */
  get head(): Head {
    return { seq: this.state.seq, hash: this.state.head };
  }

  /** The state after entry `seq` (E5 "right at `lh`"); `seq` beyond the head is `log_fork` (L10). */
  stateAt(seq: number): OrgState {
    requireLh(this, seq);
    return this.states[seq]!;
  }

  /**
   * Principals that held the key of `(scope, scope_id, epoch)` by the log: its creator and every principal that
   * could read the scope in some state in which this epoch was current.
   */
  epochHolders(scope: string, scopeId: string, epoch: number): Set<string> {
    const holders = new Set<string>();
    for (const state of this.states) {
      if (state.epoch(scope, scopeId) !== epoch) continue;
      for (const principal of state.principals.keys()) {
        if (state.canRead(principal, scope, scopeId)) holders.add(principal);
      }
    }
    const author = this.state.epochAuthors.get(epochKey(scope, scopeId, epoch));
    if (author !== undefined) holders.add(author);
    return holders;
  }
}

/**
 * Appends entries to a verified log (hub append semantics: a wrong first `seq` is `seq_conflict`). For a new org
 * `log` is null and `founder` the creator's registered signing key; otherwise the log's pinned founder is used.
 */
export async function appendEntries(
  log: VerifiedLog | null,
  entries: readonly unknown[],
  founder: string,
  options: ApplyOptions = {},
): Promise<VerifiedLog> {
  const states = [...(log?.states ?? [])];
  const added = [...(log?.entries ?? [])];
  const pinned = log === null ? founder : log.state.founder;
  for (const [index, entry] of entries.entries()) {
    states.push(await apply(states[states.length - 1] ?? null, entry, pinned, options, index === 0));
    added.push(entry as Entry);
  }
  if (states.length === 0) fail('log_empty');
  return new VerifiedLog(added, states);
}

/** Verifies a whole log from `seq` 0 against the pinned founder key (L1-L8); `log_empty` for no entries. */
export async function verifyLog(
  entries: readonly unknown[],
  founder: string,
  platform: PlatformState | null = null,
): Promise<VerifiedLog> {
  if (entries.length === 0) fail('log_empty');
  const states: OrgState[] = [];
  for (const entry of entries)
    states.push(await apply(states[states.length - 1] ?? null, entry, founder, { platform }));
  return new VerifiedLog(entries as Entry[], states);
}

/**
 * The reader's incremental verification: the entries after the verified head (a page of `GET /v1/orgs/{org}/log
 * ?after=`) checked against the state at the head, exactly as a full verification would (a gap is `log_chain`).
 */
export async function extendLog(
  log: VerifiedLog,
  entries: readonly unknown[],
  platform: PlatformState | null = null,
): Promise<VerifiedLog> {
  const states = [...log.states];
  for (const entry of entries)
    states.push(await apply(states[states.length - 1]!, entry, log.state.founder, { platform }));
  return new VerifiedLog([...log.entries, ...(entries as Entry[])], states);
}

/** L10, first half: an event names a head beyond the verified one, so the reader refreshes the log. */
export function needsRefresh(log: VerifiedLog, lh: number): boolean {
  return lh > log.head.seq;
}

/** L10, second half: after the refresh the log must contain `lh` (`log_fork` otherwise). */
export function requireLh(log: VerifiedLog, lh: number): void {
  if (needsRefresh(log, lh)) fail('log_fork');
}

/** What the client keeps per org between sessions (L3 founder key, L9 head). */
export interface Pinned {
  readonly founder: string;
  readonly head: Head | null;
}

/**
 * Log verification job (06-web.md section 11, job 1): the complete log as served by the hub, checked to extend the
 * stored head (L9, `log_rollback`) and verified from `seq` 0 against the pinned founder key (L1-L8). The caller
 * stores the returned log's head; any refusal blocks the org.
 */
export async function refreshLog(
  pinned: Pinned,
  entries: readonly unknown[],
  platform: PlatformState | null = null,
): Promise<VerifiedLog> {
  await checkExtends(pinned.head, entries);
  return verifyLog(entries, pinned.founder, platform);
}

/**
 * L10 for an event with head `lh`: if `lh` is beyond the verified head, `refresh` fetches and verifies the log again
 * (`refreshLog`); the refreshed log must contain `lh` (`log_fork`). Returns the log to check the event against.
 */
export async function resolveLh(
  log: VerifiedLog,
  lh: number,
  refresh: () => Promise<VerifiedLog>,
): Promise<VerifiedLog> {
  if (!needsRefresh(log, lh)) return log;
  const refreshed = await refresh();
  requireLh(refreshed, lh);
  return refreshed;
}
