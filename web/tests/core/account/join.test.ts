// U-WEB-invite-org (core part, 04-crypto.md sections 5.1-5.2a, 6.1): opening the package of an org invitation and
// verifying the lookup log against the package's founder and head – including manipulated hub answers (wrong founder,
// dropped or foreign `invite` entry, forged signature, wrong served head, revoked or consumed invitations, a package of
// another org) – the deferred verification of logs with platform devices, the `accept` batch (both signatures, W1
// self-wraps, `member.profile`) checked by the simulated hub's own batch rules, and the retries after `seq_conflict`.
import { describe, expect, it } from 'vitest';
import {
  ACCEPT_ATTEMPTS,
  completeInvitation,
  joinOrg,
  loadPlatform,
  needsPlatform,
  openInvitation,
  type OrgInvitation,
  prepareAccept,
  refreshInvitation,
  sendAccept,
} from '../../../src/core/account/join';
import type { Lookup } from '../../../src/core/account/register';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { uuid7 } from '../../../src/core/encoding/ids';
import { formatTime } from '../../../src/core/encoding/time';
import { openEvent } from '../../../src/core/crypto/event';
import { formatCode, inviteSecrets, openPackage, sealPackage } from '../../../src/core/crypto/invite';
import { ed25519VerifyB64u } from '../../../src/core/crypto/keys';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import { buildEntries, type Entry, entryHash, logText } from '../../../src/core/log/entries';
import { epochKey, ORG, WG } from '../../../src/core/log/state';
import { verifyLog } from '../../../src/core/log/verify';
import { HubError } from '../../../src/core/net/signedFetch';
import { createInvite } from '../../../src/core/org/invites';
import { type Actor, actor, EXPIRES, idOf, T0, TS, World } from '../log/world';
import { orgWorld, type OrgWorld } from '../org/world';

const HUB_KEY = { id: 'hub-test', public_key: b64uEncode(new Uint8Array(32).fill(9)) };
const CODE_BYTES = new Uint8Array(16).fill(3);
const CODE = formatCode(CODE_BYTES);
const now = () => T0;

interface Invited {
  readonly w: OrgWorld;
  readonly one: string;
  readonly erin: Actor;
  readonly inviteId: string;
  /** The lookup answer for the hub's current log (`patch` changes the raw answer). */
  lookup(patch?: (raw: Record<string, unknown>) => void): Lookup;
  /** Erin's signed requests (`POST`/`GET /v1/orgs/{org}/log`, `GET /v1/platform/log`). */
  request(method: string, target: string, options?: { body: unknown }): Promise<unknown>;
}

async function invited(options: { setup?: Parameters<typeof orgWorld>[0]; message?: string } = {}): Promise<Invited> {
  const one = await idOf('wg one');
  const w = await orgWorld({
    ...options.setup,
    setup: async (world, a) => {
      await world.batch(a.ada!, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await options.setup?.setup?.(world, a);
    },
  });
  const created = await createInvite(w.context, w.org, {
    request: {
      kind: 'user',
      role: 'member',
      wgs: { [one]: 'read' },
      hours: 72,
      ...(options.message === undefined ? {} : { message: options.message }),
    },
    directory: {
      orgName: 'Grüße AG',
      workgroups: new Map([
        [w.world.defaultWg, 'Default'],
        [one, 'One'],
      ]),
      members: new Map(),
    },
    hubKey: HUB_KEY,
    origin: 'https://hub.example',
    code: () => CODE_BYTES,
  });
  const erin = await actor('erin');
  w.world.add(erin);
  const lookup = (patch?: (raw: Record<string, unknown>) => void): Lookup => {
    const log = w.world.log!;
    const raw: Record<string, unknown> = {
      kind: 'user',
      scope: 'org',
      org_id: w.org,
      expires_at: created.expires,
      registered: false,
      package_ct: w.hub.invites.get(created.inviteId)!.package_ct,
      log_head: { ...log.head },
      log: log.entries.map((entry) => structuredClone(entry)),
    };
    patch?.(raw);
    return {
      inviteId: created.inviteId,
      kind: 'user',
      scope: 'org',
      expiresAt: created.expires,
      registered: false,
      raw,
    };
  };
  const request = async (method: string, target: string, opts?: { body: unknown }) => {
    if (method === 'POST' && target === `/v1/orgs/${w.org}/log`) return w.hub.append(erin.id, opts!.body as never);
    if (method === 'GET' && target.startsWith(`/v1/orgs/${w.org}/log`)) {
      const log = w.world.log!;
      return { entries: log.entries.map((entry, seq) => ({ seq, entry })), head: log.head, next_after: null };
    }
    return w.client.request(method, target, opts);
  };
  return { w, one, erin, inviteId: created.inviteId, lookup, request };
}

const joiner = (erin: Actor) => ({ userId: erin.id, identity: erin.identity });

describe('openInvitation', () => {
  it("carries the inviter's personal message of the package (E-28)", async () => {
    const { w, lookup } = await invited({ message: 'Willkommen <b>Erin</b>' });
    const invitation = await openInvitation(CODE, lookup(), w.hub.platform!.state);
    expect(invitation.message).toBe('Willkommen <b>Erin</b>');
  });

  it('opens the package and verifies the lookup log', async () => {
    const { w, one, lookup, inviteId } = await invited();
    const invitation = await openInvitation(CODE, lookup(), w.hub.platform!.state);
    expect(invitation).toMatchObject({
      inviteId,
      org: w.org,
      orgName: 'Grüße AG',
      message: null,
      defaultWg: w.world.defaultWg,
      role: 'member',
      expires: formatTime(T0 + 72 * 3600_000),
      founder: w.world.state.founder,
      packageHead: w.world.log!.head,
      head: w.world.log!.head,
    });
    expect(invitation.log!.head).toEqual(w.world.log!.head);
    expect(invitation.workgroups).toEqual([
      { id: w.world.defaultWg, name: 'Default', access: 'write', isDefault: true },
      { id: one, name: 'One', access: 'read', isDefault: false },
    ]);
    expect(invitation.keys.map((key) => [key.scope, key.scope_id, key.epoch])).toEqual(
      expect.arrayContaining([
        [ORG, w.org, 1],
        [WG, w.world.defaultWg, 1],
        [WG, one, 1],
      ]),
    );
    expect(invitation.keys).toHaveLength(3);
    // the package keys are the org's keys
    const held = w.world.ring(w.me.id);
    for (const key of invitation.keys) {
      expect(key.key.reveal(b64uEncode)).toBe(held(key.scope, key.scope_id, key.epoch)!.reveal(b64uEncode));
    }
  });

  it('accepts a longer log that extends the package head and orders unnamed workgroups', async () => {
    const { w, lookup } = await invited();
    const two = await idOf('wg two');
    await w.world.batch(w.me, [{ type: 'wg_create', wg: two, epoch: 1 }]);
    const invitation = await openInvitation(CODE, lookup(), null);
    expect(invitation.log!.head.seq).toBe(invitation.packageHead.seq + 1);
  });

  it('lists written, unnamed and equally named workgroups in a stable order', async () => {
    const [two, three] = [await idOf('wg two'), await idOf('wg three')];
    const w = await orgWorld({
      setup: async (world, a) => {
        await world.batch(a.ada!, [{ type: 'wg_create', wg: two, epoch: 1 }]);
        await world.batch(a.ada!, [{ type: 'wg_create', wg: three, epoch: 1 }]);
      },
    });
    const created = await createInvite(w.context, w.org, {
      request: { kind: 'user', role: 'admin', wgs: { [three]: 'read', [two]: 'write' }, hours: 1 },
      directory: { orgName: '', workgroups: new Map(), members: new Map() },
      hubKey: HUB_KEY,
      origin: 'https://hub.example',
      code: () => CODE_BYTES,
    });
    const log = w.world.log!;
    const raw = {
      package_ct: w.hub.invites.get(created.inviteId)!.package_ct,
      log_head: log.head,
      log: log.entries,
    };
    const invitation = await openInvitation(
      CODE,
      { inviteId: created.inviteId, kind: 'user', scope: 'org', expiresAt: '', registered: false, raw },
      null,
    );
    expect(invitation.role).toBe('admin');
    expect(invitation.orgName).toBe('');
    const sorted = [two, three].sort();
    expect(invitation.workgroups.map((wg) => [wg.id, wg.name, wg.access])).toEqual([
      [w.world.defaultWg, '', 'write'],
      ...sorted.map((id) => [id, '', id === two ? 'write' : 'read']),
    ]);
  });

  it('refuses a code that does not open the package, a missing package and another code', async () => {
    const { lookup } = await invited();
    const other = formatCode(new Uint8Array(16).fill(4));
    await expect(openInvitation(other, lookup(), null)).rejects.toMatchObject({ code: 'invite_code_invalid' });
    const secrets = await inviteSecrets(new Uint8Array(16).fill(4));
    // the hub serves another invitation's package for this invite ID
    await expect(
      openInvitation(CODE, { ...lookup(), raw: { ...lookup().raw, package_ct: 'A'.repeat(80) } }, null),
    ).rejects.toMatchObject({ code: 'invite_code_invalid' });
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => delete raw.package_ct),
        null,
      ),
    ).rejects.toMatchObject({ code: 'invite_code_invalid' });
    expect(secrets.inviteId).not.toBe(lookup().inviteId);
  });

  it('refuses a log under another founder (the hub presents a log of its own)', async () => {
    const { lookup } = await invited();
    const mallory = await actor('mallory');
    const fake = await World.create(mallory);
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => ((raw.log = fake.log!.entries), (raw.log_head = fake.log!.head))),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_rollback' });
    // even one that is long enough to reach the package head: genesis is not by the package's founder
    for (let n = 0; n < 4; n += 1)
      await fake.batch(mallory, [{ type: 'wg_create', wg: await idOf(`m${n}`), epoch: 1 }]);
    const { packageHead } = await openInvitation(CODE, lookup(), null);
    const entries = fake.log!.entries.slice(0, packageHead.seq + 1);
    // replace the entry at the package head by the real one: the chain breaks, and genesis is mallory's
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => {
          raw.log = [...entries.slice(0, -1), (raw.log as unknown[])[packageHead.seq]];
          raw.log_head = packageHead;
        }),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_genesis' });
  });

  it('refuses a log without the invite entry and a forged signature', async () => {
    const { lookup } = await invited();
    // the hub drops the invite entry: the package head is not in the log
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => (raw.log = (raw.log as unknown[]).slice(0, -1))),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_rollback' });
    // a forged signature on an entry before the package head
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => {
          const log = raw.log as Entry[];
          const first = log[1]!;
          log[1] = { ...first, sig: b64uEncode(new Uint8Array(64).fill(1)) };
        }),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_signature' });
  });

  it('refuses entries after the head with a forged signature and a wrong served head', async () => {
    const { w, lookup } = await invited();
    await w.world.batch(w.me, [{ type: 'wg_create', wg: await idOf('wg late'), epoch: 1 }]);
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => {
          const log = raw.log as Entry[];
          const last = log[log.length - 1]!;
          log[log.length - 1] = { ...last, sig: b64uEncode(new Uint8Array(64).fill(1)) };
        }),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_signature' });
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => (raw.log_head = { seq: 0, hash: 'x' })),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_chain' });
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => delete raw.log_head),
        null,
      ),
    ).rejects.toMatchObject({
      code: 'log_chain',
    });
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => (raw.log = [])),
        null,
      ),
    ).rejects.toMatchObject({
      code: 'log_empty',
    });
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => delete raw.log),
        null,
      ),
    ).rejects.toMatchObject({
      code: 'log_empty',
    });
  });

  it('refuses a package whose head has no invite entry of this code (invite_not_in_log)', async () => {
    const { w, lookup, inviteId } = await invited();
    const secrets = await inviteSecrets(CODE_BYTES);
    const log = w.world.log!;
    const pkg = {
      org: w.org,
      org_name: 'X',
      wg_names: {},
      founder: log.state.founder,
      head: log.states.length > 2 ? { seq: 1, hash: log.states[1]!.head } : log.head,
      keys: [{ scope: ORG, scope_id: w.org, epoch: 1, key: b64uEncode(new Uint8Array(32)) }],
      hub_key: HUB_KEY,
      v: 1,
    };
    // the log without the invite entry, ending at the package head
    const shortened = lookup((raw) => {
      raw.package_ct = '';
      raw.log = (raw.log as unknown[]).slice(0, 2);
      raw.log_head = pkg.head;
    });
    const sealed = await sealPackage(secrets, pkg);
    await expect(
      openInvitation(CODE, { ...shortened, raw: { ...shortened.raw, package_ct: sealed } }, null),
    ).rejects.toMatchObject({ code: 'invite_not_in_log' });
    expect(inviteId).toBe(secrets.inviteId);
  });

  it('refuses an invite entry with another pk_inv, a device invitation and a package of another org', async () => {
    const { w, lookup } = await invited();
    const secrets = await inviteSecrets(CODE_BYTES);
    const other = await inviteSecrets(new Uint8Array(16).fill(5));
    const world = w.world;
    const pkgFor = (head: { seq: number; hash: string }, org = w.org) => ({
      org,
      org_name: 'X',
      wg_names: {},
      founder: world.state.founder,
      head,
      keys: [{ scope: ORG, scope_id: w.org, epoch: 1, key: b64uEncode(new Uint8Array(32)) }],
      hub_key: HUB_KEY,
      v: 1,
    });
    // the log before the real invite entry, extended by an entry of the test
    const before = world.log!.entries.slice(0, -1);
    const withEntry = async (fields: Parameters<typeof buildEntries>[2][number]) => {
      const base = await verifyLog(before, world.state.founder);
      const [entry] = await buildEntries(
        { org: w.org, head: base.head },
        { id: w.me.id, key: w.me.identity.sig.privateKey },
        [fields],
        TS,
      );
      const log = [...before, entry];
      return { log, head: (await verifyLog(log, world.state.founder)).head };
    };
    const forged = await withEntry({
      type: 'invite',
      invite_id: secrets.inviteId,
      pk_inv: other.pkInv,
      kind: 'user',
      role: 'member',
      wgs: {},
      expires: EXPIRES,
    });
    const answer = async (built: { log: unknown[]; head: { seq: number; hash: string } }, org?: string) => {
      const base = lookup();
      return {
        ...base,
        raw: {
          ...base.raw,
          log: built.log,
          log_head: built.head,
          package_ct: await sealPackage(secrets, pkgFor(built.head, org)),
        },
      };
    };
    await expect(openInvitation(CODE, await answer(forged), null)).rejects.toMatchObject({ code: 'invite_not_in_log' });
    const device = await withEntry({
      type: 'invite',
      invite_id: secrets.inviteId,
      pk_inv: secrets.pkInv,
      kind: 'device',
      wgs: { [world.defaultWg]: 'device' },
      expires: EXPIRES,
    });
    await expect(openInvitation(CODE, await answer(device), null)).rejects.toMatchObject({ code: 'invite_not_in_log' });
    // a package without workgroup names: the names stay empty
    const valid = await withEntry({
      type: 'invite',
      invite_id: secrets.inviteId,
      pk_inv: secrets.pkInv,
      kind: 'user',
      role: 'member',
      wgs: {},
      expires: EXPIRES,
    });
    expect((await openInvitation(CODE, await answer(valid), null)).workgroups).toEqual([
      { id: world.defaultWg, name: '', access: 'write', isDefault: true },
    ]);
    const otherOrg = await idOf('other org');
    await expect(openInvitation(CODE, await answer(forged, otherOrg), null)).rejects.toMatchObject({
      code: 'log_genesis',
    });
  });

  it('refuses a revoked and a consumed invitation', async () => {
    const revoked = await invited();
    await revoked.w.hub.foreign(revoked.w.me, { type: 'invite_revoke', invite_id: revoked.inviteId });
    await expect(openInvitation(CODE, revoked.lookup(), null)).rejects.toMatchObject({ code: 'invite_revoked' });
    const consumed = await invited();
    const invitation = await openInvitation(CODE, consumed.lookup(), null);
    await sendAccept(
      consumed.request,
      await prepareAccept({ invitation, joiner: joiner(consumed.erin), displayName: 'Erin', platform: null, now }),
    );
    await expect(openInvitation(CODE, consumed.lookup(), null)).rejects.toMatchObject({ code: 'invite_consumed' });
  });
});

describe('logs with platform devices', () => {
  const setup = {
    platformDevices: ['pdev'],
    setup: async (world: World, a: Record<string, Actor>) => {
      await world.batch(a.ada!, [{ type: 'device_assign', device: a.pdev!.id, wg: world.defaultWg, platform_seq: 2 }]);
    },
  };

  it('are verified after the sign-in, with the platform log', async () => {
    const { w, lookup, erin, request } = await invited({ setup });
    expect(needsPlatform(lookup().raw.log as unknown[])).toBe(true);
    expect(needsPlatform([null, { body: null }, { body: { type: 'device_assign' } }])).toBe(false);
    expect(needsPlatform([{ body: { type: 'grant' } }, { body: { type: 'admit' } }])).toBe(true);
    const deferred = await openInvitation(CODE, lookup(), null);
    expect(deferred.log).toBeNull();
    expect(deferred.role).toBe('member');
    expect(deferred.defaultWg).toBe(w.world.defaultWg);
    await expect(
      prepareAccept({ invitation: deferred, joiner: joiner(erin), displayName: 'E', platform: null }),
    ).rejects.toMatchObject({ code: 'log_empty' });
    await expect(completeInvitation(deferred, CODE, null)).rejects.toMatchObject({ code: 'log_rights' });
    const platform = await loadPlatform(request, w.platform.root);
    const complete = await completeInvitation(deferred, CODE, platform);
    expect(complete.log!.head).toEqual(w.world.log!.head);
    expect(await completeInvitation(complete, CODE, platform)).toBe(complete);
    // with a known platform log the check runs at once
    expect((await openInvitation(CODE, lookup(), platform)).log).not.toBeNull();
  });

  /** A lookup with a package of this code whose fields are patched, for the given log and served head. */
  async function crafted(
    lookup: Invited['lookup'],
    patch: Record<string, unknown>,
    log: unknown[],
    head: { seq: number; hash: string },
  ): Promise<Lookup> {
    const secrets = await inviteSecrets(CODE_BYTES);
    const base = lookup();
    const pkg = { ...(await openPackage(secrets, base.raw.package_ct as string)), ...patch };
    return { ...base, raw: { ...base.raw, package_ct: await sealPackage(secrets, pkg), log, log_head: head } };
  }

  it('check the hash chain, the package head and genesis before the sign-in', async () => {
    const { w, lookup, inviteId } = await invited({ setup });
    const log = lookup().raw.log as Entry[];
    const last = log[log.length - 1]!;
    // the hub drops the invite entry: the package head is missing
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => (raw.log = log.slice(0, -1))),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_rollback' });
    // an entry the hub injects after the head: wrong link, wrong seq, wrong served head
    const injected = (body: Record<string, unknown>) => ({ ...last, body: { ...last.body, ...body } });
    const head = { seq: log.length, hash: 'x' };
    await expect(
      openInvitation(
        CODE,
        lookup(
          (raw) => ((raw.log = [...log, injected({ seq: log.length, prev: 'A'.repeat(43) })]), (raw.log_head = head)),
        ),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_chain' });
    const linked = await entryHash(last);
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => ((raw.log = [...log, injected({ seq: 0, prev: linked })]), (raw.log_head = head))),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_chain' });
    // a well-linked injected entry passes the anonymous check, but not the verification after the sign-in
    const fake = injected({ seq: log.length, prev: linked, invite_id: b64uEncode(new Uint8Array(16)) });
    const forged = [...log, fake];
    await expect(
      openInvitation(
        CODE,
        lookup((raw) => ((raw.log = forged), (raw.log_head = head))),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_chain' });
    const forgedHead = { seq: log.length, hash: await entryHash(fake) };
    const deferred = await openInvitation(
      CODE,
      lookup((raw) => ((raw.log = forged), (raw.log_head = forgedHead))),
      null,
    );
    expect(deferred.log).toBeNull();
    await expect(completeInvitation(deferred, CODE, w.hub.platform!.state)).rejects.toMatchObject({
      code: 'log_signature',
    });
    // genesis must name the package's founder and org
    const packageHead = deferred.packageHead;
    await expect(
      openInvitation(
        CODE,
        await crafted(lookup, { founder: b64uEncode(new Uint8Array(32).fill(7)) }, log, packageHead),
        null,
      ),
    ).rejects.toMatchObject({ code: 'log_genesis' });
    await expect(
      openInvitation(CODE, await crafted(lookup, { org: await idOf('other org') }, log, packageHead), null),
    ).rejects.toMatchObject({ code: 'log_genesis' });
    // the invite entry must lie at or before the package head
    const before = log.length - 2;
    const early = { seq: before, hash: await entryHash(log[before]) };
    await expect(
      openInvitation(CODE, await crafted(lookup, { head: early }, log, packageHead), null),
    ).rejects.toMatchObject({
      code: 'invite_not_in_log',
    });
    // a first entry that is not genesis
    const lone = { ...log[1]!, body: { ...log[1]!.body, seq: 0, prev: '' } };
    const loneHead = { seq: 0, hash: await entryHash(lone) };
    await expect(
      openInvitation(CODE, await crafted(lookup, { head: loneHead }, [lone], loneHead), null),
    ).rejects.toMatchObject({
      code: 'log_genesis',
    });
    // the revocation after the package head shows in the verification after the sign-in
    await w.hub.foreign(w.me, { type: 'invite_revoke', invite_id: inviteId });
    const revoked = await openInvitation(CODE, lookup(), null);
    await expect(completeInvitation(revoked, CODE, w.hub.platform!.state)).rejects.toMatchObject({
      code: 'invite_revoked',
    });
  });
});

describe('loadPlatform', () => {
  it('reads every page and verifies against the root key; an empty platform log is null', async () => {
    const pages = [
      { entries: [], head: null, next_after: 0 },
      { entries: [], head: null, next_after: null },
    ];
    const calls: string[] = [];
    const request = async (_method: string, target: string) => {
      calls.push(target);
      return pages.shift();
    };
    expect(await loadPlatform(request, 'x')).toBeNull();
    expect(calls).toEqual(['/v1/platform/log', '/v1/platform/log?after=0']);
    await expect(loadPlatform(async () => ({ entries: 'no' }), 'x')).rejects.toMatchObject({ code: 'header_invalid' });
    await expect(loadPlatform(async () => null, 'x')).rejects.toMatchObject({ code: 'header_invalid' });
  });
});

describe('prepareAccept and joinOrg', () => {
  it('builds accept with sig and inv_sig, self-wraps of the package keys and member.profile', async () => {
    const { w, erin, lookup, request, one } = await invited();
    const invitation = await openInvitation(CODE, lookup(), w.hub.platform!.state);
    const prepared = await prepareAccept({
      invitation,
      joiner: joiner(erin),
      displayName: '  Erin Ü  ',
      platform: w.hub.platform!.state,
      now,
    });
    const body = prepared.body as {
      entries: Entry[];
      wraps: { recipient_id: string; scope: string; scope_id: string }[];
      events: unknown[];
    };
    expect(body.entries).toHaveLength(1);
    const [entry] = body.entries;
    expect(entry!.body).toMatchObject({
      type: 'accept',
      invite_id: invitation.inviteId,
      subject: erin.id,
      kind: 'user',
    });
    const secrets = await inviteSecrets(CODE_BYTES);
    await ed25519VerifyB64u(b64uDecode(secrets.pkInv), entry!.inv_sig, logText(entry!.body));
    await ed25519VerifyB64u(erin.identity.sigPublic(), entry!.sig, logText(entry!.body));
    expect(body.wraps.map((wrap) => [wrap.recipient_id, wrap.scope, wrap.scope_id])).toEqual(
      expect.arrayContaining([
        [erin.id, ORG, w.org],
        [erin.id, WG, w.world.defaultWg],
        [erin.id, WG, one],
      ]),
    );
    expect(body.wraps).toHaveLength(3);
    for (const wrap of body.wraps as never[]) {
      const {
        scope,
        scope_id,
        epoch,
        recipient_id,
        wrap: sealed,
      } = wrap as { scope: string; scope_id: string; epoch: number; recipient_id: string; wrap: string; sig: string };
      const key = await unwrapKey(erin.identity.kem, { scope, scope_id, epoch, recipient: recipient_id }, sealed);
      expect(key.reveal(b64uEncode)).toBe(prepared.keys.get(epochKey(scope, scope_id, epoch))!.reveal(b64uEncode));
    }
    expect(prepared.profile).toBe(true);
    const orgKey = prepared.keys.get(epochKey(ORG, w.org, 1))!;
    const opened = await openEvent(orgKey, body.events[0], erin.identity.sigPublic());
    expect(JSON.parse(fromUtf8(opened))).toEqual({ display_name: 'Erin Ü' });
    await sendAccept(request, prepared);
    // the hub applied its own rules (L1-L8, W3, E-checks are the hub's; here the batch rules)
    expect(w.world.state.member(erin.id)).not.toBeNull();
    expect(w.world.log!.head).toEqual(prepared.log.head);
    expect(w.hub.events).toHaveLength(1);
  });

  it('refuses an invalid display name and a joiner who is already a member', async () => {
    const { w, erin, lookup } = await invited();
    const invitation = await openInvitation(CODE, lookup(), null);
    await expect(
      prepareAccept({ invitation, joiner: joiner(erin), displayName: ' ', platform: null }),
    ).rejects.toMatchObject({
      code: 'header_invalid',
    });
    await expect(
      prepareAccept({
        invitation,
        joiner: { userId: w.me.id, identity: w.me.identity },
        displayName: 'Ada',
        platform: null,
      }),
    ).rejects.toMatchObject({ code: 'log_accept' });
  });

  it('sends no profile when the package lacks the current org key (rotated after the invitation)', async () => {
    const { w, erin, lookup, request } = await invited();
    await w.world.batch(w.me, [{ type: 'rotate', scope: 'org', scope_id: w.org, epoch: 2 }]);
    const invitation = await openInvitation(CODE, lookup(), null);
    const prepared = await prepareAccept({
      invitation,
      joiner: joiner(erin),
      displayName: 'Erin',
      platform: null,
      now,
    });
    expect(prepared.profile).toBe(false);
    expect((prepared.body as { events: unknown[] }).events).toEqual([]);
    // the stale org key still belongs to the user (it reads the org scope), the wrap of the current epoch is missing
    expect(prepared.keys.has(epochKey(ORG, w.org, 1))).toBe(true);
    await sendAccept(request, prepared);
    expect(w.world.state.member(erin.id)).not.toBeNull();
  });

  it('checks the answer head', async () => {
    const { erin, lookup } = await invited();
    const invitation = await openInvitation(CODE, lookup(), null);
    const prepared = await prepareAccept({
      invitation,
      joiner: joiner(erin),
      displayName: 'Erin',
      platform: null,
      now,
    });
    await expect(sendAccept(async () => ({ head: { seq: 0, hash: 'x' } }), prepared)).rejects.toMatchObject({
      code: 'unavailable',
    });
  });

  it('rebuilds on a new lookup after seq_conflict', async () => {
    const { w, erin, lookup, request } = await invited();
    const invitation = await openInvitation(CODE, lookup(), null);
    w.hub.before.push(() => w.hub.foreign(w.me, { type: 'wg_create', wg: uuid7(), epoch: 1 }));
    const anonCalls: unknown[] = [];
    const anon = async (_method: string, _target: string, body?: unknown) => {
      anonCalls.push(body);
      return lookup().raw;
    };
    const joined = await joinOrg(
      { anon, request, isMember: async () => false },
      { invitation, joiner: joiner(erin), displayName: 'Erin', platform: null, code: CODE, now },
    );
    expect(anonCalls).toEqual([{ invite_id: invitation.inviteId }]);
    expect(joined.invitation.log!.head.seq).toBe(invitation.log!.head.seq + 1);
    expect(w.world.log!.head).toEqual(joined.prepared.log.head);
  });

  it(`gives up after ${ACCEPT_ATTEMPTS} conflicts and rethrows other failures`, async () => {
    const { w, erin, lookup, request } = await invited();
    const invitation = await openInvitation(CODE, lookup(), null);
    for (let n = 0; n < ACCEPT_ATTEMPTS; n += 1) {
      w.hub.before.push(() => w.hub.foreign(w.me, { type: 'wg_create', wg: uuid7(), epoch: 1 }));
    }
    const deps = { anon: async () => lookup().raw, request, isMember: async () => false };
    const input = { invitation, joiner: joiner(erin), displayName: 'Erin', platform: null, code: CODE, now };
    await expect(joinOrg(deps, input)).rejects.toMatchObject({ code: 'seq_conflict' });
    const failing = { ...deps, request: async () => Promise.reject(new HubError('invite_registered', 409)) };
    await expect(joinOrg(failing, input)).rejects.toMatchObject({ code: 'invite_registered' });
    const broken = { ...deps, request: async () => Promise.reject(new Error('boom')) };
    await expect(joinOrg(broken, input)).rejects.toThrow('boom');
  });

  it('recognizes an accept whose answer got lost by the membership and the member log', async () => {
    const { w, erin, lookup, request } = await invited();
    const invitation = await openInvitation(CODE, lookup(), null);
    const input = { invitation, joiner: joiner(erin), displayName: 'Erin', platform: null, code: CODE, now };
    let lose = true;
    const lossy = async (method: string, target: string, options?: { body: unknown }) => {
      const answer = await request(method, target, options);
      if (method === 'POST' && lose) {
        lose = false;
        throw new HubError('unavailable', 0);
      }
      return answer;
    };
    // the membership check fails as well (the hub is still restarting): the failure stands
    const down = { anon: async () => lookup().raw, request: lossy, isMember: () => Promise.reject(new Error('down')) };
    await expect(joinOrg(down, input)).rejects.toMatchObject({ code: 'unavailable' });
    // the stored accept counts once the membership and the member log show it (KOR-36: any failure, not only
    // seq_conflict); a retry with a new batch (other time) is refused with seq_conflict and the stored one counts too
    const deps = { ...down, isMember: async () => true };
    expect((await joinOrg(deps, { ...input, now: () => T0 + 1000 })).prepared.log.head).toEqual(w.world.log!.head);
  });

  it('refuses a member log without the accept or with a wrong head', async () => {
    const { w, erin, lookup, request } = await invited();
    const invitation = await openInvitation(CODE, lookup(), null);
    const input = { invitation, joiner: joiner(erin), displayName: 'Erin', platform: null, code: CODE, now };
    w.hub.before.push(async () => undefined);
    const deps = { anon: async () => lookup().raw, request, isMember: async () => true };
    await expect(joinOrg(deps, input)).rejects.toMatchObject({ code: 'log_accept' });
    w.hub.before.push(async () => undefined);
    // the whole log over two pages, but without a served head
    const paged = async (method: string, target: string, options?: { body: unknown }) => {
      if (method !== 'GET') return request(method, target, options);
      const items = w.world.log!.entries.map((entry, seq) => ({ seq, entry }));
      return target.endsWith('?after=0')
        ? { entries: items.slice(1), head: null, next_after: null }
        : { entries: items.slice(0, 1), head: null, next_after: 0 };
    };
    await expect(joinOrg({ ...deps, request: paged }, input)).rejects.toMatchObject({ code: 'log_chain' });
    w.hub.before.push(async () => undefined);
    const malformed = async (method: string, target: string, options?: { body: unknown }) =>
      method === 'GET' ? { entries: [7] } : request(method, target, options);
    await expect(joinOrg({ ...deps, request: malformed }, input)).rejects.toMatchObject({ code: 'log_rollback' });
  });
});

describe('refreshInvitation', () => {
  it('needs a log that extends the verified one and a well-formed answer', async () => {
    const { lookup } = await invited();
    const invitation: OrgInvitation = await openInvitation(CODE, lookup(), null);
    const shorter = lookup((raw) => {
      raw.log = (raw.log as unknown[]).slice(0, -1);
    }).raw;
    await expect(refreshInvitation(async () => shorter, invitation, CODE, null)).rejects.toMatchObject({
      code: 'log_rollback',
    });
    await expect(refreshInvitation(async () => 'x', invitation, CODE, null)).rejects.toMatchObject({
      code: 'header_invalid',
    });
    const deferred = { ...invitation, log: null };
    expect((await refreshInvitation(async () => lookup().raw, deferred, CODE, null)).log).not.toBeNull();
  });
});
