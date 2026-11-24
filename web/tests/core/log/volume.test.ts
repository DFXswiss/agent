// KOR-35 (ADR-086): the first sign-in into a big org verifies its membership log and the platform log and then checks
// wraps and catch-up offers against them. That work must grow linearly with the logs: a log of about 950 entries took
// over a minute in the browser and 3 300 entries more than five minutes (the rights questions asked every state of
// the log for every slot, and every entry copied the growing maps of the state). The benchmark builds an org shaped
// like the volume seed (invitations and accepts of members and devices, open invitations) and a platform log of user
// invitations with the real builders, runs the sign-in work for 500 and 4 000 entries and counts it: the rights
// questions (`OrgState.canRead`, `OrgState.deviceWgs`) and every map write (`Map.prototype.set`, which also counts the
// entries a `new Map(map)` copies). The counts must grow near-linearly and stay below a bound per entry; they do not
// depend on the machine (the times are printed for information only, never asserted). The results are compared too:
// the fast paths must answer exactly as the definitions.
import { describe, expect, it, vi } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { ed25519KeyPair, Identity } from '../../../src/core/crypto/keys';
import { credentialLosses } from '../../../src/core/credentials/rotation';
import { confirmedSlots } from '../../../src/core/log/catchup';
import {
  buildEntries,
  buildPlatformEntries,
  entryHash,
  type Entry,
  type Head,
  type LogFields,
  type LogSigner,
} from '../../../src/core/log/entries';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { ORG, OrgState, WG } from '../../../src/core/log/state';
import { verifyLog, type VerifiedLog } from '../../../src/core/log/verify';
import { mayWrap } from '../../../src/core/log/wraps';
import { actor, EXPIRES, idOf, inviteFor, seed, TS } from './world';

interface Org {
  readonly founder: string;
  readonly founderSig: string;
  readonly org: string;
  readonly wg: string;
  readonly entries: Entry[];
  readonly platform: Entry[];
  readonly root: string;
}

const orgs = new Map<number, Org>();

/** About `n` entries: per 7 principals 4 users and 3 devices (invite + accept each), every 5th invitation left open. */
async function org(n: number): Promise<Org> {
  const cached = orgs.get(n);
  if (cached !== undefined) return cached;
  const founder = await actor('founder');
  const org = await idOf('org');
  const wg = await idOf('default wg');
  const signer: LogSigner = { id: founder.id, key: founder.identity.sig.privateKey };
  const member = await Identity.fromRaw(await seed('kem member'), await seed('sig member'));
  const entries: Entry[] = [];
  let head: Head | null = null;
  const add = async (by: LogSigner, fields: LogFields) => {
    const [entry] = await buildEntries({ org, head }, by, [fields], TS);
    entries.push(entry!);
    head = { seq: entries.length - 1, hash: await entryHash(entry) };
  };
  await add(signer, {
    type: 'genesis',
    subject: founder.id,
    pub: founder.identity.public(),
    default_wg: wg,
    private: false,
  });
  for (let i = 0; entries.length < n; i += 1) {
    const device = i % 7 >= 4;
    const invite = await inviteFor(`volume ${i}`);
    const common = { type: 'invite', invite_id: invite.inviteId, pk_inv: invite.pkInv, expires: EXPIRES } as const;
    await add(
      signer,
      device
        ? { ...common, kind: 'device', wgs: { [wg]: 'device' } }
        : { ...common, kind: 'user', role: 'member', wgs: {} },
    );
    if (i % 5 === 4 || entries.length === n) continue;
    const subject = await idOf(`volume ${i}`);
    await add(
      { id: subject, key: member.sig.privateKey, invite: invite.signing.privateKey },
      { type: 'accept', invite_id: invite.inviteId, subject, kind: device ? 'device' : 'user', pub: member.public() },
    );
  }
  const root = await ed25519KeyPair(await seed('platform root'));
  const list = [{ type: 'root', pub: b64uEncode(root.publicKey) }];
  for (let i = 1; i < n / 2; i += 1) {
    const invite = await inviteFor(`platform ${i}`);
    list.push({ type: 'user_invite', invite_id: invite.inviteId, pk_inv: invite.pkInv, expires: EXPIRES } as never);
  }
  const platform = await buildPlatformEntries(null, root.privateKey, list, TS);
  const built = {
    founder: founder.id,
    founderSig: b64uEncode(founder.identity.sigPublic()),
    org,
    wg,
    entries,
    platform,
    root: b64uEncode(root.publicKey),
  };
  orgs.set(n, built);
  return built;
}

/** The definition of `epochHolders` (every principal, every state of the epoch). */
function holdersByDefinition(log: VerifiedLog, scope: string, scopeId: string, epoch: number): Set<string> {
  const holders = new Set<string>();
  for (const state of log.states.filter((s) => s.epoch(scope, scopeId) === epoch)) {
    for (const principal of state.principals.keys())
      if (state.canRead(principal, scope, scopeId)) holders.add(principal);
  }
  holders.add(log.state.epochAuthors.get(`${scope} ${scopeId} ${epoch}`)!);
  return holders;
}

/** What a first sign-in of the founder computes from the logs (log-verify, wrap-verify, catch-up, credentials). */
async function signIn(data: Org, parts: number[] = []) {
  const mark = () => parts.push(performance.now());
  mark();
  const platform = await verifyPlatformLog(data.platform, data.root);
  mark();
  const log = await verifyLog(data.entries, data.founderSig, platform.state);
  mark();
  const state: OrgState = log.state;
  const offered = [
    ...state.readers(ORG, data.org).map((recipient) => ({ scope: ORG, scope_id: data.org, epoch: 1, recipient })),
    ...state.readers(WG, data.wg).map((recipient) => ({ scope: WG, scope_id: data.wg, epoch: 1, recipient })),
  ];
  const confirmed = confirmedSlots(log, data.founder, offered);
  // a wrap stored by a principal that reads nothing now: decided by the epoch's holders
  const outsider = await idOf('outsider');
  const byOutsider = offered.filter((slot) => mayWrap(log, slot, outsider)).length;
  mark();
  const holders = log.epochHolders(ORG, data.org, 1);
  mark();
  const losses = credentialLosses([log], platform, data.founder);
  mark();
  return { log, platform, offered, confirmed, byOutsider, holders, losses };
}

interface Timing {
  total: number;
  parts: string;
}

/** One run (ms), with its parts: platform log, org log, wrap checks, holders, credential losses. */
async function timed(n: number): Promise<Timing> {
  const marks: number[] = [];
  await signIn(await org(n), marks);
  const parts = marks.slice(1).map((at, index) => (at - marks[index]!).toFixed(0));
  return { total: marks.at(-1)! - marks[0]!, parts: parts.join('/') };
}

interface Work {
  rights: number;
  writes: number;
}

/** Bounds per entry of the counted work at 4 000 entries (measured with the fix: 6.7 and 3.3). */
const RIGHTS_PER_ENTRY = 10;
const WRITES_PER_ENTRY = 6;

/** The counted work of one sign-in (the logs are built before counting). */
async function counted(n: number): Promise<Work> {
  const data = await org(n);
  const canRead = vi.spyOn(OrgState.prototype, 'canRead');
  const deviceWgs = vi.spyOn(OrgState.prototype, 'deviceWgs');
  const set = vi.spyOn(Map.prototype, 'set');
  try {
    await signIn(data);
    return { rights: canRead.mock.calls.length + deviceWgs.mock.calls.length, writes: set.mock.calls.length };
  } finally {
    canRead.mockRestore();
    deviceWgs.mockRestore();
    set.mockRestore();
  }
}

describe('sign-in into a big org (KOR-35)', () => {
  it('answers like the definitions on a volume-shaped log', async () => {
    const data = await org(300);
    const result = await signIn(data);
    expect(result.log.states).toHaveLength(300);
    expect(result.platform.states).toHaveLength(150);
    expect(result.confirmed).toHaveLength(result.offered.length);
    expect(result.byOutsider).toBe(0);
    expect(result.losses.size).toBe(0);
    for (const scope of [
      [ORG, data.org],
      [WG, data.wg],
    ] as const) {
      expect([...result.log.epochHolders(scope[0], scope[1], 1)].sort()).toEqual(
        [...holdersByDefinition(result.log, scope[0], scope[1], 1)].sort(),
      );
    }
    expect(result.log.epochHolders(ORG, data.org, 1)).toBe(result.holders);
    for (const [index, state] of result.log.states.entries()) {
      const copy = new Map(state.principals);
      expect([...state.principals.keys()]).toEqual([...copy.keys()]);
      expect(state.invites.size).toBe([...state.invites.values()].length);
      if (index > 0) expect(state.seq).toBe(result.log.states[index - 1]!.seq + 1);
    }
  });

  it('grows linearly with the length of the logs', { timeout: 300_000 }, async () => {
    const small = await counted(500);
    const large = await counted(4_000);
    const time = [await timed(500), await timed(4_000)];
    const line = (n: number, work: Work, { total, parts }: Timing) =>
      `${n} entries: ${work.rights} rights questions, ${work.writes} map writes (${(work.rights / n).toFixed(1)} and ` +
      `${(work.writes / n).toFixed(1)} per entry); ${total.toFixed(0)} ms (parts ${parts}, information only)`;
    console.info(`sign-in work: ${line(500, small, time[0]!)}; ${line(4_000, large, time[1]!)}`);
    expect(large.rights / small.rights).toBeLessThan(12);
    expect(large.writes / small.writes).toBeLessThan(12);
    expect(large.rights / 4_000).toBeLessThan(RIGHTS_PER_ENTRY);
    expect(large.writes / 4_000).toBeLessThan(WRITES_PER_ENTRY);
  });
});
