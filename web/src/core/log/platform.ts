// Platform log verification P1-P4 and the certification state (04-crypto.md sections 4.8 and 6.2, ADR-023, ADR-026).
// TypeScript twin of `agent_hub.log.platform`.
//
// Every entry is signed by the platform root (label `platform-log\0`); the browser pins the root public key compiled
// into the build (`VITE_PLATFORM_ROOT_KEY`, 06-web.md section 10.4), which the app passes in as `root` (like the
// start-up config check: src/core never reads the build environment). Checks per entry, in this order:
// 1. format (`checkPlatformEntry`);
// 2. P1 `seq` gapless from 0 (`log_chain`; the first entry of an append: `seq_conflict`), P2 `prev` (`log_chain`);
// 3. P3 `root` exactly at `seq` 0 (`log_genesis`) and its `pub` equal to the pinned key (`platform_root_mismatch`);
// 4. P4 signature under the pinned root key (`log_signature`);
// 5. type rules (`log_rights` unless stated): invitation IDs unique; `device_certify` names an open `device_invite`
//    (`invite_not_in_log`, used once: `invite_consumed`) and carries valid `enroll_sig`/`inv_sig` over the
//    enrollment text rebuilt from the entry (`log_accept`), and the device is not currently certified;
//    `device_decertify` names a certified device; `role_grant`/`role_revoke` change the role.
// A certification is valid until a later `device_decertify` of the device (section 6.2). Validity is per
// certification (`valid(seq)`): a device decertified and certified again with new keys does not make its old
// certification, and the old keys an org learnt from it, valid again (`excluded`).
import { b64uDecode } from '../encoding/b64u';
import { fail } from '../crypto/errors';
import { verifyEnrollment } from '../crypto/enroll';
import {
  type Body,
  checkExtends,
  checkPlatformEntry,
  type Entry,
  entryHash,
  type Head,
  platformText,
  signatureValid,
  succeeds,
} from './entries';
import { compareText, type OrgState, pairKey, splitKey } from './state';

export const CERTIFIED = 'certified';
export const DECERTIFIED = 'decertified';
export const UNKNOWN = 'unknown';

/** A `device_certify` entry (`seq`) and its end, if any. */
export interface Certification {
  readonly device: string;
  readonly kem: string;
  readonly sig: string;
  readonly seq: number;
  readonly inviteId: string;
  readonly decertifiedSeq: number | null;
}

/** A `device_invite` or `user_invite` (both carry `pk_inv`; ADR-043 added it to `user_invite`). */
export interface PlatformInvite {
  readonly inviteId: string;
  readonly kind: string;
  readonly pkInv: string;
  readonly expires: string;
  readonly seq: number;
  readonly certifiedSeq: number | null;
}

export interface PlatformStateFields {
  readonly root: string;
  readonly seq: number;
  readonly head: string;
  readonly invites: ReadonlyMap<string, PlatformInvite>;
  readonly certifications: ReadonlyMap<number, Certification>;
  /** `device -> seq` of its latest certification. */
  readonly devices: ReadonlyMap<string, number>;
  /** `pairKey(user, role) -> seq` of the grant. */
  readonly roles: ReadonlyMap<string, number>;
  readonly orgAccess: ReadonlyMap<string, boolean>;
}

/** The state after the platform-log entry `seq`. */
export class PlatformState implements PlatformStateFields {
  declare readonly root: string;
  declare readonly seq: number;
  declare readonly head: string;
  declare readonly invites: ReadonlyMap<string, PlatformInvite>;
  declare readonly certifications: ReadonlyMap<number, Certification>;
  declare readonly devices: ReadonlyMap<string, number>;
  declare readonly roles: ReadonlyMap<string, number>;
  declare readonly orgAccess: ReadonlyMap<string, boolean>;

  constructor(fields: PlatformStateFields) {
    Object.assign(this, fields);
    Object.freeze(this);
  }

  /** A copy with some fields replaced. */
  with(changes: Partial<PlatformStateFields>): PlatformState {
    return new PlatformState({ ...this, ...changes });
  }

  /** The certification made by the entry `seq` (null if that entry is not a `device_certify`). */
  certification(seq: number): Certification | null {
    return this.certifications.get(seq) ?? null;
  }

  /** `certified`, `decertified` or `unknown`. */
  status(device: string): string {
    const latest = this.devices.get(device);
    if (latest === undefined) return UNKNOWN;
    return this.certifications.get(latest)!.decertifiedSeq === null ? CERTIFIED : DECERTIFIED;
  }

  /** The entry `seq` is a `device_certify` that no later `device_decertify` has ended (section 6.2). */
  valid(seq: number): boolean {
    const found = this.certifications.get(seq);
    return found !== undefined && found.decertifiedSeq === null;
  }

  /**
   * Platform devices of an org whose certification (the one their keys come from, `platformSeq`) has ended: they
   * are no recipients, readers or authors any more, also after a re-certification with new keys. Sorted.
   */
  excluded(org: OrgState): string[] {
    return [...org.principals.values()]
      .filter((p) => p.platformSeq !== null && !this.valid(p.platformSeq))
      .map((p) => p.id)
      .sort();
  }

  /** Platform role (e.g. `inviter`). */
  hasRole(user: string, role: string): boolean {
    return this.roles.has(pairKey(user, role));
  }

  /** ADR-026: a private org has platform access; any other org once the latest `org_platform_access` grants it. */
  platformAccess(org: string, isPrivate: boolean): boolean {
    return isPrivate || this.orgAccess.get(org) === true;
  }

  /** `(seq, hash)` of the last entry. */
  headValue(): Head {
    return { seq: this.seq, hash: this.head };
  }

  /** A JSON view of the state (vectors). */
  projection(): Record<string, unknown> {
    return {
      head: { seq: this.seq, hash: this.head },
      devices: [...this.devices.entries()]
        .sort(([a], [b]) => compareText(a, b))
        .map(([device, seq]) => ({ device, status: this.status(device), certify_seq: seq })),
      invites: [...this.invites.values()]
        .sort((a, b) => compareText(a.inviteId, b.inviteId))
        .map((inv) => ({ invite_id: inv.inviteId, kind: inv.kind, seq: inv.seq, certified_seq: inv.certifiedSeq })),
      roles: [...this.roles.keys()].sort(compareText).map((key) => {
        const [user, role] = splitKey(key);
        return { user, role };
      }),
      org_access: Object.fromEntries([...this.orgAccess.entries()].sort(([a], [b]) => compareText(a, b))),
    };
  }
}

/** Entries and the state after each of them. */
export class VerifiedPlatformLog {
  constructor(
    readonly entries: readonly Entry[],
    readonly states: readonly PlatformState[],
  ) {}

  /** The current state. */
  get state(): PlatformState {
    return this.states[this.states.length - 1]!;
  }
}

function enrollmentValid(body: Body, pkInv: string): Promise<boolean> {
  const signed = {
    device_id: body.device,
    kind: 'platform',
    invite_id: body.invite_id,
    pub: body.pub,
    agent_version: body.agent_version,
    os: body.os,
    arch: body.arch,
    capacity: body.capacity,
    enroll_sig: body.enroll_sig,
    inv_sig: body.inv_sig,
  };
  return succeeds(() => verifyEnrollment(signed, b64uDecode(pkInv)));
}

function invite(state: PlatformState, body: Body, seq: number): PlatformState {
  const inviteId = body.invite_id as string;
  if (state.invites.has(inviteId)) fail('log_rights');
  const kind = body.type === 'device_invite' ? 'device' : 'user';
  const pkInv = body.pk_inv as string;
  const found: PlatformInvite = { inviteId, kind, pkInv, expires: body.expires as string, seq, certifiedSeq: null };
  return state.with({ invites: new Map(state.invites).set(inviteId, found) });
}

async function certify(state: PlatformState, body: Body, seq: number): Promise<PlatformState> {
  const found = state.invites.get(body.invite_id as string);
  if (found === undefined || found.kind !== 'device') fail('invite_not_in_log');
  if (found.certifiedSeq !== null) fail('invite_consumed');
  if (!(await enrollmentValid(body, found.pkInv))) fail('log_accept');
  const device = body.device as string;
  if (state.status(device) === CERTIFIED) fail('log_rights');
  const pub = body.pub as { kem: string; sig: string };
  const cert: Certification = {
    device,
    kem: pub.kem,
    sig: pub.sig,
    seq,
    inviteId: found.inviteId,
    decertifiedSeq: null,
  };
  return state.with({
    invites: new Map(state.invites).set(found.inviteId, { ...found, certifiedSeq: seq }),
    certifications: new Map(state.certifications).set(seq, cert),
    devices: new Map(state.devices).set(device, seq),
  });
}

function decertify(state: PlatformState, body: Body, seq: number): PlatformState {
  const device = body.device as string;
  if (state.status(device) !== CERTIFIED) fail('log_rights');
  const latest = state.devices.get(device)!;
  const ended = { ...state.certifications.get(latest)!, decertifiedSeq: seq };
  return state.with({ certifications: new Map(state.certifications).set(latest, ended) });
}

function role(state: PlatformState, body: Body, seq: number): PlatformState {
  const key = pairKey(body.subject as string, body.role as string);
  const grant = body.type === 'role_grant';
  if (state.roles.has(key) === grant) fail('log_rights');
  const roles = new Map(state.roles);
  if (grant) roles.set(key, seq);
  else roles.delete(key);
  return state.with({ roles });
}

function orgAccess(state: PlatformState, body: Body): PlatformState {
  return state.with({ orgAccess: new Map(state.orgAccess).set(body.org as string, body.granted as boolean) });
}

type Transition = (state: PlatformState, body: Body, seq: number) => PlatformState | Promise<PlatformState>;
const TRANSITIONS: Record<string, Transition> = {
  device_invite: invite,
  user_invite: invite,
  device_certify: certify,
  device_decertify: decertify,
  role_grant: role,
  role_revoke: role,
  org_platform_access: orgAccess,
};

/** The state after one more entry, or a refusal (P1-P4 and the type rules). */
export async function applyPlatform(
  state: PlatformState | null,
  entry: unknown,
  root: string,
  firstOfAppend = false,
): Promise<PlatformState> {
  const body = checkPlatformEntry(entry);
  const seq = state === null ? 0 : state.seq + 1;
  if (body.seq !== seq) fail(firstOfAppend ? 'seq_conflict' : 'log_chain');
  if (body.prev !== (state === null ? '' : state.head)) fail('log_chain');
  if ((state === null) !== (body.type === 'root')) fail('log_genesis');
  if (state === null && body.pub !== root) fail('platform_root_mismatch');
  if (!(await signatureValid(root, (entry as Body).sig, platformText(body)))) fail('log_signature');
  const head = await entryHash(entry);
  if (state === null) {
    return new PlatformState({
      root,
      seq: 0,
      head,
      invites: new Map(),
      certifications: new Map(),
      devices: new Map(),
      roles: new Map(),
      orgAccess: new Map(),
    });
  }
  return (await TRANSITIONS[body.type as string]!(state, body, seq)).with({ seq, head });
}

/** Appends entries to a verified log (a wrong first `seq` is `seq_conflict`). */
export async function appendPlatform(
  log: VerifiedPlatformLog | null,
  entries: readonly unknown[],
  root: string,
): Promise<VerifiedPlatformLog> {
  const states = [...(log?.states ?? [])];
  const added = [...(log?.entries ?? [])];
  for (const [index, entry] of entries.entries()) {
    states.push(await applyPlatform(states[states.length - 1] ?? null, entry, root, index === 0));
    added.push(entry as Entry);
  }
  if (states.length === 0) fail('log_empty');
  return new VerifiedPlatformLog(added, states);
}

/** Verifies a whole platform log against the pinned root key (the key compiled into the build, never the hub's). */
export async function verifyPlatformLog(entries: readonly unknown[], root: string): Promise<VerifiedPlatformLog> {
  if (entries.length === 0) fail('log_empty');
  const states: PlatformState[] = [];
  for (const entry of entries) states.push(await applyPlatform(states[states.length - 1] ?? null, entry, root));
  return new VerifiedPlatformLog(entries as Entry[], states);
}

/**
 * Platform-log verification job (06-web.md section 11, job 1): the complete platform log as served by the hub must
 * extend the stored verified head (`log_rollback`, as L9 for the membership log: a hub that drops a later
 * `device_decertify` is detected) and verify against the compiled root key.
 */
export async function refreshPlatformLog(
  stored: Head | null,
  entries: readonly unknown[],
  root: string,
): Promise<VerifiedPlatformLog> {
  await checkExtends(stored, entries);
  return verifyPlatformLog(entries, root);
}

/**
 * The decertified platform devices of an org for code that creates wraps: an org with platform devices needs the
 * verified platform state (without it a decertified device would count as a recipient; `log_rights`).
 */
export function excludedFor(platform: PlatformState | null, org: OrgState): string[] {
  if (platform !== null) return platform.excluded(org);
  if ([...org.principals.values()].some((p) => p.platformSeq !== null)) fail('log_rights');
  return [];
}
