// The settings section "Mailboxes" (KOR-74, ADR-099 section 9) in jsdom: the list (primary mark, address, purpose,
// release with workgroup names), add with a taken name refused, change with the write-only password, release to
// workgroups, the check on a device (valid, invalid, failed, refused, read error), delete with one confirmation, a
// record that does not open, load failures – and that no password reaches the DOM.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { bodyJson } from '../../src/core/encoding/json';
import { sealResponse } from '../../src/core/crypto/request';
import { HubError } from '../../src/core/net/signedFetch';
import { putEvents } from '../../src/core/store/slices/events';
import { setProfile } from '../../src/core/store/slices/me';
import { noteResponse } from '../../src/core/store/slices/requests';
import { MailboxesSection, section } from '../../src/ui/settings/sections/mailboxes';
import type { CredWorld } from '../core/credentials/world';
import { A, B, body, type MailHub, PASSWORD, WG2, world } from '../core/credentials/mail-hub';
import { mount } from './chat-world';

interface Sent {
  id: string;
  posted: unknown;
  bodies: { body: unknown; final: boolean }[];
  tick: number;
}

/** The device side of device requests: answers are sealed by `t.device` and read by the browser on a frame. */
function device(t: CredWorld) {
  const sent: Sent[] = [];
  const state = { refuse: null as HubError | null, readFails: false };
  t.client.on('POST /v1/device-requests', (posted) => {
    if (state.refuse !== null) throw state.refuse;
    const header = (posted as { envelope: { header: { id: string; kind: string } } }).envelope.header;
    const request: Sent = { id: header.id, posted, bodies: [], tick: 0 };
    sent.push(request);
    t.client.on(`GET /v1/device-requests/${header.id}`, async () => {
      if (state.readFails) throw new HubError('unavailable', 503);
      const responses = [];
      for (const [index, item] of request.bodies.entries()) {
        const response = {
          v: 1,
          id: `0190f000-0000-7000-8000-0000000${String(sent.indexOf(request)).padStart(2, '0')}${index}f0`,
          user: t.me.id,
          device: t.device.id,
          kind: header.kind,
          in_reply_to: request.id,
          seq: index + 1,
          final: item.final,
          ts: '2026-11-21T12:00:01.000Z',
          enc_alg: 'HPKE-X25519-A256GCM',
        };
        const envelope = await sealResponse(
          response,
          t.me.identity.kemPublic(),
          bodyJson(item.body),
          t.device.identity.sig.privateKey,
        );
        responses.push({ seq: index + 1, envelope, created_at: response.ts });
      }
      return {
        request_id: request.id,
        kind: header.kind,
        device_id: t.device.id,
        state: 'delivered',
        created_at: '2026-11-21T12:00:00.000Z',
        expires_at: '2026-11-21T12:15:00.000Z',
        responses,
      };
    });
    return { request_id: header.id, state: 'pending' };
  });
  const respond = async (index: number, answer: unknown) => {
    const request = sent[index]!;
    request.bodies.push({ body: answer, final: true });
    request.tick += 1;
    await act(async () => noteResponse(t.store, request.id, request.tick));
  };
  const frame = async (index: number) => {
    const request = sent[index]!;
    request.tick += 1;
    await act(async () => noteResponse(t.store, request.id, request.tick));
  };
  return { sent, respond, frame, state };
}

/** Verified names of the organization and its default workgroup (what the pulled profiles give). */
function names(t: CredWorld) {
  putEvents(
    t.store,
    t.org,
    [
      { kind: 'org.profile', wg: '', body: { name: 'Acme' } },
      { kind: 'wg.profile', wg: t.wg, body: { name: 'Main' } },
    ].map((item, i) => ({
      org: t.org,
      scopeId: t.org,
      scopeSeq: i + 1,
      receivedAt: null,
      event: {},
      status: 'verified' as const,
      code: null,
      header: { id: `p${i}`, kind: item.kind, ref: '', author: t.me.id, wg: item.wg },
      body: item.body,
    })),
  );
}

async function page(options: { seed?: (hub: MailHub, t: CredWorld) => Promise<void> } = {}) {
  const { t, hub } = await world();
  await options.seed?.(hub, t);
  t.client.on('GET /v1/me/devices', () => ({
    devices: [
      {
        device_id: t.device.id,
        kind: 'platform',
        org_id: t.org,
        wg_ids: [t.wg],
        online: true,
        last_seen_at: null,
        features: [],
      },
    ],
  }));
  const d = device(t);
  mount(t, [{ path: '/s', element: <MailboxesSection /> }], '/s');
  return { t, hub, d };
}

const noPassword = () => expect(document.body.innerHTML).not.toContain(PASSWORD);

function fill(values: Record<string, string>) {
  for (const [field, value] of Object.entries(values))
    fireEvent.change(screen.getByTestId(`mbform.${field}`), { target: { value } });
}

const FULL = {
  name: 'signin',
  purpose: 'sign-in mails',
  email: 'claude@example.com',
  imap_host: 'imap.example.com',
  imap_user: 'claude@example.com',
  imap_password: PASSWORD,
};

describe('MailboxesSection', () => {
  it('is the settings section "Mailboxes"', () => {
    expect(section).toMatchObject({ id: 'mailboxes', order: 62, titleKey: 'mailboxes.title' });
  });

  it('adds the first mailbox, which becomes the one of the automatic login', async () => {
    const { hub } = await page();
    expect(screen.getByTestId('mailboxes.loading')).toBeVisible();
    expect(await screen.findByTestId('mailboxes.empty')).toBeVisible();
    fireEvent.click(screen.getByTestId('mailboxes.add'));
    fireEvent.click(screen.getByTestId('mbform.cancel'));
    fireEvent.click(screen.getByTestId('mailboxes.add'));
    const save = screen.getByTestId('mbform.save');
    expect(screen.getByTestId('mbform.imap_port')).toHaveValue(993);
    expect(screen.getByLabelText('IMAP password')).toHaveAttribute('type', 'password');
    expect(save).toBeDisabled();
    fill(FULL);
    expect(save).toBeEnabled();
    fireEvent.click(save);
    const card = await screen.findByTestId(/^mbcard\.[0-9a-f-]{36}$/);
    const id = [...hub.rows.keys()][0]!;
    expect(card).toHaveAttribute('data-testid', `mbcard.${id}`);
    expect(within(card).getByTestId('mbcard.primary')).toHaveTextContent('for the automatic login');
    expect(within(card).getByTestId('mbcard.email')).toHaveTextContent('claude@example.com');
    expect(within(card).getByTestId('mbcard.purpose')).toHaveTextContent('sign-in mails');
    expect(within(card).getByTestId('mbcard.released')).toHaveTextContent('Released to no workgroup.');
    expect(screen.queryByTestId('mbform')).toBeNull();
    expect(hub.puts[0]!.body).toMatchObject({ expected_version: 0, use: { wgs: [] } });
    noPassword();
  });

  it('refuses a taken name and reports a failed save', async () => {
    const { hub } = await page({ seed: (h) => h.add(A, body({ name: 'signin' }), { primary: true }) });
    await screen.findByTestId(`mbcard.${A}`);
    fireEvent.click(screen.getByTestId('mailboxes.add'));
    fill(FULL);
    expect(screen.getByTestId('mbform.name.taken')).toBeVisible();
    expect(screen.getByTestId('mbform.save')).toBeDisabled();
    fill({ name: 'second' });
    hub.failPut = [new HubError('forbidden', 403)];
    fireEvent.click(screen.getByTestId('mbform.save'));
    expect(await screen.findByTestId('mbform.failed')).toHaveAttribute('data-code', 'forbidden');
  });

  it('changes a mailbox and keeps the stored password while the field stays empty', async () => {
    const { hub } = await page({
      seed: (h) => h.add(A, body({ name: 'signin', purpose: 'old' }), { primary: true }),
    });
    const card = await screen.findByTestId(`mbcard.${A}`);
    fireEvent.click(within(card).getByTestId('mbcard.edit'));
    expect(within(card).getByTestId('mbform.name')).toHaveValue('signin');
    expect(within(card).getByTestId('mbform.imap_password')).toHaveValue('');
    expect(within(card).getByLabelText('IMAP password (empty: keep the stored one)')).toBeVisible();
    fireEvent.click(within(card).getByTestId('mbform.cancel'));
    fireEvent.click(within(card).getByTestId('mbcard.edit'));
    fireEvent.change(within(card).getByTestId('mbform.purpose'), { target: { value: 'new purpose' } });
    fireEvent.click(within(card).getByTestId('mbform.save'));
    await waitFor(() => expect(within(card).queryByTestId('mbform')).toBeNull());
    expect(await within(card).findByText('new purpose')).toBeVisible();
    expect(await hub.opened(A)).toMatchObject({ purpose: 'new purpose', imap_password: PASSWORD });
    noPassword();
  });

  it('releases a mailbox to workgroups by their names and to none', async () => {
    const { t, hub } = await page({
      seed: async (h, w) => {
        names(w);
        await h.add(A, body({ name: 'signin', use: { wgs: [WG2] } }), { use: { wgs: [WG2] } });
      },
    });
    const card = await screen.findByTestId(`mbcard.${A}`);
    expect(within(card).getByTestId(`mbcard.released.${WG2}`)).toHaveTextContent(WG2.slice(0, 8));
    fireEvent.click(within(card).getByTestId('mbcard.release'));
    const release = within(card).getByTestId('mbrelease');
    const own = within(release).getByTestId(`mbrelease.wg.${t.wg}`);
    expect(own.closest('label')).toHaveTextContent('Main (Acme)');
    expect(own).not.toBeChecked();
    expect(within(release).getByTestId(`mbrelease.wg.${WG2}`)).toBeChecked();
    fireEvent.click(own);
    fireEvent.click(within(release).getByTestId(`mbrelease.wg.${WG2}`));
    fireEvent.click(within(release).getByTestId('mbrelease.save'));
    expect(await within(card).findByTestId('mbrelease.saved')).toBeVisible();
    expect((await hub.opened(A)).use).toEqual({ wgs: [t.wg] });
    expect(hub.puts.at(-1)!.body.use).toEqual({ wgs: [t.wg] });
    expect(await within(card).findByTestId(`mbcard.released.${t.wg}`)).toHaveTextContent('Main (Acme)');
    hub.failPut = [new HubError('forbidden', 403)];
    fireEvent.click(within(card).getByTestId('mbrelease.save'));
    expect(await within(card).findByTestId('mbrelease.failed')).toHaveAttribute('data-code', 'forbidden');
    fireEvent.click(within(card).getByTestId('mbcard.release.close'));
    expect(within(card).queryByTestId('mbrelease')).toBeNull();
  });

  it('names a workgroup by the start of its IDs before the names are read, and says so when there is none', async () => {
    const { t } = await page({ seed: (h) => h.add(A, body({ name: 'signin' })) });
    const card = await screen.findByTestId(`mbcard.${A}`);
    fireEvent.click(within(card).getByTestId('mbcard.release'));
    const own = within(card).getByTestId(`mbrelease.wg.${t.wg}`);
    expect(own.closest('label')).toHaveTextContent(`${t.wg.slice(0, 8)} (${t.org.slice(0, 8)})`);
    act(() => setProfile(t.store, { orgs: [] }));
    expect(within(card).getByTestId('mbrelease.none')).toBeVisible();
  });

  it('checks a mailbox on a device with its body', async () => {
    const { d } = await page({ seed: (h) => h.add(A, body({ name: 'signin' })) });
    const card = await screen.findByTestId(`mbcard.${A}`);
    fireEvent.click(within(card).getByTestId('mbcard.check'));
    fireEvent.click(await within(card).findByTestId('mbcheck.start'));
    expect(await within(card).findByTestId('mbcheck.waiting')).toHaveAttribute('data-read-error', '');
    expect(d.sent[0]!.posted).toMatchObject({ envelope: { header: { kind: 'mailbox.check' } } });
    d.state.readFails = true;
    await d.frame(0);
    await waitFor(() =>
      expect(within(card).getByTestId('mbcheck.waiting')).toHaveAttribute('data-read-error', 'unavailable'),
    );
    d.state.readFails = false;
    await d.respond(0, { step: 'done', valid: true });
    expect(await within(card).findByTestId('mbcheck.result')).toHaveTextContent('The device signed in to the mailbox.');
    fireEvent.click(within(card).getByTestId('mbcheck.close'));
    fireEvent.click(within(card).getByTestId('mbcard.check'));
    fireEvent.click(await within(card).findByTestId('mbcheck.start'));
    await within(card).findByTestId('mbcheck.waiting');
    await d.respond(1, { step: 'done', valid: false });
    expect(await within(card).findByTestId('mbcheck.result')).toHaveAttribute('data-valid', 'false');
    fireEvent.click(within(card).getByTestId('mbcheck.close'));
    fireEvent.click(within(card).getByTestId('mbcard.check'));
    fireEvent.click(await within(card).findByTestId('mbcheck.start'));
    await within(card).findByTestId('mbcheck.waiting');
    await d.respond(2, { step: 'failed', code: 'upstream_unreachable' });
    expect(await within(card).findByTestId('mbcheck.failed')).toHaveAttribute('data-code', 'upstream_unreachable');
    fireEvent.click(within(card).getByTestId('mbcheck.close'));
    d.state.refuse = new HubError('rate_limited', 429);
    fireEvent.click(within(card).getByTestId('mbcard.check'));
    fireEvent.click(await within(card).findByTestId('mbcheck.start'));
    expect(await within(card).findByTestId('mbcheck.failed')).toHaveAttribute('data-code', 'rate_limited');
    expect(within(card).queryByTestId('mbcheck.starting')).toBeNull();
    noPassword();
  });

  it('deletes after one confirmation and reports a failure', async () => {
    const { hub } = await page({ seed: (h) => h.add(A, body({ name: 'signin' })) });
    const card = await screen.findByTestId(`mbcard.${A}`);
    fireEvent.click(within(card).getByTestId('mbcard.remove'));
    expect(within(card).getByTestId('mbcard.confirm')).toHaveTextContent('Delete this mailbox?');
    fireEvent.click(within(card).getByTestId('mbcard.remove.no'));
    fireEvent.click(within(card).getByTestId('mbcard.remove'));
    hub.failDelete = new HubError('forbidden', 403);
    fireEvent.click(within(card).getByTestId('mbcard.remove.yes'));
    expect(await within(card).findByTestId('mbcard.failed')).toHaveAttribute('data-code', 'forbidden');
    hub.failDelete = null;
    fireEvent.click(within(card).getByTestId('mbcard.remove.yes'));
    expect(await screen.findByTestId('mailboxes.empty')).toBeVisible();
    expect(hub.rows.size).toBe(0);
  });

  it('shows a record that does not open with its code and only lets it be deleted', async () => {
    await page({
      seed: async (h) => {
        await h.add(A, body(), { primary: true });
        await h.add(B, body({ extra: 1 }));
      },
    });
    const primary = await screen.findByTestId(`mbcard.${A}`);
    expect(primary).toHaveTextContent('Mailbox without a name');
    expect(within(primary).queryByTestId('mbcard.purpose')).toBeNull();
    const broken = screen.getByTestId(`mbcard.${B}`);
    expect(within(broken).getByTestId('mbcard.unverifiable')).toHaveAttribute('data-code', 'params_invalid');
    expect(within(broken).queryByTestId('mbcard.edit')).toBeNull();
    expect(within(broken).queryByTestId('mbcard.check')).toBeNull();
    expect(within(broken).getByTestId('mbcard.remove')).toBeVisible();
  });

  it('reports a failed load and loads again', async () => {
    const { t } = await world();
    t.client.on('GET /v1/me/mailboxes', () => {
      throw new HubError('unavailable', 503);
    });
    mount(t, [{ path: '/s', element: <MailboxesSection /> }], '/s', 'de');
    expect(await screen.findByTestId('mailboxes.failed')).toHaveAttribute('data-code', 'unavailable');
    t.client.on('GET /v1/me/mailboxes', () => ({ mailboxes: [] }));
    fireEvent.click(screen.getByTestId('mailboxes.retry'));
    expect(await screen.findByTestId('mailboxes.empty')).toHaveTextContent('Noch kein Postfach.');
  });
});
