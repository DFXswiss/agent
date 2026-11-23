// Settings sections "Model access", "Credentials" and "Quotas", the usage strip and the credential-rotation toast in
// jsdom (06-web.md sections 5.2, 8.2, 11 job 5): every state of the forms, the device-request flows with sealed device
// responses (Claude login relay and automation, credential and mailbox checks), the list with delete, and that no
// token, key or password reaches the DOM.
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyJson } from '../../src/core/encoding/json';
import { sealResponse } from '../../src/core/crypto/request';
import type { PlatformConfig } from '../../src/core/config/check';
import { HubError } from '../../src/core/net/signedFetch';
import { setConfig } from '../../src/core/store/slices/config';
import { noteCredentialRotation } from '../../src/core/store/slices/credentials';
import { setProfile } from '../../src/core/store/slices/me';
import { setQuotas } from '../../src/core/store/slices/quotas';
import { noteResponse } from '../../src/core/store/slices/requests';
import { putTasks } from '../../src/core/store/slices/tasks';
import { putUsage, type SessionUsage } from '../../src/core/store/slices/usage';
import { section as credentialsSection, CredentialsSection } from '../../src/ui/settings/sections/credentials';
import { section as modelSection, ModelSection } from '../../src/ui/settings/sections/model';
import { section as quotasSection, QuotasSection } from '../../src/ui/settings/sections/quotas';
import { CredentialRotationToasts, headerItem, TOAST_MS } from '../../src/ui/shell/header/credentials';
import { UsageSlot } from '../../src/ui/task-tabs/chat/UsageSlot';
import { UsageStrip } from '../../src/ui/task-tabs/chat/usage-strip';
import { taskRow } from '../core/tasks/world';
import {
  API_KEY,
  credWorld,
  type CredWorld,
  GITHUB,
  LOGIN_TOKEN,
  MAILBOX,
  seed,
  SUBSCRIPTION,
} from '../core/credentials/world';
import { mount } from './chat-world';

afterEach(() => vi.useRealTimers());

interface Sent {
  id: string;
  kind: string;
  follows: string;
  bodies: { body: unknown; final: boolean }[];
  state: string;
  /** Grows with every change: the `device_response` hint the watcher reacts to. */
  tick: number;
}

/** The device double: every posted request gets a route that lists its sealed responses. */
function device(t: CredWorld) {
  const sent: Sent[] = [];
  let refuse: HubError | null = null;
  t.client.on('POST /v1/device-requests', (body) => {
    if (refuse !== null) throw refuse;
    const header = (body as { envelope: { header: { id: string; kind: string; follows: string } } }).envelope.header;
    const request: Sent = {
      id: header.id,
      kind: header.kind,
      follows: header.follows,
      bodies: [],
      state: 'delivered',
      tick: 0,
    };
    sent.push(request);
    t.client.on(`GET /v1/device-requests/${header.id}`, async () => {
      if (request.state === 'unreadable') throw new HubError('unavailable', 503);
      const responses = [];
      for (const [index, item] of request.bodies.entries()) {
        const response = {
          v: 1,
          id: `0190f000-0000-7000-8000-0000000${String(sent.indexOf(request)).padStart(2, '0')}${index}f0`,
          user: t.me.id,
          device: t.device.id,
          kind: request.kind,
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
        kind: request.kind,
        device_id: t.device.id,
        state: request.state,
        created_at: '2026-11-21T12:00:00.000Z',
        expires_at: '2026-11-21T12:15:00.000Z',
        responses,
      };
    });
    return { request_id: header.id, state: 'pending' };
  });
  const respond = async (index: number, body: unknown, final = false) => {
    const request = sent[index]!;
    request.bodies.push({ body, final });
    request.tick += 1;
    await act(async () => noteResponse(t.store, request.id, request.tick));
  };
  const setState = async (index: number, state: string) => {
    const request = sent[index]!;
    request.state = state;
    request.tick += 1;
    await act(async () => noteResponse(t.store, request.id, request.tick));
  };
  return {
    sent,
    respond,
    setState,
    refuse: (error: HubError | null) => {
      refuse = error;
    },
  };
}

async function world(): Promise<CredWorld> {
  const t = await credWorld();
  t.client.on('GET /v1/me/devices', () => ({
    devices: [{ device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null }],
  }));
  return t;
}

const credentialsPage = (t: CredWorld) =>
  mount(t, [{ path: '/settings', element: <CredentialsSection /> }], '/settings');

/** No secret of the fixtures anywhere in the document. */
function noSecrets() {
  const html = document.body.innerHTML;
  for (const secret of [API_KEY.key, MAILBOX.imap_password, SUBSCRIPTION.token, GITHUB.token]) {
    expect(html).not.toContain(secret);
  }
}

describe('registration', () => {
  it('registers the three sections in order', () => {
    expect(modelSection).toMatchObject({ id: 'model', order: 60, titleKey: 'model.title' });
    expect(credentialsSection).toMatchObject({ id: 'credentials', order: 62, titleKey: 'credentials.title' });
    expect(quotasSection).toMatchObject({ id: 'quotas', order: 64, titleKey: 'model.quotas.title' });
    expect(headerItem).toMatchObject({ id: 'credentials', order: 91 });
  });
});

describe('credentials list', () => {
  it('reports a failed load and loads again', async () => {
    const t = await world();
    let fail = true;
    t.client.on('GET /v1/me/credentials', () => {
      if (fail) throw new HubError('unavailable', 503);
      return t.hub.list();
    });
    credentialsPage(t);
    expect(screen.getByTestId('credentials.loading')).toBeVisible();
    expect(await screen.findByTestId('credentials.load-failed')).toHaveAttribute('data-code', 'unavailable');
    fail = false;
    fireEvent.click(screen.getByTestId('credentials.reload'));
    expect(await screen.findByTestId('credentials.empty')).toHaveTextContent('No credentials are stored.');
  });

  it('lists every kind with its summary, version and codes, never a secret', async () => {
    const t = await world();
    await seed(t, 'github', GITHUB, { author: t.device });
    await seed(t, 'anthropic_api_key', API_KEY, { version: 3 });
    await seed(t, 'claude_subscription', { ...SUBSCRIPTION, email: null }, { author: t.device });
    await seed(t, 'mailbox', MAILBOX);
    credentialsPage(t);
    expect(await screen.findByTestId('credentials.item.github.detail')).toHaveTextContent('Account octo-cat');
    expect(screen.getByTestId('credentials.item.anthropic_api_key.version')).toHaveTextContent(/^Version 3, changed /);
    expect(screen.queryByTestId('credentials.item.anthropic_api_key.detail')).toBeNull();
    expect(screen.queryByTestId('credentials.item.claude_subscription.detail')).toBeNull();
    expect(screen.getByTestId('credentials.item.mailbox.detail')).toHaveTextContent(
      'claude@example.com on imap.example.com',
    );
    expect(screen.getByTestId('api-key.input')).toHaveAttribute('type', 'password');
    expect(screen.getByTestId('claude.connect')).toHaveTextContent('Connect again');
    expect(screen.getByTestId('mailbox.save')).toHaveTextContent('Replace mailbox');
    noSecrets();
  });

  it('shows the origin and expiry of a subscription token, an expired one highlighted (ADR-056 section 7)', async () => {
    const t = await world();
    await seed(t, 'claude_subscription', { ...LOGIN_TOKEN, expires_at: '2000-01-01T00:00:00.000Z' });
    credentialsPage(t);
    const origin = await screen.findByTestId('credentials.item.claude_subscription.origin');
    expect(origin).toHaveTextContent('From the Claude Code login');
    const expiry = screen.getByTestId('credentials.item.claude_subscription.expiry');
    expect(expiry).toHaveAttribute('data-expired', 'true');
    expect(expiry).toHaveTextContent(/^Expired /);
    noSecrets();
  });

  it('shows a valid login token with its expiry', async () => {
    const t = await world();
    await seed(t, 'claude_subscription', { ...LOGIN_TOKEN, expires_at: '2999-01-01T00:00:00.000Z' });
    credentialsPage(t);
    const expiry = await screen.findByTestId('credentials.item.claude_subscription.expiry');
    expect(expiry).toHaveAttribute('data-expired', 'false');
    expect(expiry).toHaveTextContent(/^Valid until /);
  });

  it('shows a long-lived token without expiry', async () => {
    const t = await world();
    await seed(t, 'claude_subscription', SUBSCRIPTION, { author: t.device });
    credentialsPage(t);
    expect(await screen.findByTestId('credentials.item.claude_subscription.origin')).toHaveTextContent(
      'Long-lived token',
    );
    expect(screen.queryByTestId('credentials.item.claude_subscription.expiry')).toBeNull();
  });

  it('shows the e-mail of a subscription and unverifiable records with their code', async () => {
    const t = await world();
    await seed(t, 'claude_subscription', SUBSCRIPTION, { author: t.device });
    t.hub.records.set('mailbox', { ...t.hub.records.get('claude_subscription')!, record: 'junk' as never });
    credentialsPage(t);
    expect(await screen.findByTestId('credentials.item.claude_subscription.detail')).toHaveTextContent(
      'Account claude@example.com',
    );
    expect(screen.getByTestId('credentials.item.mailbox')).toHaveAttribute('data-status', 'unverifiable');
    expect(screen.getByTestId('credentials.item.mailbox.code')).toHaveTextContent('header_invalid');
    expect(screen.getByTestId('mailbox.save')).toHaveTextContent('Save mailbox');
  });

  it('deletes after confirmation and reports a failure in the dialog', async () => {
    const t = await world();
    await seed(t, 'anthropic_api_key', API_KEY);
    let fail = true;
    t.client.on('DELETE /v1/me/credentials/anthropic_api_key', () => {
      if (fail) throw new HubError('forbidden', 403);
      t.hub.records.delete('anthropic_api_key');
      return {};
    });
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('credentials.item.anthropic_api_key.delete'));
    expect(screen.getByTestId('credentials.delete.dialog')).toHaveTextContent('Delete Anthropic API key?');
    fireEvent.click(screen.getByTestId('credentials.delete.cancel'));
    expect(screen.queryByTestId('credentials.delete.dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('credentials.item.anthropic_api_key.delete'));
    fireEvent.keyDown(screen.getByTestId('credentials.delete.dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('credentials.delete.dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('credentials.item.anthropic_api_key.delete'));
    fireEvent.click(screen.getByTestId('credentials.delete.confirm'));
    expect(await screen.findByTestId('credentials.delete.failed')).toHaveAttribute('data-code', 'forbidden');
    fail = false;
    fireEvent.click(screen.getByTestId('credentials.delete.confirm'));
    expect(await screen.findByTestId('credentials.empty')).toBeVisible();
    expect(screen.queryByTestId('credentials.delete.dialog')).toBeNull();
  });
});

describe('API key', () => {
  it('stores the key encrypted, clears the field and reports failures', async () => {
    const t = await world();
    credentialsPage(t);
    const input = await screen.findByTestId('api-key.input');
    expect(screen.getByTestId('api-key.save')).toBeDisabled();
    expect(screen.queryByTestId('api-key.check')).toBeNull();
    fireEvent.change(input, { target: { value: 'has space' } });
    expect(screen.getByTestId('api-key.save')).toBeDisabled();
    fireEvent.change(input, { target: { value: ` ${API_KEY.key} ` } });
    fireEvent.click(screen.getByTestId('api-key.save'));
    expect(await screen.findByTestId('api-key.saved')).toBeVisible();
    expect(input).toHaveValue('');
    expect(await screen.findByTestId('credentials.item.anthropic_api_key')).toBeVisible();
    expect(JSON.stringify(t.hub.records.get('anthropic_api_key'))).not.toContain(API_KEY.key);
    t.client.on('PUT /v1/me/credentials/anthropic_api_key', () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.change(input, { target: { value: 'sk-ant-another' } });
    fireEvent.click(screen.getByTestId('api-key.save'));
    expect(await screen.findByTestId('api-key.failed')).toHaveAttribute('data-code', 'forbidden');
    noSecrets();
  });

  it('checks the stored key on a device: valid, refused, failures and hub ends', async () => {
    const t = await world();
    await seed(t, 'anthropic_api_key', API_KEY);
    const d = device(t);
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('api-key.check'));
    expect(screen.getByTestId('api-key.check-flow.devices.loading')).toBeVisible();
    fireEvent.click(await screen.findByTestId('api-key.check-flow.start'));
    expect(await screen.findByTestId('api-key.check-flow.waiting')).toHaveTextContent('Waiting for the execution');
    expect(d.sent[0]).toMatchObject({ kind: 'credential.check' });
    await d.respond(0, { step: 'done', valid: true }, true);
    expect(await screen.findByTestId('api-key.check-flow.result')).toHaveTextContent('The credential works.');
    fireEvent.click(screen.getByTestId('api-key.check-flow.close'));
    expect(screen.queryByTestId('api-key.check-flow')).toBeNull();
    fireEvent.click(screen.getByTestId('api-key.check'));
    fireEvent.click(await screen.findByTestId('api-key.check-flow.start'));
    await screen.findByTestId('api-key.check-flow.waiting');
    await d.setState(1, 'unreadable');
    expect(await screen.findByTestId('api-key.check-flow.waiting')).toHaveAttribute('data-read-error', 'unavailable');
    await d.setState(1, 'delivered');
    await d.respond(1, { step: 'done', valid: false }, true);
    expect(await screen.findByTestId('api-key.check-flow.result')).toHaveTextContent('The credential was refused.');
    fireEvent.click(screen.getByTestId('api-key.check-flow.close'));
    fireEvent.click(screen.getByTestId('api-key.check'));
    fireEvent.click(await screen.findByTestId('api-key.check-flow.start'));
    await screen.findByTestId('api-key.check-flow.waiting');
    await d.respond(2, { step: 'failed', code: 'upstream_unreachable' }, true);
    expect(await screen.findByTestId('api-key.check-flow.failed')).toHaveAttribute('data-code', 'upstream_unreachable');
    fireEvent.click(screen.getByTestId('api-key.check-flow.close'));
    d.refuse(new HubError('device_not_assigned', 403));
    fireEvent.click(screen.getByTestId('api-key.check'));
    fireEvent.click(await screen.findByTestId('api-key.check-flow.start'));
    expect(screen.getByTestId('api-key.check-flow.starting')).toBeVisible();
    expect(await screen.findByTestId('api-key.check-flow.failed')).toHaveAttribute('data-code', 'device_not_assigned');
  });
});

describe('device choice', () => {
  it('offers no device while all are offline, reports failures and tries again', async () => {
    const t = await world();
    await seed(t, 'mailbox', MAILBOX);
    let answer: () => unknown = () => ({
      devices: [
        { device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: false, last_seen_at: null },
      ],
    });
    t.client.on('GET /v1/me/devices', () => answer());
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('mailbox.check'));
    expect(await screen.findByTestId('mailbox.check-flow.devices.failed')).toHaveTextContent(
      'No execution device is online.',
    );
    answer = () => {
      throw new HubError('unavailable', 503);
    };
    fireEvent.click(screen.getByTestId('mailbox.check-flow.devices.retry'));
    expect(await screen.findByTestId('mailbox.check-flow.devices.failed')).toHaveAttribute('data-code', 'unavailable');
  });

  it('lists offline devices disabled next to the online one', async () => {
    const t = await world();
    await seed(t, 'mailbox', MAILBOX);
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        { device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null },
        {
          device_id: t.devices[0]!.id,
          kind: 'platform',
          org_id: t.org,
          wg_ids: [t.wg],
          online: false,
          last_seen_at: null,
        },
      ],
    }));
    await t.world.batch(t.me, [
      {
        type: 'device_assign',
        device: t.devices[0]!.id,
        wg: t.wg,
        platform_seq: t.platform.seqs.get(t.devices[0]!.id)!,
      },
    ]);
    (await import('../../src/core/store/slices/orgs')).setLog(t.store, t.org, t.world.log!);
    const d = device(t);
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('mailbox.check'));
    const select = await screen.findByTestId('mailbox.check-flow.devices.select');
    const options = within(select).getAllByRole('option');
    expect(options[1]).toBeDisabled();
    expect(options[1]).toHaveTextContent('(offline)');
    fireEvent.change(select, { target: { value: t.device.id } });
    fireEvent.click(screen.getByTestId('mailbox.check-flow.start'));
    await screen.findByTestId('mailbox.check-flow.waiting');
    expect(d.sent[0]).toMatchObject({ kind: 'mailbox.check' });
    await d.setState(0, 'expired');
    expect(await screen.findByTestId('mailbox.check-flow.failed')).toHaveAttribute('data-code', 'timeout');
  });
});

describe('Claude subscription', () => {
  it('connects through the relay: link, pasted code as follow-up, e-mail; the token never shows', async () => {
    const t = await world();
    const d = device(t);
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('claude.connect'));
    expect(await screen.findByTestId('claude.mode.automatic')).toBeDisabled();
    expect(screen.getByTestId('claude.mode')).toHaveTextContent('store a mailbox below first');
    expect(screen.getByTestId('claude.mode.manual')).toBeChecked();
    fireEvent.click(await screen.findByTestId('claude.start'));
    expect(await screen.findByTestId('claude.waiting')).toBeVisible();
    expect(d.sent[0]).toMatchObject({ kind: 'claude.login', follows: '' });
    await d.respond(0, { step: 'authorize', url: 'https://claude.ai/oauth/authorize?code=true' });
    const open = await screen.findByTestId('claude.authorize.open');
    expect(open).toHaveAttribute('href', 'https://claude.ai/oauth/authorize?code=true');
    expect(open).toHaveAttribute('target', '_blank');
    expect(open).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByTestId('claude.automation-failed')).toBeNull();
    const send = screen.getByTestId('claude.code.send');
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByTestId('claude.code'), { target: { value: 'x'.repeat(513) } });
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByTestId('claude.code'), { target: { value: ' abc#123 ' } });
    fireEvent.click(send);
    expect(await screen.findByText('Code sent')).toBeVisible();
    expect(screen.getByTestId('claude.code')).toBeDisabled();
    expect(d.sent[1]).toMatchObject({ kind: 'claude.login', follows: d.sent[0]!.id });
    await d.setState(1, 'unreadable');
    expect(await screen.findByTestId('claude.read-failed')).toHaveAttribute('data-code', 'unavailable');
    await d.setState(1, 'delivered');
    t.hub.records.set('claude_subscription', {
      record: (await seed(t, 'claude_subscription', SUBSCRIPTION, { author: t.device }))!,
      version: 1,
      epoch: 1,
      updated_at: '2026-11-21T12:00:00.000Z',
    });
    await d.respond(1, { step: 'done', email: 'claude@example.com' }, true);
    expect(await screen.findByTestId('claude.connected')).toHaveTextContent(
      'The subscription claude@example.com is connected.',
    );
    expect(screen.queryByTestId('claude.flow')).toBeNull();
    expect(await screen.findByTestId('credentials.item.claude_subscription')).toBeVisible();
    noSecrets();
    fireEvent.click(screen.getByTestId('claude.connect'));
    expect(screen.queryByTestId('claude.connected')).toBeNull();
    fireEvent.click(screen.getByTestId('claude.close'));
    expect(screen.queryByTestId('claude.flow')).toBeNull();
  });

  it('runs the automation with progress, falls back to the relay and connects without an e-mail', async () => {
    const t = await world();
    await seed(t, 'mailbox', MAILBOX);
    const d = device(t);
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('claude.connect'));
    expect(await screen.findByTestId('claude.mode.automatic')).toBeChecked();
    fireEvent.click(screen.getByTestId('claude.mode.manual'));
    fireEvent.click(screen.getByTestId('claude.mode.automatic'));
    fireEvent.click(await screen.findByTestId('claude.start'));
    await screen.findByTestId('claude.waiting');
    await d.respond(0, { step: 'progress', stage: 'magic_link_received' });
    expect(await screen.findByTestId('claude.progress')).toHaveTextContent('The login e-mail arrived');
    await d.respond(0, { step: 'authorize', url: 'https://claude.ai/x', automation: 'failed' });
    expect(await screen.findByTestId('claude.automation-failed')).toHaveTextContent('Automatic login failed');
    fireEvent.change(screen.getByTestId('claude.code'), { target: { value: 'abc' } });
    fireEvent.submit(screen.getByTestId('claude.authorize'));
    await screen.findByText('Code sent');
    await d.respond(1, { step: 'done' }, true);
    expect(await screen.findByTestId('claude.connected')).toHaveTextContent('The subscription is connected.');
  });

  it('reports failures of the device, of sending the code and of the start, and tries again', async () => {
    const t = await world();
    const d = device(t);
    credentialsPage(t);
    fireEvent.click(await screen.findByTestId('claude.connect'));
    fireEvent.click(await screen.findByTestId('claude.start'));
    await screen.findByTestId('claude.waiting');
    await d.respond(0, { step: 'authorize', url: 'https://claude.ai/x' });
    d.refuse(new HubError('rate_limited', 429));
    fireEvent.change(await screen.findByTestId('claude.code'), { target: { value: 'abc' } });
    fireEvent.click(screen.getByTestId('claude.code.send'));
    expect(await screen.findByTestId('claude.code.failed')).toHaveAttribute('data-code', 'rate_limited');
    d.refuse(null);
    fireEvent.click(screen.getByTestId('claude.code.send'));
    await screen.findByText('Code sent');
    expect(screen.queryByTestId('claude.code.failed')).toBeNull();
    await d.respond(1, { step: 'failed', code: 'code_invalid' }, true);
    expect(await screen.findByTestId('claude.failed')).toHaveTextContent('The code was not accepted.');
    fireEvent.click(screen.getByTestId('claude.retry'));
    d.refuse(new HubError('device_not_assigned', 403));
    fireEvent.click(await screen.findByTestId('claude.start'));
    expect(await screen.findByTestId('claude.failed')).toHaveAttribute('data-code', 'device_not_assigned');
    expect(screen.queryByTestId('claude.starting')).toBeNull();
    d.refuse(null);
    fireEvent.click(screen.getByTestId('claude.retry'));
    fireEvent.click(await screen.findByTestId('claude.start'));
    expect(screen.getByTestId('claude.starting')).toBeVisible();
    await screen.findByTestId('claude.waiting');
    await d.setState(2, 'failed');
    expect(await screen.findByTestId('claude.failed')).toHaveAttribute('data-code', 'request_failed');
  });
});

describe('mailbox', () => {
  it('stores the mailbox from a valid form, reports failures and checks it', async () => {
    const t = await world();
    const d = device(t);
    credentialsPage(t);
    const save = await screen.findByTestId('mailbox.save');
    expect(screen.getByTestId('mailbox.imap_password')).toHaveAttribute('type', 'password');
    expect(screen.getByTestId('mailbox.imap_port')).toHaveValue(993);
    expect(save).toBeDisabled();
    for (const [field, value] of Object.entries({
      imap_host: 'imap.example.com',
      imap_user: 'claude@example.com',
      imap_password: MAILBOX.imap_password,
      email: 'claude@example.com',
    })) {
      fireEvent.change(screen.getByTestId(`mailbox.${field}`), { target: { value } });
    }
    expect(save).toBeEnabled();
    t.client.on('PUT /v1/me/credentials/mailbox', () => {
      throw new HubError('epoch_stale', 400);
    });
    fireEvent.click(save);
    expect(await screen.findByTestId('mailbox.failed')).toHaveAttribute('data-code', 'epoch_stale');
    t.client.on('PUT /v1/me/credentials/mailbox', (body) => t.hub.put('mailbox', body as never));
    fireEvent.click(save);
    expect(await screen.findByTestId('mailbox.saved')).toBeVisible();
    expect(screen.getByTestId('mailbox.imap_password')).toHaveValue('');
    expect(await screen.findByTestId('credentials.item.mailbox.detail')).toBeVisible();
    noSecrets();
    fireEvent.click(screen.getByTestId('mailbox.check'));
    fireEvent.click(await screen.findByTestId('mailbox.check-flow.start'));
    await screen.findByTestId('mailbox.check-flow.waiting');
    await d.respond(0, { step: 'done', valid: true }, true);
    expect(await screen.findByTestId('mailbox.check-flow.result')).toHaveAttribute('data-valid', 'true');
    fireEvent.click(screen.getByTestId('mailbox.check-flow.close'));
    expect(screen.getByTestId('mailbox.check')).toBeVisible();
  });
});

const CONFIG = {
  models: {
    platform: ['deepseek/deepseek-v4.1-flash-1017'],
    api_key: ['claude-sonnet-4-5', 'claude-opus-4-1'],
    subscription: ['claude-opus-4-1'],
  },
  defaults: { platform: 'deepseek/deepseek-v4.1-flash-1017' },
} as unknown as PlatformConfig;

describe('model access', () => {
  it('saves the preference and a configured model; the model falls back when the preference does not allow it', async () => {
    const t = await world();
    setConfig(t.store, CONFIG);
    t.client.on('PUT /v1/me/settings', (body) => body);
    mount(t, [{ path: '/settings', element: <ModelSection /> }], '/settings');
    expect(screen.getByTestId('model.access.auto')).toBeChecked();
    expect(screen.getByTestId('model.model.none')).toBeVisible();
    expect(screen.getByTestId('model.platform-note')).toHaveTextContent('only on DFX execution devices');
    fireEvent.click(screen.getByTestId('model.access.api_key'));
    const model = screen.getByTestId('model.model');
    expect(
      within(model)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Default of the access', 'claude-sonnet-4-5', 'claude-opus-4-1']);
    fireEvent.change(model, { target: { value: 'claude-opus-4-1' } });
    fireEvent.click(screen.getByTestId('model.access.subscription'));
    expect(model).toHaveValue('claude-opus-4-1');
    fireEvent.click(screen.getByTestId('model.access.platform'));
    expect(model).toHaveValue('');
    expect(within(model).getAllByRole('option')[0]).toHaveTextContent('Default (deepseek/deepseek-v4.1-flash-1017)');
    fireEvent.change(model, { target: { value: 'deepseek/deepseek-v4.1-flash-1017' } });
    fireEvent.change(model, { target: { value: '' } });
    fireEvent.click(screen.getByTestId('model.save'));
    expect(await screen.findByTestId('model.saved')).toBeVisible();
    expect(t.client.of('PUT /v1/me/settings')[0]!.body).toEqual({
      model_access: 'platform',
      model_id: null,
      locale: 'en',
    });
    t.client.on('PUT /v1/me/settings', () => {
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('model.save'));
    expect(await screen.findByTestId('model.failed')).toHaveAttribute('data-code', 'unavailable');
  });

  it('starts from the stored settings', async () => {
    const t = await world();
    setConfig(t.store, CONFIG);
    setProfile(t.store, { settings: { model_access: 'api_key', model_id: 'claude-sonnet-4-5', locale: 'de' } });
    mount(t, [{ path: '/settings', element: <ModelSection /> }], '/settings', 'de');
    expect(screen.getByTestId('model.access.api_key')).toBeChecked();
    expect(screen.getByTestId('model.model')).toHaveValue('claude-sonnet-4-5');
    fireEvent.click(screen.getByTestId('model.access.auto'));
    expect(screen.getByTestId('model.model')).toHaveValue('');
  });
});

describe('quotas', () => {
  it('shows bars with reset times, "not available" without rows, failures and reloads', async () => {
    const t = await world();
    let answer: () => unknown = () => ({
      quotas: [
        {
          window: 'five_hour',
          used_percent: 42,
          resets_at: '2026-11-21T15:00:00.000Z',
          severity: null,
          fetched_at: '2026-11-21T12:00:00.000Z',
          device_id: t.device.id,
        },
        {
          window: 'weekly:claude-opus-4-1',
          used_percent: 120,
          resets_at: null,
          severity: 'warning',
          fetched_at: '2026-11-21T12:00:00.000Z',
          device_id: t.device.id,
        },
      ],
    });
    t.client.on('GET /v1/me/quotas', () => answer());
    mount(t, [{ path: '/settings', element: <QuotasSection /> }], '/settings');
    expect(screen.getByTestId('quotas.loading')).toBeVisible();
    expect(await screen.findByTestId('quotas.bar.five_hour.used')).toHaveTextContent('42%');
    expect(screen.getByTestId('quotas.bar.five_hour')).toHaveTextContent('5 hours');
    expect(screen.getByTestId('quotas.bar.five_hour.reset')).toHaveTextContent(/^Resets /);
    expect(screen.getByTestId('quotas.bar.weekly:claude-opus-4-1')).toHaveTextContent('Week, claude-opus-4-1');
    expect(screen.getByTestId('quotas.bar.weekly:claude-opus-4-1.reset')).toHaveTextContent('Reset time unknown');
    expect(screen.getByTestId('quotas.bar.weekly:claude-opus-4-1.meter')).toHaveAttribute('value', '1');
    answer = () => {
      throw new HubError('unavailable', 503);
    };
    fireEvent.click(screen.getByTestId('quotas.reload'));
    expect(await screen.findByTestId('quotas.load-failed')).toHaveAttribute('data-code', 'unavailable');
    answer = () => ({ quotas: [] });
    fireEvent.click(screen.getByTestId('quotas.reload'));
    expect(await screen.findByTestId('quotas.unavailable')).toBeVisible();
    expect(screen.queryByTestId('quotas.load-failed')).toBeNull();
    act(() => setQuotas(t.store, []));
  });
});

const usage = (overrides: Partial<SessionUsage> = {}): SessionUsage => ({
  requests: 2,
  input_tokens: 12_345,
  output_tokens: 678,
  cache_read_tokens: 100,
  cache_write_tokens: 0,
  thinking_tokens: 0,
  cost_micro_usd: 1300,
  cost_basis: 'provider',
  context_used_tokens: 30_000,
  context_window_tokens: 120_000,
  last_provider: 'DeepInfra',
  updated_at: '2026-11-21T12:00:00.000Z',
  ...overrides,
});

describe('usage strip', () => {
  it('shows tokens, cost with basis and context of the main session, live', async () => {
    const t = await world();
    const task = taskRow(t, { main_session_id: 's-main' }) as never;
    putTasks(t.store, [task]);
    const id = (task as { task_id: string }).task_id;
    render(<UsageSlot orgId={t.org} taskId={id} strip={null} />);
    expect(screen.getByTestId('chat.usage')).toBeEmptyDOMElement();
    mount(t, [{ path: '/', element: <UsageStrip orgId={t.org} taskId={id} /> }], '/');
    expect(await screen.findByTestId('usage.none')).toHaveTextContent('No model usage yet.');
    act(() => putUsage(t.store, id, 's-main', usage()));
    expect(screen.getByTestId('usage.tokens')).toHaveTextContent('12,345 in, 678 out, 100 cache');
    expect(screen.getByTestId('usage.cost')).toHaveTextContent('$0.0013 (provider)');
    expect(screen.getByTestId('usage.context')).toHaveTextContent('25%');
    act(() =>
      putUsage(t.store, id, 's-child', usage({ cost_basis: 'subscription_equivalent', cost_micro_usd: 2_000_000 })),
    );
    expect(screen.getByTestId('usage.cost')).toHaveTextContent('$2.00 (provider, subscription equivalent)');
    expect(screen.getByTestId('usage.cost')).toHaveAttribute('data-basis', 'provider subscription_equivalent');
  });

  it('says the context is unknown without a main session', async () => {
    const t = await world();
    putUsage(t.store, 't-x', 's1', usage({ cost_basis: 'list_price' }));
    mount(t, [{ path: '/', element: <UsageStrip orgId={t.org} taskId="t-x" /> }], '/', 'de');
    expect(await screen.findByTestId('usage.context')).toHaveTextContent('unbekannt');
    expect(screen.getByTestId('usage.cost')).toHaveTextContent('Listenpreis');
  });
});

describe('rotation toast', () => {
  it('announces each rotation, closes by itself or with its button', async () => {
    const t = await world();
    vi.useFakeTimers();
    mount(t, [{ path: '/', element: <CredentialRotationToasts /> }], '/');
    const rotated = (dropped: number) => ({ status: 'rotated' as const, epoch: 2, records: 2, dropped });
    act(() => noteCredentialRotation(t.store, { result: rotated(0), at: 0 }));
    expect(screen.getByTestId('credential-rotation.item')).toHaveTextContent('2 record(s) re-encrypted.');
    expect(screen.getByTestId('credential-rotation.item')).not.toHaveTextContent('deleted');
    act(() => vi.advanceTimersByTime(TOAST_MS));
    expect(screen.queryByTestId('credential-rotation.item')).toBeNull();
    act(() => noteCredentialRotation(t.store, { result: rotated(1), at: 0 }));
    expect(screen.getByTestId('credential-rotation.item')).toHaveTextContent('1 record(s) stored by that device');
    fireEvent.click(screen.getByTestId('credential-rotation.close'));
    act(() =>
      noteCredentialRotation(t.store, {
        result: { status: 'blocked', kinds: ['mailbox', 'anthropic_api_key'] },
        at: 0,
      }),
    );
    expect(screen.getByTestId('credential-rotation.item')).toHaveTextContent(
      'blocked: Mailbox for the automatic login, Anthropic API key cannot be opened',
    );
    fireEvent.click(screen.getByTestId('credential-rotation.close'));
    expect(screen.queryByTestId('credential-rotation.item')).toBeNull();
  });
});
