// U-WEB-ui-members (core part), ADR-060: the direct admission of an existing account – every identity answer of the hub
// checked against the verified platform log (shape, user, anchor at receipt time, typed code), the lookup by code or
// user, the admin's invitees, the `admit` batch through `submitBatch` against the simulated hub, its refusals, and the
// user's own account code.
import { describe, expect, it } from 'vitest';
import { accountCode, formatAccountCode } from '../../../src/core/crypto/identity';
import type { PlatformState } from '../../../src/core/log/platform';
import { ORG, WG } from '../../../src/core/log/state';
import { admitAccount, findAccount, loadInvitees, ownAccountCode, proveIdentity } from '../../../src/core/org/admit';
import type { Role } from '../../../src/core/org/members';
import type { SubmitProgress } from '../../../src/core/org/batch';
import { setIdentity } from '../../../src/core/store/slices/me';
import { admitWorld } from './admit-world';

const refused = (code: string) => expect.objectContaining({ code });

describe('proveIdentity', () => {
  it('proves an anchored answer and recomputes the account code', async () => {
    const { w, answer } = await admitWorld();
    const { bob } = w.others;
    const platform = w.hub.platform!.state;
    const proven = await proveIdentity(answer(bob), platform);
    expect(proven).toEqual({
      userId: bob.id,
      pub: bob.identity.public(),
      platformSeq: 2,
      idSig: '',
      accountCode: answer(bob).account_code,
    });
    expect(await proveIdentity(answer(bob), platform, { user: bob.id, code: proven.accountCode })).toEqual(proven);
  });

  it('refuses malformed answers', async () => {
    const { w, answer } = await admitWorld();
    const platform = w.hub.platform!.state;
    const good = answer(w.others.bob);
    const bad: unknown[] = [
      null,
      'text',
      { ...good, pub: null },
      { ...good, pub: 'keys' },
      { ...good, user_id: 7 },
      { ...good, pub: { ...good.pub, kem: 1 } },
      { ...good, pub: { ...good.pub, sig: null } },
      { ...good, platform_seq: -1 },
      { ...good, platform_seq: '2' },
      { ...good, id_sig: null },
    ];
    for (const item of bad) await expect(proveIdentity(item, platform)).rejects.toEqual(refused('identity_unproven'));
  });

  it('refuses another user, an unanchored identity and another code', async () => {
    const { w, answer } = await admitWorld();
    const { bob, cy } = w.others;
    const platform = w.hub.platform!.state;
    await expect(proveIdentity(answer(bob), platform, { user: cy.id })).rejects.toEqual(refused('identity_unproven'));
    // the anchor at seq 3 is cy's
    await expect(proveIdentity({ ...answer(bob), platform_seq: 3 }, platform)).rejects.toEqual(
      refused('identity_unproven'),
    );
    await expect(proveIdentity(answer(bob), platform, { code: answer(cy).account_code })).rejects.toEqual(
      refused('identity_unproven'),
    );
  });

  it('turns crypto errors into identity_unproven and passes other errors on', async () => {
    const { w, answer } = await admitWorld();
    const good = answer(w.others.bob);
    const anchoring = { identityAnchored: async () => true } as unknown as PlatformState;
    // keys of the wrong length: the account code cannot be computed
    await expect(proveIdentity({ ...good, pub: { kem: 'AA', sig: 'AA' } }, anchoring)).rejects.toEqual(
      refused('identity_unproven'),
    );
    const broken = {
      identityAnchored: async () => {
        throw new TypeError('defect');
      },
    } as unknown as PlatformState;
    await expect(proveIdentity(good, broken)).rejects.toThrow(TypeError);
  });
});

describe('findAccount', () => {
  it('finds an account by user and by a typed code, normalized', async () => {
    const { w, answer } = await admitWorld();
    const { bob } = w.others;
    const platform = w.hub.platform!.state;
    const byUser = await findAccount(w.client, platform, { user: bob.id });
    expect(byUser.userId).toBe(bob.id);
    const code = answer(bob).account_code;
    const typed = formatAccountCode(code).toLowerCase().replace('-', ' ');
    const byCode = await findAccount(w.client, platform, { code: typed });
    expect(byCode).toEqual(byUser);
    expect(w.client.of('GET /v1/users/lookup').map((call) => call.target)).toEqual([`/v1/users/lookup?code=${code}`]);
  });

  it('refuses a malformed code without asking the hub, and an answer for another code', async () => {
    const { w, answer } = await admitWorld();
    const { bob, cy } = w.others;
    const platform = w.hub.platform!.state;
    await expect(findAccount(w.client, platform, { code: 'not a code' })).rejects.toEqual(
      refused('account_code_invalid'),
    );
    expect(w.client.of('GET /v1/users/lookup')).toHaveLength(0);
    // a hub that answers with cy for bob's code
    w.client.on('GET /v1/users/lookup', () => answer(cy));
    await expect(findAccount(w.client, platform, { code: answer(bob).account_code })).rejects.toEqual(
      refused('identity_unproven'),
    );
    // a hub that answers with another user than asked for
    w.client.on(`GET /v1/users/${bob.id}/identity`, () => answer(cy));
    await expect(findAccount(w.client, platform, { user: bob.id })).rejects.toEqual(refused('identity_unproven'));
  });
});

describe('loadInvitees', () => {
  it('lists the invitees with their proven identity, null when unproven, and skips malformed rows', async () => {
    const { w, answer, invitees } = await admitWorld();
    const { bob, cy, dev2, pdev } = w.others;
    invitees.push(
      { user_id: bob.id, invite_id: 'i1', registered_at: 'r1', identity: answer(bob) },
      { user_id: dev2.id, invite_id: 'i2', registered_at: 'r2', identity: null },
      // an identity that does not belong to the row's user
      { user_id: pdev.id, invite_id: 'i3', registered_at: 'r3', identity: answer(cy) },
      { user_id: cy.id, invite_id: 'i4', registered_at: 'r4', identity: { ...answer(cy), platform_seq: 2 } },
      null,
      { user_id: 5, registered_at: 'r5' },
      { user_id: bob.id, registered_at: 6 },
    );
    const rows = await loadInvitees(w.context);
    expect(rows.map((row) => [row.userId, row.registeredAt, row.identity?.accountCode ?? null])).toEqual([
      [bob.id, 'r1', answer(bob).account_code],
      [dev2.id, 'r2', null],
      [pdev.id, 'r3', null],
      [cy.id, 'r4', null],
    ]);
  });

  it('reads odd answers as no invitees and needs the verified platform log', async () => {
    const { w } = await admitWorld();
    w.client.on('GET /v1/me/invitees', () => null);
    expect(await loadInvitees(w.context)).toEqual([]);
    w.client.on('GET /v1/me/invitees', () => ({ invitees: 'none' }));
    expect(await loadInvitees(w.context)).toEqual([]);
    w.store.update('orgs', (orgs) => ({ ...orgs, platform: null }));
    await expect(loadInvitees(w.context)).rejects.toEqual(refused('identity_unproven'));
  });
});

describe('admitAccount', () => {
  it('appends admit with both anchors and wraps the org, default and listed workgroup keys for the new member', async () => {
    const { w, one, answer } = await admitWorld();
    const { ada, bob } = w.others;
    const subject = await findAccount(w.client, w.hub.platform!.state, { user: bob.id });
    const steps: SubmitProgress[] = [];
    const { built, attempts } = await admitAccount(
      w.context,
      w.org,
      subject,
      'member',
      { [one]: 'read', [w.world.defaultWg]: 'read' },
      (step) => steps.push(step),
    );
    expect(attempts).toBe(1);
    expect(built!.body.entries.map((entry) => entry.body)).toEqual([
      expect.objectContaining({
        type: 'admit',
        by: ada.id,
        subject: bob.id,
        pub: bob.identity.public(),
        platform_seq: 2,
        id_sig: '',
        by_platform_seq: answer(ada).platform_seq,
        by_id_sig: '',
        role: 'member',
        wgs: { [one]: 'read' },
      }),
    ]);
    const forBob = built!.body.wraps.filter((wrap) => wrap.recipient_id === bob.id);
    expect(forBob.map((wrap) => [wrap.scope, wrap.scope_id]).sort()).toEqual(
      [
        [ORG, w.org],
        [WG, w.world.defaultWg],
        [WG, one],
      ].sort(),
    );
    expect(w.world.state.member(bob.id)).toMatchObject({ role: 'member', consented: false });
    expect(w.world.state.wgAccess(bob.id, one)).toBe('read');
    expect(steps).toEqual([]); // no seq_conflict: no retry report
  });

  it('admits an admin, and does nothing for an account that is a member already', async () => {
    const { w } = await admitWorld();
    const { bob, cy } = w.others;
    const platform = w.hub.platform!.state;
    const member = await findAccount(w.client, platform, { user: cy.id });
    expect((await admitAccount(w.context, w.org, member, 'member', {})).built).toBeNull();
    const subject = await findAccount(w.client, platform, { user: bob.id });
    await admitAccount(w.context, w.org, subject, 'admin', {});
    expect(w.world.state.isAdmin(bob.id)).toBe(true);
  });

  it('refuses a bad role, a non-admin, an archived workgroup', async () => {
    const { w, old } = await admitWorld();
    const { bob } = w.others;
    const subject = await findAccount(w.client, w.hub.platform!.state, { user: bob.id });
    await expect(admitAccount(w.context, w.org, subject, 'owner' as Role, {})).rejects.toEqual(refused('forbidden'));
    await expect(admitAccount(w.context, w.org, subject, 'member', { [old]: 'read' })).rejects.toEqual(
      refused('forbidden'),
    );
    const member = await admitWorld({ as: 'cy' });
    await expect(admitAccount(member.w.context, member.w.org, subject, 'member', {})).rejects.toEqual(
      refused('forbidden'),
    );
    expect(w.world.state.member(bob.id)).toBeNull();
  });

  it('needs the platform log, the unlocked identity and a hub anchor of exactly that identity', async () => {
    const { w } = await admitWorld();
    const { bob } = w.others;
    const subject = await findAccount(w.client, w.hub.platform!.state, { user: bob.id });
    // the hub's anchor of ada names other keys than the unlocked ones
    setIdentity(w.store, bob.identity);
    await expect(admitAccount(w.context, w.org, subject, 'member', {})).rejects.toEqual(refused('identity_unproven'));
    setIdentity(w.store, null);
    await expect(admitAccount(w.context, w.org, subject, 'member', {})).rejects.toEqual(refused('unauthorized'));
    w.store.update('me', (me) => ({ ...me, userId: null, identity: w.me.identity }));
    await expect(admitAccount(w.context, w.org, subject, 'member', {})).rejects.toEqual(refused('unauthorized'));
    w.store.update('orgs', (orgs) => ({ ...orgs, platform: null }));
    await expect(admitAccount(w.context, w.org, subject, 'member', {})).rejects.toEqual(refused('identity_unproven'));
  });
});

describe('ownAccountCode', () => {
  it('computes the code from the unlocked identity, null without one', async () => {
    const { w } = await admitWorld();
    const { ada } = w.others;
    expect(await ownAccountCode(w.context)).toBe(await accountCode(ada.id, ada.identity.public()));
    setIdentity(w.store, null);
    expect(await ownAccountCode(w.context)).toBeNull();
    w.store.update('me', (me) => ({ ...me, userId: null, identity: ada.identity }));
    expect(await ownAccountCode(w.context)).toBeNull();
  });
});
