// U-WEB-credentials (KOR-74, ADR-099): mailboxes in the browser – the bodies with name, purpose and release, the list
// with each record verified and opened (never the password), adding and changing one sealed under the newest own CK
// with the mirror of its release, the release to workgroups, the check on a device with the mailbox's body, removal,
// and the workgroups a mailbox can be released to.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openRequest } from '../../../src/core/crypto/request';
import type { Sealed } from '../../../src/core/crypto/signed';
import {
  addMailbox,
  changeMailbox,
  EMPTY_FORM,
  fetchMailbox,
  formBody,
  formOf,
  formReady,
  isMailboxName,
  loadMailboxes,
  nameTaken,
  openMailbox,
  releasableWorkgroups,
  releaseMailbox,
  removeMailbox,
  startMailboxCheck,
  updateMailbox,
} from '../../../src/core/credentials/mailboxes';
import { isMailboxBody, validBody } from '../../../src/core/credentials/records';
import { isMailboxUse, validRequestBody } from '../../../src/core/requests/bodies';
import { HubError } from '../../../src/core/net/signedFetch';
import { actor } from '../log/world';
import { A, B, body, C, D, form, item, PASSWORD, WG, WG2, world } from './mail-hub';
import { MAILBOX } from './world';

describe('bodies', () => {
  it('accepts a mailbox with name, purpose and release, also where the primary mailbox is read', () => {
    const full = body({ name: 'sign-in.2_x', purpose: 'p'.repeat(500), use: { wgs: [WG, WG2] } });
    expect(isMailboxBody(full)).toBe(true);
    expect(validBody('mailbox', full)).toBe(true);
    expect(isMailboxBody(MAILBOX)).toBe(true);
    expect(isMailboxBody(body({ use: { wgs: [WG, WG] } }))).toBe(true); // the contract allows a repeated one
    for (const bad of [
      { name: '' },
      { name: 'Upper' },
      { name: '-start' },
      { name: 'x'.repeat(65) },
      { purpose: '' },
      { purpose: 'p'.repeat(501) },
      { use: { wgs: ['x'] } },
      { use: { wgs: [WG], extra: 1 } },
      { use: [] },
      { imap_port: 0 },
      { email: 'no' },
    ])
      expect(isMailboxBody(body(bad))).toBe(false);
    expect(
      isMailboxUse({
        wgs: Array.from({ length: 51 }, (_, i) => `0190f000-0000-7000-8000-0000000001${String(i).padStart(2, '0')}`),
      }),
    ).toBe(false);
    expect(isMailboxUse({ wgs: 'x' })).toBe(false);
    expect(isMailboxUse(null)).toBe(false);
  });

  it('allows the check of a given mailbox', () => {
    expect(validRequestBody('mailbox.check', {}, false)).toBe(true);
    expect(validRequestBody('mailbox.check', { mailbox: body({ name: 'a' }) }, false)).toBe(true);
    expect(validRequestBody('mailbox.check', { mailbox: { imap_host: 'x' } }, false)).toBe(false);
    expect(validRequestBody('mailbox.check', { other: 1 }, false)).toBe(false);
    expect(validRequestBody('mailbox.check', { mailbox: MAILBOX }, true)).toBe(false);
  });

  it('builds a body of the form and checks names', () => {
    expect(isMailboxName('a.b-c_1')).toBe(true);
    expect(isMailboxName('A')).toBe(false);
    expect(formBody(form({ name: ' signin ', email: ' claude@example.com ' }))).toEqual({
      ...MAILBOX,
      name: 'signin',
      purpose: 'sign-in mails',
    });
    expect(formBody(form({ purpose: '  ' }))).toEqual({ ...MAILBOX, name: 'signin' });
    expect(formBody(form({ imap_port: 'x' }))).toBeNull();
    expect(formBody(form({ name: '' }))).toBeNull();
    expect(formBody(form({ imap_password: '' }))).toBeNull();
    expect(formBody(form({ imap_password: '' }), 'kept')).toMatchObject({ imap_password: 'kept' });
    expect(formOf(item(A))).toEqual({ ...EMPTY_FORM });
    expect(formOf(item(A, { name: 'n', purpose: 'p', email: 'e', host: 'h', port: 143, user: 'u' }))).toEqual({
      name: 'n',
      purpose: 'p',
      email: 'e',
      imap_host: 'h',
      imap_port: '143',
      imap_user: 'u',
      imap_password: '',
    });
    const items = [item(A, { name: 'signin' }), item(B, { name: 'other' })];
    expect(nameTaken(items, ' signin ')).toBe(true);
    expect(nameTaken(items, 'signin', A)).toBe(false);
    expect(formReady(form(), items, null)).toBe(false);
    expect(formReady(form({ imap_password: '' }), items, A)).toBe(true);
    expect(formReady(form({ imap_password: '' }), [], null)).toBe(false);
  });
});

describe('loadMailboxes', () => {
  it('opens every record, never returns the password and puts the primary one first', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'zeta', purpose: 'reviews', use: { wgs: [t.wg] } }), { use: { wgs: [t.wg] } });
    await hub.add(B, body(), { primary: true });
    await hub.add(C, body({ name: 'alpha' }));
    await hub.add(D, body({ name: 'alpha' })); // two of a name (another client): both listed, by ID
    const found = await loadMailboxes(t.context);
    expect(found.map((entry) => [entry.id, entry.primary, entry.name])).toEqual([
      [B, true, null],
      [C, false, 'alpha'],
      [D, false, 'alpha'],
      [A, false, 'zeta'],
    ]);
    expect(found[3]).toEqual({
      id: A,
      primary: false,
      status: 'verified',
      code: null,
      name: 'zeta',
      purpose: 'reviews',
      email: MAILBOX.email,
      host: MAILBOX.imap_host,
      port: 993,
      user: MAILBOX.imap_user,
      wgs: [t.wg],
    });
    expect(found[0]!.wgs).toEqual([]);
    expect(JSON.stringify(found)).not.toContain(PASSWORD);
  });

  it('keeps records that do not open as unverifiable, skips gone ones and lists nothing without keys', async () => {
    const { t, hub } = await world();
    expect(await loadMailboxes(t.context)).toEqual([]);
    expect(t.client.of('GET /v1/wraps')).toHaveLength(0);
    await hub.add(A, body(), { owner: '0190f000-0000-7000-8000-0000000000ee' });
    await hub.add(B, body({ extra: 1 }));
    await hub.add(C, body(), { author: await actor('stranger') });
    await hub.add(D, body(), { epoch: 9 });
    const tampered = '0190f000-0000-7000-8000-0000000000a5';
    await hub.add(tampered, body());
    const record = hub.rows.get(tampered)!.record as { ct: string };
    record.ct = (record.ct.startsWith('A') ? 'B' : 'A') + record.ct.slice(1);
    const named = '0190f000-0000-7000-8000-0000000000a6';
    await hub.add(named, body({ name: 'ok' }));
    const gone = '0190f000-0000-7000-8000-0000000000a7';
    const list = t.client.routes.get('GET /v1/me/mailboxes')!;
    t.client.on('GET /v1/me/mailboxes', (sent, target) => {
      const answer = list(sent, target) as { mailboxes: unknown[] };
      return { mailboxes: [...answer.mailboxes, { id: gone, primary: false, version: 1, use: null }] };
    });
    const found = await loadMailboxes(t.context);
    expect(found[0]!.id).toBe(named);
    expect(Object.fromEntries(found.map((entry) => [entry.id, entry.code]))).toEqual({
      [named]: null,
      [A]: 'header_invalid',
      [B]: 'params_invalid',
      [C]: 'author_not_writer',
      [D]: 'epoch_stale',
      [tampered]: 'unverifiable',
    });
    expect(found.find((entry) => entry.id === A)).toMatchObject({ status: 'unverifiable', email: null, wgs: [] });
  });

  it('refuses a malformed list and an unknown user, and passes other errors of a read on', async () => {
    const { t, hub } = await world();
    for (const answer of [null, { mailboxes: 'x' }, { mailboxes: [{ id: A, primary: false, version: 1, use: 1 }] }]) {
      t.client.on('GET /v1/me/mailboxes', () => answer);
      await expect(loadMailboxes(t.context)).rejects.toMatchObject({ code: 'unavailable' });
    }
    t.client.on('GET /v1/me/mailboxes', () => ({ mailboxes: [] }));
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(loadMailboxes(t.context)).rejects.toMatchObject({ code: 'unauthorized' });
    hub.failGet = new HubError('forbidden', 403);
    await expect(fetchMailbox(t.context, A)).rejects.toMatchObject({ code: 'forbidden' });
  });

  it('refuses an answer that is no record', async () => {
    const { t } = await world();
    await expect(openMailbox(t.context, A, null, [])).rejects.toMatchObject({ code: 'header_invalid' });
    const answer = { owner_id: t.me.id, record: 'x' };
    await expect(openMailbox(t.context, A, answer, [])).rejects.toMatchObject({ code: 'header_invalid' });
    t.store.update('me', (me) => ({ ...me, identity: null }));
    await expect(openMailbox(t.context, A, answer, [])).rejects.toMatchObject({ code: 'unauthorized' });
  });
});

describe('add and change', () => {
  it('adds a mailbox sealed under the newest CK with the mirror of its release', async () => {
    const { t, hub } = await world();
    const id = await addMailbox(t.context, form(), [t.wg, t.wg], []);
    expect(hub.puts[0]!.body).toMatchObject({ expected_version: 0, use: { wgs: [t.wg] } });
    expect((hub.puts[0]!.body.record as Sealed).header).toMatchObject({ id, kind: 'mailbox', version: 1, epoch: 1 });
    expect(await hub.opened(id)).toEqual({
      ...MAILBOX,
      name: 'signin',
      purpose: 'sign-in mails',
      use: { wgs: [t.wg] },
    });
    expect(hub.rows.get(id)!.primary).toBe(true);
    const second = await addMailbox(t.context, form({ name: 'second', purpose: '' }), [], []);
    expect(hub.puts[1]!.body.use).toEqual({ wgs: [] });
    expect(await hub.opened(second)).toEqual({ ...MAILBOX, name: 'second' });
    expect(hub.rows.get(second)!.primary).toBe(false);
  });

  it('refuses a bad form, a taken name and a bad release, and retries one concurrent write', async () => {
    const { t, hub } = await world();
    await expect(addMailbox(t.context, form({ imap_host: '' }), [], [])).rejects.toMatchObject({
      code: 'invalid_argument',
    });
    const taken = [item(A, { name: 'signin' })];
    await expect(addMailbox(t.context, form(), [], taken)).rejects.toMatchObject({ code: 'invalid_argument' });
    await expect(addMailbox(t.context, form(), ['x'], [])).rejects.toMatchObject({ code: 'invalid_argument' });
    expect(hub.puts).toHaveLength(0);
    hub.failPut = [new HubError('epoch_stale', 409)];
    const id = await addMailbox(t.context, form(), [], []);
    expect(hub.rows.has(id)).toBe(true);
    hub.failPut = [new HubError('epoch_stale', 409), new HubError('version_conflict', 409)];
    await expect(addMailbox(t.context, form({ name: 'b' }), [], [])).rejects.toMatchObject({
      code: 'version_conflict',
    });
    hub.failPut = [new HubError('forbidden', 403)];
    await expect(addMailbox(t.context, form({ name: 'c' }), [], [])).rejects.toMatchObject({ code: 'forbidden' });
    t.hub.wraps.length = 0;
    await expect(addMailbox(t.context, form({ name: 'd' }), [], [])).rejects.toMatchObject({ code: 'epoch_stale' });
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(addMailbox(t.context, form({ name: 'e' }), [], [])).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('changes a mailbox (the primary one too), keeps the stored password and the release', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'old', purpose: 'old purpose', use: { wgs: [t.wg] } }), {
      primary: true,
      use: { wgs: [t.wg] },
    });
    const items = [item(A, { name: 'old' }), item(B, { name: 'other' })];
    await updateMailbox(t.context, A, form({ name: 'new', purpose: '', imap_password: '' }), items);
    expect(await hub.opened(A)).toEqual({ ...MAILBOX, name: 'new', use: { wgs: [t.wg] } });
    expect(hub.puts.at(-1)!.body).toMatchObject({ expected_version: 1, use: { wgs: [t.wg] } });
    expect((hub.puts.at(-1)!.body.record as Sealed).header).toMatchObject({ version: 2, author: t.me.id });
    await updateMailbox(t.context, A, form({ name: 'new', imap_password: 'another password' }), items);
    expect(await hub.opened(A)).toMatchObject({ imap_password: 'another password', purpose: 'sign-in mails' });
    expect(() => updateMailbox(t.context, A, form({ name: 'other' }), items)).toThrow();
    expect(() => updateMailbox(t.context, A, form({ imap_port: '0' }), items)).toThrow();
  });

  it('releases a mailbox to workgroups and to none', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'm' }));
    await releaseMailbox(t.context, A, [WG2, t.wg, WG2]);
    expect((await hub.opened(A)).use).toEqual({ wgs: [WG2, t.wg].sort() });
    expect(hub.puts.at(-1)!.body.use).toEqual({ wgs: [WG2, t.wg].sort() });
    await releaseMailbox(t.context, A, []);
    expect('use' in (await hub.opened(A))).toBe(false);
    expect(hub.puts.at(-1)!.body.use).toEqual({ wgs: [] });
    expect(() => releaseMailbox(t.context, A, ['x'])).toThrow();
  });

  it('retries one concurrent change and refuses gone records and an unknown user', async () => {
    const { t, hub } = await world();
    await hub.add(A, body());
    hub.failPut = [new HubError('version_conflict', 409)];
    await changeMailbox(t.context, A, (opened) => {
      opened.name = 'again';
    });
    expect((await hub.opened(A)).name).toBe('again');
    hub.failPut = [new HubError('version_conflict', 409), new HubError('epoch_stale', 409)];
    await expect(changeMailbox(t.context, A, () => undefined)).rejects.toMatchObject({ code: 'version_conflict' });
    await expect(changeMailbox(t.context, C, () => undefined)).rejects.toMatchObject({ code: 'not_found' });
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(changeMailbox(t.context, A, () => undefined)).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('removes a mailbox, a gone one too', async () => {
    const { t, hub } = await world();
    await hub.add(A, body());
    await removeMailbox(t.context, A);
    await removeMailbox(t.context, A);
    expect(hub.rows.size).toBe(0);
    hub.failDelete = new HubError('forbidden', 403);
    await expect(removeMailbox(t.context, A)).rejects.toMatchObject({ code: 'forbidden' });
  });
});

describe('the check on a device', () => {
  it('sends the opened body of the mailbox sealed to the device', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'm' }));
    t.client.on('POST /v1/device-requests', (sent) => ({
      request_id: (sent as { envelope: { header: { id: string } } }).envelope.header.id,
      state: 'pending',
    }));
    const prepared = await startMailboxCheck(t.context, t.device.id, A);
    expect(prepared.kind).toBe('mailbox.check');
    const envelope = (t.client.of('POST /v1/device-requests')[0]!.body as { envelope: never }).envelope;
    const plain = await openRequest(t.device.identity.kem, envelope, b64uDecode(t.me.identity.public().sig));
    expect(JSON.parse(fromUtf8(plain))).toEqual({ mailbox: body({ name: 'm' }) });
    expect(JSON.stringify(envelope)).not.toContain(PASSWORD);
    await expect(startMailboxCheck(t.context, t.device.id, B)).rejects.toMatchObject({ code: 'not_found' });
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(startMailboxCheck(t.context, t.device.id, A)).rejects.toMatchObject({ code: 'unauthorized' });
  });
});

describe('releasableWorkgroups', () => {
  it('lists the active workgroups with explicit access of the organizations with a verified log', async () => {
    const { t } = await world();
    const orgs = t.store.getState().orgs;
    const unknown = '0190f000-0000-7000-8000-0000000000f9';
    expect(releasableWorkgroups(orgs, [t.org, unknown, t.org], t.me.id)).toEqual([{ org: t.org, wg: t.wg }]);
    expect(releasableWorkgroups(orgs, [t.org], unknown)).toEqual([]);
    expect(releasableWorkgroups(orgs, [t.org], null)).toEqual([]);
  });
});
