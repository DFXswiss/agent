// GitHub identities in jsdom (ADR-078, KM-35): the own accounts in the settings (primary badge, make primary,
// reconnect, disconnect a further account, add one with a signing key), the company accounts screen (members read,
// admins connect – the device's body is sealed for the organization's devices – reconnect, remove and set the default,
// `peer_outdated` with device names), the workgroup default, the identity of a new task and of a task, and the
// attribution of operations.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { bodyJson } from '../../src/core/encoding/json';
import { sealResponse } from '../../src/core/crypto/request';
import { HubError } from '../../src/core/net/signedFetch';
import { putEvents } from '../../src/core/store/slices/events';
import { noteCompanyLogin } from '../../src/core/store/slices/githubIdentities';
import { type OperationRow, putOperations } from '../../src/core/store/slices/operations';
import { setLog } from '../../src/core/store/slices/orgs';
import { noteResponse } from '../../src/core/store/slices/requests';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { Composer } from '../../src/ui/composer/Composer';
import { routes as companyRoutes } from '../../src/ui/screens/github-accounts/routes';
import { nav } from '../../src/ui/screens/github-accounts/nav';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { routes as wgRoutes } from '../../src/ui/screens/wg-settings/routes';
import { GithubSection } from '../../src/ui/settings/sections/github';
import { TS } from '../core/log/world';
import { taskRow } from '../core/tasks/world';
import type { CredWorld } from '../core/credentials/world';
import { BODY, company, COMPANY, type IdentityHub, own, OWN, SECOND, sealed, world } from '../core/github/identity-hub';
import { MAIN, mount, TASK } from './chat-world';
import { labelDevice } from './device-labels';

type World = { t: CredWorld; hub: IdentityHub };

async function setup(options: { member?: boolean } = {}): Promise<World> {
  const w = await world(options);
  const { t } = w;
  t.client.on('GET /v1/me/devices', () => ({
    devices: [{ device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null }],
  }));
  t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  return w;
}

/** Serves the request the flow posts, with responses sealed by the device. */
function deviceAnswers(t: CredWorld) {
  const state = { id: '', bodies: [] as { body: unknown; final: boolean }[], posted: [] as unknown[] };
  t.client.on('POST /v1/device-requests', (body) => {
    state.posted.push(body);
    state.bodies = [];
    state.id = (body as { envelope: { header: { id: string } } }).envelope.header.id;
    t.client.on(`GET /v1/device-requests/${state.id}`, async () => {
      const responses = [];
      for (const [index, item] of state.bodies.entries()) {
        const header = {
          v: 1,
          id: `0190f000-0000-7000-8000-00000000f1${index}0`,
          user: t.me.id,
          device: t.device.id,
          kind: 'github.connect',
          in_reply_to: state.id,
          seq: index + 1,
          final: item.final,
          ts: new Date(t.clock.now).toISOString(),
          enc_alg: 'HPKE-X25519-A256GCM',
        };
        const envelope = await sealResponse(
          header,
          t.me.identity.kemPublic(),
          bodyJson(item.body),
          t.device.identity.sig.privateKey,
        );
        responses.push({ seq: index + 1, envelope, created_at: header.ts });
      }
      return {
        request_id: state.id,
        kind: 'github.connect',
        device_id: t.device.id,
        state: 'delivered',
        created_at: TS,
        expires_at: '2026-11-21T12:15:00.000Z',
        responses,
      };
    });
    return { request_id: state.id, state: 'pending' };
  });
  const respond = async (body: unknown, final = true) => {
    state.bodies.push({ body, final });
    await act(async () => noteResponse(t.store, state.id, state.bodies.length));
  };
  return { state, respond };
}

const settings = (t: CredWorld) => mount(t, [{ path: '/settings', element: <GithubSection /> }], '/settings');

describe('own accounts in the settings', () => {
  it('lists the accounts with the primary badge and details, makes one primary and removes a further one', async () => {
    const { t, hub } = await setup();
    const key = { alg: 'ssh-ed25519', seed: `${'A'.repeat(42)}w`, public: `ssh-ed25519 ${'A'.repeat(68)}` };
    own(hub, OWN, await sealed(t), { primary: true });
    own(
      hub,
      SECOND,
      await sealed(t, {
        id: SECOND,
        body: { ...BODY, token: 'ghp_transferred', login: 'octo-two', name: 'Octo Two', signing_key: key },
      }),
    );
    t.client.on(`PUT /v1/github-identities/${SECOND}/primary`, () => ({}));
    let deletes = 0;
    t.client.request = (
      (original) =>
      async (method: string, target: string, options: { body?: unknown } = {}) => {
        if (method === 'DELETE' && target === `/v1/github-identities/${SECOND}`) {
          deletes += 1;
          if (deletes === 1) throw new HubError('unavailable', 503);
          hub.records.delete(SECOND);
          return {};
        }
        return original(method, target, options);
      }
    )(t.client.request);
    settings(t);
    const items = await screen.findAllByTestId('github.accounts.item');
    expect(items.map((item) => within(item).getByTestId('github.accounts.login').textContent)).toEqual([
      'octo-cat',
      'octo-two (Octo Two)',
    ]);
    expect(within(items[0]!).getByTestId('github.accounts.primary')).toHaveTextContent('Primary');
    expect(within(items[0]!).queryByTestId('github.accounts.remove')).toBeNull();
    expect(within(items[1]!).getByTestId('github.accounts.signing')).toHaveAttribute('data-signing', 'true');
    expect(within(items[0]!).getByTestId('github.accounts.scopes')).toHaveTextContent('repo');
    fireEvent.click(within(items[1]!).getByTestId('github.accounts.make-primary'));
    await waitFor(() => expect(t.client.of(`PUT /v1/github-identities/${SECOND}/primary`)).toHaveLength(1));
    fireEvent.click(within(screen.getAllByTestId('github.accounts.item')[1]!).getByTestId('github.accounts.remove'));
    const dialog = screen.getByTestId('github.accounts.remove.dialog');
    expect(dialog).toHaveTextContent('stays valid at GitHub');
    fireEvent.click(within(dialog).getByTestId('github.accounts.remove.cancel'));
    fireEvent.click(within(screen.getAllByTestId('github.accounts.item')[1]!).getByTestId('github.accounts.remove'));
    fireEvent.click(screen.getByTestId('github.accounts.remove.confirm'));
    expect(await screen.findByTestId('github.accounts.remove.failed')).toHaveTextContent('Service unavailable');
    fireEvent.click(screen.getByTestId('github.accounts.remove.confirm'));
    await waitFor(() => expect(screen.getAllByTestId('github.accounts.item')).toHaveLength(1));
  });

  it('says a minted token is revoked, shows unverifiable accounts and a failed change', async () => {
    const { t, hub } = await setup();
    own(hub, OWN, await sealed(t, { body: { ...BODY, token: 'ghu_minted' } }));
    own(hub, SECOND, await sealed(t, { id: SECOND, epoch: 2 }));
    own(hub, COMPANY, await sealed(t, { id: COMPANY, body: { ...BODY, scopes: [] } }));
    let lists = 0;
    t.client.on('GET /v1/me/credentials', () => {
      lists += 1;
      if (lists > 1) throw new HubError('unavailable', 503);
      return { credentials: [] };
    });
    t.client.on(`PUT /v1/github-identities/${COMPANY}/primary`, () => ({}));
    t.client.on(`PUT /v1/github-identities/${OWN}/primary`, () => {
      throw new HubError('forbidden', 403);
    });
    settings(t);
    const items = await screen.findAllByTestId('github.accounts.item');
    expect(items[1]).toHaveAttribute('data-status', 'unverifiable');
    expect(within(items[1]!).queryByTestId('github.accounts.make-primary')).toBeNull();
    fireEvent.click(within(items[0]!).getByTestId('github.accounts.make-primary'));
    expect(await screen.findByTestId('github.accounts.failed')).toBeVisible();
    expect(within(items[2]!).getByTestId('github.accounts.scopes')).toHaveTextContent('those of the GitHub App');
    fireEvent.click(within(items[0]!).getByTestId('github.accounts.remove'));
    expect(screen.getByTestId('github.accounts.remove.dialog')).toHaveTextContent('revokes it there');
    fireEvent.keyDown(screen.getByTestId('github.accounts.remove.dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('github.accounts.remove.dialog')).toBeNull();
    // a change reloads the identities and the primary connection (whose failure the section does not show here)
    fireEvent.click(within(items[2]!).getByTestId('github.accounts.make-primary'));
    await waitFor(() => expect(lists).toBe(2));
    expect(screen.queryByTestId('github.load-failed')).toBeNull();
  });

  it('adds another account with a signing key and reconnects a listed one', async () => {
    const { t, hub } = await setup();
    own(hub, OWN, await sealed(t), { primary: true });
    const answers = deviceAnswers(t);
    settings(t);
    fireEvent.click(await screen.findByTestId('github.accounts.add'));
    const panel = screen.getByTestId('github.accounts.add-panel');
    fireEvent.click(within(panel).getByTestId('github.accounts.add-panel.close'));
    fireEvent.click(screen.getByTestId('github.accounts.add'));
    fireEvent.click(screen.getByTestId('github.accounts.add-panel.signing'));
    fireEvent.click(screen.getByTestId('github.accounts.add-panel.device-flow'));
    fireEvent.click(await screen.findByTestId('github.start'));
    await waitFor(() => expect(answers.state.posted).toHaveLength(1));
    own(hub, SECOND, await sealed(t, { id: SECOND, body: { ...BODY, login: 'octo-two' } }));
    await answers.respond({ step: 'done', login: 'octo-two', identity_id: SECOND, signing: 'scope_missing' });
    await waitFor(() => expect(screen.getAllByTestId('github.accounts.item')).toHaveLength(2));
    fireEvent.click(within(screen.getAllByTestId('github.accounts.item')[0]!).getByTestId('github.accounts.reconnect'));
    const reconnect = screen.getByTestId('github.accounts.reconnect-panel');
    fireEvent.click(within(reconnect).getByTestId('github.accounts.reconnect-panel.token'));
    expect(await screen.findByTestId('github.token.input')).toBeVisible();
    fireEvent.click(screen.getByTestId('github.flow.close'));
    expect(screen.queryByTestId('github.flow')).toBeNull();
  });

  it('shows the signing outcome of a connection', async () => {
    const { t, hub } = await setup();
    own(hub, OWN, await sealed(t), { primary: true });
    const answers = deviceAnswers(t);
    settings(t);
    fireEvent.click(await screen.findByTestId('github.accounts.reconnect'));
    fireEvent.click(screen.getByTestId('github.accounts.reconnect-panel.device-flow'));
    fireEvent.click(await screen.findByTestId('github.start'));
    await waitFor(() => expect(answers.state.posted).toHaveLength(1));
    // an answer whose flow stays mounted: a done with a signing outcome renders it before the reload
    await answers.respond({ step: 'done', login: 'octo-cat', signing: 'registered' });
    await waitFor(() => expect(t.client.of('GET /v1/me/github-identities').length).toBeGreaterThan(1));
  });

  it('reports a failed load and loads again; shows nothing without any account', async () => {
    const { t, hub } = await setup();
    let fail = true;
    t.client.on('GET /v1/me/github-identities', () => {
      if (fail) throw new HubError('unavailable', 503);
      return hub.list();
    });
    settings(t);
    expect(await screen.findByTestId('github.accounts.load-failed')).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByTestId('github.accounts.reload'));
    await waitFor(() => expect(screen.queryByTestId('github.accounts.load-failed')).toBeNull());
    expect(screen.queryByTestId('github.accounts')).toBeNull();
  });
});

const companyPage = (t: CredWorld) => mount(t, companyRoutes as never, `/o/${t.org}/github`);

describe('company accounts', () => {
  it('is in the navigation and shows members the accounts without controls', async () => {
    expect(nav).toEqual([{ id: 'github', order: 63, to: '/o/:org/github', labelKey: 'identities.nav' }]);
    const { t, hub } = await setup({ member: true });
    company(hub, COMPANY);
    hub.defaults = [{ scope: 'org', scope_id: t.org, identity_id: COMPANY, set_by: t.ada.id, set_at: TS }];
    companyPage(t);
    const item = await screen.findByTestId('company-github.item');
    expect(within(item).getByTestId('company-github.label')).toHaveTextContent(
      'Company account of the organization · connected',
    );
    expect(within(item).getByTestId('company-github.default')).toBeVisible();
    expect(screen.queryByTestId('company-github.add')).toBeNull();
    expect(within(item).queryByTestId('company-github.remove')).toBeNull();
  });

  it('connects a company account: the device answers the body, the browser seals it for the devices', async () => {
    const { t, hub } = await setup();
    putEvents(t.store, t.org, [
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { id: 'p1', kind: 'org.profile', author: t.me.id },
        body: { name: 'Acme' },
      },
    ]);
    const answers = deviceAnswers(t);
    companyPage(t);
    expect(await screen.findByTestId('company-github.empty')).toBeVisible();
    fireEvent.click(screen.getByTestId('company-github.add'));
    fireEvent.click(screen.getByTestId('company-github.add-panel.close'));
    fireEvent.click(screen.getByTestId('company-github.add'));
    fireEvent.click(screen.getByTestId('company-github.add-panel.device-flow'));
    fireEvent.click(await screen.findByTestId('github.start'));
    await waitFor(() => expect(answers.state.posted).toHaveLength(1));
    expect(answers.state.posted[0]).toMatchObject({ org_id: t.org });
    await answers.respond({
      step: 'done',
      login: 'acme-bot',
      source: 'operator',
      body: { ...BODY, login: 'acme-bot' },
    });
    const item = await screen.findByTestId('company-github.item');
    expect(item).toHaveTextContent('Company account of Acme · connected');
    expect(item).toHaveTextContent('(acme-bot)');
    const put = hub.puts.at(-1)!;
    expect(put.body).toMatchObject({ owner_id: t.org, expected_version: 0, key: { epoch: 1 } });
  });

  it('reports a done without a body and lets the admin try again', async () => {
    const { t } = await setup();
    const answers = deviceAnswers(t);
    companyPage(t);
    fireEvent.click(await screen.findByTestId('company-github.add'));
    fireEvent.click(screen.getByTestId('company-github.add-panel.device-flow'));
    fireEvent.click(await screen.findByTestId('github.start'));
    await waitFor(() => expect(answers.state.posted).toHaveLength(1));
    await answers.respond({ step: 'done', login: 'acme-bot' });
    expect(await screen.findByTestId('github.save-failed')).toHaveAttribute('data-code', 'params_invalid');
    fireEvent.click(screen.getByTestId('github.retry'));
    expect(await screen.findByTestId('github.start')).toBeVisible();
  });

  it('sets and clears the default, reconnects, removes and names outdated devices', async () => {
    const { t, hub } = await setup();
    company(hub, COMPANY);
    noteCompanyLogin(t.store, COMPANY, 'acme-bot');
    labelDevice(t.store, t.org, t.device.id, 'Build box');
    const outcomes: unknown[] = [
      new HubError('peer_outdated', 409, { devices: [t.device.id] }),
      new HubError('peer_outdated', 409),
      new HubError('identity_forbidden', 403),
      {},
    ];
    t.client.on(`PUT /v1/github-defaults/org/${t.org}`, (body) => {
      const next = outcomes.shift();
      if (next instanceof HubError) throw next;
      hub.defaults =
        (body as { identity_id: string | null }).identity_id === null
          ? []
          : [{ scope: 'org', scope_id: t.org, identity_id: COMPANY, set_by: t.me.id, set_at: TS }];
      return {};
    });
    companyPage(t);
    const item = await screen.findByTestId('company-github.item');
    expect(item).toHaveTextContent('acme-bot');
    fireEvent.click(within(item).getByTestId('company-github.make-default'));
    expect(await screen.findByTestId('company-github.failed')).toHaveTextContent(
      'Some devices of this organization must be updated first: Build box.',
    );
    fireEvent.click(within(item).getByTestId('company-github.make-default'));
    await waitFor(() =>
      expect(screen.getByTestId('company-github.failed')).toHaveTextContent(
        'Some devices of this organization must be updated first.',
      ),
    );
    fireEvent.click(within(item).getByTestId('company-github.make-default'));
    await waitFor(() =>
      expect(screen.getByTestId('company-github.failed')).toHaveAttribute('data-code', 'identity_forbidden'),
    );
    fireEvent.click(within(item).getByTestId('company-github.make-default'));
    expect(await screen.findByTestId('company-github.default')).toBeVisible();
    outcomes.push({});
    fireEvent.click(screen.getByTestId('company-github.clear-default'));
    await waitFor(() => expect(screen.queryByTestId('company-github.default')).toBeNull());
    fireEvent.click(screen.getByTestId('company-github.reconnect'));
    expect(screen.getByTestId('company-github.reconnect-panel')).toBeVisible();
    fireEvent.click(screen.getByTestId('company-github.reconnect-panel.close'));
    let removes = 0;
    t.client.request = (
      (original) =>
      async (method: string, target: string, options: { body?: unknown } = {}) => {
        if (method === 'DELETE') {
          removes += 1;
          if (removes === 1) throw new HubError('unavailable', 503);
          hub.records.delete(COMPANY);
          return {};
        }
        return original(method, target, options);
      }
    )(t.client.request);
    fireEvent.click(screen.getByTestId('company-github.remove'));
    fireEvent.keyDown(screen.getByTestId('company-github.remove.dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('company-github.remove.dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('company-github.remove'));
    fireEvent.click(screen.getByTestId('company-github.remove.cancel'));
    fireEvent.click(screen.getByTestId('company-github.remove'));
    fireEvent.click(screen.getByTestId('company-github.remove.confirm'));
    expect(await screen.findByTestId('company-github.remove.failed')).toBeVisible();
    fireEvent.click(screen.getByTestId('company-github.remove.confirm'));
    expect(await screen.findByTestId('company-github.empty')).toBeVisible();
  });

  it('reconnects into the same record', async () => {
    const { t, hub } = await setup();
    company(hub, COMPANY, { version: 2 });
    const answers = deviceAnswers(t);
    companyPage(t);
    fireEvent.click(await screen.findByTestId('company-github.reconnect'));
    fireEvent.click(screen.getByTestId('company-github.reconnect-panel.device-flow'));
    fireEvent.click(await screen.findByTestId('github.start'));
    await waitFor(() => expect(answers.state.posted).toHaveLength(1));
    await answers.respond({ step: 'done', login: 'acme-bot', body: BODY });
    await waitFor(() => expect(hub.puts).toHaveLength(1));
    expect(hub.puts[0]).toMatchObject({ id: COMPANY, body: { expected_version: 2 } });
  });

  it('waits for the verified org and reports a failed load', async () => {
    const { t } = await setup();
    let fail = true;
    t.client.on('GET /v1/me/github-identities', () => {
      if (fail) throw new HubError('unavailable', 503);
      return { identities: [], defaults: [] };
    });
    companyPage(t);
    expect(await screen.findByTestId('company-github.load-failed')).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByTestId('company-github.reload'));
    expect(await screen.findByTestId('company-github.empty')).toBeVisible();
    const other = await setup();
    mount(other.t, companyRoutes as never, '/o/0190f000-0000-7000-8000-00000000eeee/github');
    expect(await screen.findByTestId('company-github.unverified')).toBeVisible();
    expect(screen.getAllByTestId('company-github.screen')).toHaveLength(1);
  });
});

describe('workgroup default', () => {
  it('lets writers choose a company account of the org and names outdated devices', async () => {
    const { t, hub } = await setup();
    company(hub, COMPANY);
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    let fail = true;
    t.client.on(`PUT /v1/github-defaults/wg/${t.wg}`, (body) => {
      if (fail) throw new HubError('peer_outdated', 409);
      hub.defaults = [
        {
          scope: 'wg',
          scope_id: t.wg,
          identity_id: (body as { identity_id: string }).identity_id,
          set_by: t.me.id,
          set_at: TS,
        },
      ];
      return {};
    });
    mount(t, wgRoutes as never, `/o/${t.org}/w/${t.wg}/settings`);
    const select = await screen.findByTestId('wg-settings.github.select');
    expect(select).toHaveValue('');
    fireEvent.change(select, { target: { value: COMPANY } });
    expect(await screen.findByTestId('wg-settings.github.failed')).toHaveTextContent('updated first');
    fail = false;
    fireEvent.change(select, { target: { value: COMPANY } });
    await waitFor(() => expect(screen.getByTestId('wg-settings.github.select')).toHaveValue(COMPANY));
    fireEvent.change(select, { target: { value: '' } });
    await waitFor(() =>
      expect(t.client.of(`PUT /v1/github-defaults/wg/${t.wg}`).at(-1)!.body).toEqual({ identity_id: null }),
    );
  });

  it.each([
    [true, 'Company account of'],
    [false, 'No default'],
  ])('shows readers the choice without a control (default %s)', async (chosen, text) => {
    const { t, hub } = await setup({ member: true });
    company(hub, COMPANY);
    const readOnly = '0190f000-0000-7000-8000-0000000000f9';
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: readOnly, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: readOnly, access: 'read' },
    ]);
    setLog(t.store, t.org, t.world.log!);
    t.client.on(`GET /v1/scopes/${readOnly}/events`, () => ({ events: [], next_after: null }));
    if (chosen)
      hub.defaults = [{ scope: 'wg', scope_id: readOnly, identity_id: COMPANY, set_by: t.ada.id, set_at: TS }];
    mount(t, wgRoutes as never, `/o/${t.org}/w/${readOnly}/settings`);
    expect(await screen.findByTestId('wg-settings.github.view')).toHaveTextContent(text);
  });
});

describe('the identity of a task', () => {
  it('sends the chosen account with a new task', async () => {
    const { t, hub } = await setup();
    own(hub, OWN, await sealed(t), { primary: true });
    company(hub, COMPANY);
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    t.client.on('POST /v1/tasks', (body) => ({
      task: taskRow(t, { task_id: (body as { task_id: string }).task_id }),
      session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 3000 },
      events: [],
    }));
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    const select = await screen.findByTestId('composer.identity');
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Automatic', 'octo-cat', expect.stringContaining('Company account of')]);
    fireEvent.change(select, { target: { value: COMPANY } });
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Open a PR' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(t.client.of('POST /v1/tasks')).toHaveLength(1));
    expect(t.client.of('POST /v1/tasks')[0]!.body).toMatchObject({ github_identity: COMPANY });
  });

  async function taskPage(row: Partial<TaskRow>, options: { member?: boolean } = {}) {
    const w = await setup(options);
    const { t, hub } = w;
    own(hub, OWN, await sealed(t), { primary: true });
    company(hub, COMPANY);
    const task = taskRow(t, row) as TaskRow;
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
    putTasks(t.store, [task]);
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=chat`);
    await screen.findByTestId('task.screen');
    return w;
  }

  it('shows the identity and lets a writer change and clear it', async () => {
    const { t } = await taskPage({ github_identity: null });
    expect(await screen.findByTestId('task.identity.automatic')).toBeVisible();
    let fail = true;
    t.client.on(`PUT /v1/tasks/${TASK}/github-identity`, () => {
      if (fail) throw new HubError('identity_forbidden', 403);
      return {};
    });
    fireEvent.click(screen.getByTestId('task.identity.change'));
    fireEvent.click(screen.getByTestId('task.identity.cancel'));
    fireEvent.click(screen.getByTestId('task.identity.change'));
    fireEvent.change(await screen.findByTestId('task.identity.select'), { target: { value: OWN } });
    fireEvent.click(screen.getByTestId('task.identity.save'));
    expect(await screen.findByTestId('task.identity.failed')).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByTestId('task.identity.save'));
    await waitFor(() => expect(screen.getByTestId('identities.name')).toHaveTextContent('octo-cat'));
    fireEvent.click(screen.getByTestId('task.identity.change'));
    fireEvent.change(await screen.findByTestId('task.identity.select'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('task.identity.save'));
    expect(await screen.findByTestId('task.identity.automatic')).toBeVisible();
  });

  it('names company accounts and accounts the browser cannot name', async () => {
    const { t } = await taskPage({ github_identity: COMPANY }, { member: true });
    await waitFor(() => expect(screen.getByTestId('identities.name')).toHaveAttribute('data-kind', 'company'));
    expect(screen.getByTestId('identities.name')).toHaveTextContent('a company account of the organization');
    act(() => noteCompanyLogin(t.store, COMPANY, 'acme-bot'));
    expect(screen.getByTestId('identities.name')).toHaveTextContent('acme-bot, a company account of');
    act(() => putTasks(t.store, [{ ...t.store.getState().tasks.tasks[TASK]!, github_identity: SECOND }]));
    expect(screen.getByTestId('identities.name')).toHaveTextContent('another account');
  });
});

describe('operations', () => {
  const row = (t: CredWorld, overrides: Partial<OperationRow>): OperationRow => ({
    operation_id: '0190f000-0000-7000-8000-0000000000b1',
    org_id: t.org,
    wg_id: t.wg,
    task_id: TASK,
    session_id: MAIN,
    device_id: t.device.id,
    actor_id: t.me.id,
    type: 'github.create_pull_request',
    state: 'succeeded',
    error_code: null,
    retryable: false,
    duplicate_of: null,
    attempt: 1,
    params_event_id: null,
    result_event_id: null,
    created_at: '2026-11-21T12:00:01.000Z',
    decided_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  });

  it('says as which account and on whose instruction an operation ran', async () => {
    const { t, hub } = await setup();
    own(hub, OWN, await sealed(t), { primary: true });
    const task = taskRow(t) as TaskRow;
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    const rows = [
      row(t, { github_identity: OWN, triggered_by: t.me.id }),
      row(t, {
        operation_id: 'op-2',
        github_identity: SECOND,
        triggered_by: null,
        created_at: '2026-11-21T12:00:00.000Z',
      }),
      row(t, { operation_id: 'op-3', triggered_by: 'stranger', created_at: '2026-11-21T11:59:00.000Z' }),
      row(t, { operation_id: 'op-4', created_at: '2026-11-21T11:58:00.000Z' }),
    ];
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: rows, next_before: null }));
    putTasks(t.store, [task]);
    putOperations(t.store, rows);
    putEvents(t.store, t.org, [
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 3,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { id: 'm1', kind: 'member.profile', ref: t.me.id, author: t.me.id },
        body: { display_name: 'Ada <b>' },
      },
    ]);
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=operations`);
    await waitFor(() =>
      expect(screen.getAllByTestId('operations.attribution').map((item) => item.textContent)).toEqual([
        'as octo-cat, triggered by Ada <b>',
        'as another account',
        'triggered by an unknown member',
      ]),
    );
  });
});
