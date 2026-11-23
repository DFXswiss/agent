// Settings section "GitHub" in jsdom (06-web.md section 8.1): states, the connect flow with sealed device responses
// (device code with copy, link and countdown, done, failed, unverifiable), device choice, disconnect with the link
// to the GitHub settings, and the composer chip that offers "Connect GitHub" only while not connected.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bodyJson } from '../../src/core/encoding/json';
import { newHeader, sealCredential } from '../../src/core/crypto/credential';
import { sealResponse } from '../../src/core/crypto/request';
import { HubError } from '../../src/core/net/signedFetch';
import { setProfile } from '../../src/core/store/slices/me';
import { setLog } from '../../src/core/store/slices/orgs';
import { noteResponse } from '../../src/core/store/slices/requests';
import { setGithub } from '../../src/core/store/slices/github';
import { Composer } from '../../src/ui/composer/Composer';
import { section, GithubSection } from '../../src/ui/settings/sections/github';
import { remaining } from '../../src/ui/settings/sections/github/ConnectFlow';
import { actor, type Actor } from '../core/log/world';
import { taskWorld, type TaskWorld } from '../core/tasks/world';
import { mount } from './chat-world';
import { labelDevice } from './device-labels';

type World = TaskWorld & { device: Actor };

async function world(): Promise<World> {
  const t = await taskWorld();
  const device = await actor('ui-gh-device');
  await t.world.admitDevice(t.me, device, [t.wg]);
  setLog(t.store, t.org, t.world.log!);
  setProfile(t.store, { orgs: [{ org_id: t.org, role: 'admin', private: true, default_wg: t.wg }] });
  t.client.on('GET /v1/wraps', () => ({ wraps: [t.credWrap] }));
  t.client.on('GET /v1/me/credentials', () => ({ credentials: [] }));
  t.client.on('GET /v1/me/devices', () => ({
    devices: [{ device_id: device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null }],
  }));
  return { ...t, device };
}

async function record(t: World) {
  const header = newHeader({
    id: '0190f000-0000-7000-8000-0000000000c1',
    owner: t.me.id,
    kind: 'github',
    version: 1,
    epoch: 1,
    author: t.device.id,
    ts: '2026-11-21T12:00:00.000Z',
  });
  const body = { token: 'gho_x', login: 'octo-cat', user_id: 1, scopes: [], created_at: '2026-11-21T12:00:00.000Z' };
  return sealCredential(t.credKey, header, bodyJson(body), t.device.identity.sig.privateKey);
}

function connected(t: World, found: unknown) {
  t.client.on('GET /v1/me/credentials', () => ({
    credentials: [{ kind: 'github', version: 1, epoch: 1, updated_at: 'x' }],
  }));
  t.client.on('GET /v1/me/credentials/github', () => ({ record: found }));
}

/** Serves the request the flow posts, with the responses `bodies` (sealed by the device). */
function deviceAnswers(t: World) {
  const state = { id: '', bodies: [] as { body: unknown; final: boolean }[], hubState: 'delivered' };
  t.client.on('POST /v1/device-requests', (body) => {
    state.id = (body as { envelope: { header: { id: string } } }).envelope.header.id;
    t.client.on(`GET /v1/device-requests/${state.id}`, view);
    return { request_id: state.id, state: 'pending' };
  });
  const view = async (_body: unknown, target: string) => {
    const responses = [];
    for (const [index, item] of state.bodies.entries()) {
      const header = {
        v: 1,
        id: `0190f000-0000-7000-8000-00000000f0${index}0`,
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
    expect(target).toBe(`/v1/device-requests/${state.id}`);
    return {
      request_id: state.id,
      kind: 'github.connect',
      device_id: t.device.id,
      state: state.hubState,
      created_at: '2026-11-21T12:00:00.000Z',
      expires_at: '2026-11-21T12:15:00.000Z',
      responses,
    };
  };
  const respond = async (body: unknown, final = false) => {
    state.bodies.push({ body, final });
    await act(async () => noteResponse(t.store, state.id, state.bodies.length));
  };
  return { state, respond };
}

const page = (t: World) => mount(t, [{ path: '/settings', element: <GithubSection /> }], '/settings');

afterEach(() => vi.useRealTimers());

describe('GitHub section', () => {
  it('is registered as a settings section', () => {
    expect(section).toMatchObject({ id: 'github', titleKey: 'github.title' });
    expect(remaining(61_000)).toBe('1:01');
    expect(remaining(-5)).toBe('0:00');
  });

  it('shows a connected account with its login and disconnects with the GitHub settings link', async () => {
    const t = await world();
    connected(t, await record(t));
    t.client.on('DELETE /v1/me/credentials/github', () => ({}));
    page(t);
    expect(await screen.findByTestId('github.connected')).toHaveTextContent('Connected as octo-cat');
    expect(screen.queryByTestId('github.connect')).toBeNull();
    fireEvent.click(screen.getByTestId('github.disconnect'));
    fireEvent.click(screen.getByTestId('github.disconnect.cancel'));
    expect(screen.queryByTestId('github.disconnect.dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('github.disconnect'));
    fireEvent.keyDown(screen.getByTestId('github.disconnect.dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('github.disconnect.dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('github.disconnect'));
    fireEvent.click(screen.getByTestId('github.disconnect.confirm'));
    expect(await screen.findByTestId('github.disconnected')).toBeVisible();
    expect(screen.getByTestId('github.settings-link')).toHaveAttribute(
      'href',
      'https://github.com/settings/applications',
    );
    expect(screen.getByTestId('github.settings-link')).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('reports a failed disconnect in the dialog', async () => {
    const t = await world();
    connected(t, await record(t));
    t.client.on('DELETE /v1/me/credentials/github', () => {
      throw new HubError('unavailable', 503);
    });
    page(t);
    fireEvent.click(await screen.findByTestId('github.disconnect'));
    fireEvent.click(screen.getByTestId('github.disconnect.confirm'));
    expect(await screen.findByTestId('github.disconnect.failed')).toHaveTextContent('Service unavailable');
  });

  it('shows an unverifiable record and offers both actions', async () => {
    const t = await world();
    connected(t, 'junk');
    page(t);
    expect(await screen.findByTestId('github.unverifiable')).toHaveTextContent('header_invalid');
    expect(screen.getByTestId('github.connect')).toBeVisible();
    expect(screen.getByTestId('github.disconnect')).toBeVisible();
  });

  it('reports a failed load and loads again', async () => {
    const t = await world();
    let fail = true;
    t.client.on('GET /v1/me/credentials', () => {
      if (fail) throw new HubError('unavailable', 503);
      return { credentials: [] };
    });
    page(t);
    expect(await screen.findByTestId('github.load-failed')).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByTestId('github.reload'));
    expect(await screen.findByTestId('github.disconnected')).toBeVisible();
  });

  it('connects through the device code flow', async () => {
    const t = await world();
    const answers = deviceAnswers(t);
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    labelDevice(t.store, t.org, t.device.id, 'Build box');
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    const select = await screen.findByTestId('github.devices.select');
    expect(select).toHaveValue(t.device.id);
    // the device by its label (KOR-05, B-23)
    expect(within(select).getByRole('option')).toHaveTextContent('Build box – default workgroup');
    fireEvent.change(select, { target: { value: t.device.id } });
    fireEvent.click(screen.getByTestId('github.start'));
    expect(await screen.findByTestId('github.waiting')).toBeVisible();
    t.clock.now = Date.now();
    await answers.respond({
      step: 'device_code',
      user_code: 'WDJB-MJHT',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
    });
    expect(await screen.findByTestId('github.code.value')).toHaveTextContent('WDJB-MJHT');
    expect(screen.getByTestId('github.code.open')).toHaveAttribute('href', 'https://github.com/login/device');
    expect(screen.getByTestId('github.code.open')).toHaveAttribute('target', '_blank');
    expect(screen.getByTestId('github.code.expires')).toHaveTextContent(/The code expires in 1[45]:\d\d\./);
    fireEvent.click(screen.getByTestId('github.code.copy'));
    expect(await screen.findByText('Copied')).toBeVisible();
    expect(writeText).toHaveBeenCalledWith('WDJB-MJHT');
    connected(t, await record(t));
    await answers.respond({ step: 'done', login: 'octo-cat' }, true);
    expect(await screen.findByTestId('github.connected')).toHaveTextContent('Connected as octo-cat');
    expect(screen.queryByTestId('github.flow')).toBeNull();
  });

  it('shows an expired code, a failed copy and a failure with retry', async () => {
    const t = await world();
    const answers = deviceAnswers(t);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn(async () => Promise.reject(new Error('denied'))) },
      configurable: true,
    });
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    fireEvent.click(await screen.findByTestId('github.start'));
    t.clock.now = Date.now() - 2_000_000;
    await answers.respond({
      step: 'device_code',
      user_code: 'WDJB-MJHT',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
    });
    expect(await screen.findByTestId('github.code.expires')).toHaveTextContent('The code has expired.');
    fireEvent.click(screen.getByTestId('github.code.copy'));
    await waitFor(() => expect(screen.getByTestId('github.code.copy')).toHaveTextContent('Copy code'));
    await answers.respond({ step: 'failed', code: 'timeout' }, true);
    expect(await screen.findByTestId('github.failed')).toHaveTextContent('The request timed out.');
    fireEvent.click(screen.getByTestId('github.retry'));
    expect(await screen.findByTestId('github.devices.select')).toBeVisible();
    fireEvent.click(screen.getByTestId('github.flow.close'));
    expect(screen.queryByTestId('github.flow')).toBeNull();
  });

  it('names hub failures of the request with the error catalog', async () => {
    const t = await world();
    const answers = deviceAnswers(t);
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    fireEvent.click(await screen.findByTestId('github.start'));
    answers.state.hubState = 'failed';
    await answers.respond({ junk: true });
    expect(await screen.findByTestId('github.failed')).toHaveAttribute('data-code', 'request_failed');
    expect(screen.getByTestId('github.failed')).toHaveTextContent('a newer request of the same kind replaced it');
  });

  it('says when no device is online, and when none can be prepared or the request is refused', async () => {
    const t = await world();
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        { device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: false, last_seen_at: null },
      ],
    }));
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    expect(await screen.findByTestId('github.devices.none')).toBeVisible();
    t.client.on('GET /v1/me/devices', () => {
      throw new HubError('rate_limited', 429);
    });
    fireEvent.click(screen.getByTestId('github.retry'));
    expect(await screen.findByTestId('github.failed')).toHaveAttribute('data-code', 'rate_limited');
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        { device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null },
        {
          device_id: t.devices[0]!.id,
          kind: 'platform',
          org_id: t.org,
          wg_ids: [t.wg],
          online: true,
          last_seen_at: null,
        },
      ],
    }));
    t.client.on('POST /v1/device-requests', () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('github.retry'));
    fireEvent.click(await screen.findByTestId('github.start'));
    expect(await screen.findByTestId('github.failed')).toHaveAttribute('data-code', 'forbidden');
  });

  it('lists offline devices as disabled options', async () => {
    const t = await world();
    const other = await actor('ui-gh-other');
    await t.world.admitDevice(t.me, other, [t.wg]);
    setLog(t.store, t.org, t.world.log!);
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        { device_id: t.device.id, kind: 'org', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null },
        { device_id: other.id, kind: 'org', org_id: 'elsewhere', wg_ids: ['w'], online: false, last_seen_at: null },
      ],
    }));
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    const options = within(await screen.findByTestId('github.devices.select')).getAllByRole('option');
    expect(options[1]).toBeDisabled();
    // without a label read: the neutral name with the end of the ID
    expect(options[1]).toHaveTextContent(`Device …${other.id.slice(-6)} (offline)`);
  });
});

describe('failed status reads', () => {
  it('says so while waiting and keeps trying', async () => {
    const t = await world();
    t.client.on('POST /v1/device-requests', (body) => {
      const id = (body as { envelope: { header: { id: string } } }).envelope.header.id;
      t.client.on(`GET /v1/device-requests/${id}`, () => {
        throw new HubError('not_found', 404);
      });
      return { request_id: id, state: 'pending' };
    });
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    fireEvent.click(await screen.findByTestId('github.start'));
    expect(await screen.findByTestId('github.read-failed')).toHaveAttribute('data-code', 'not_found');
  });
});

describe('closing the flow early', () => {
  it('drops the device list of a flow that was closed meanwhile', async () => {
    const t = await world();
    let release: (value: unknown) => void = () => undefined;
    t.client.on('GET /v1/me/devices', () => new Promise((resolve) => (release = resolve)));
    page(t);
    fireEvent.click(await screen.findByTestId('github.connect'));
    fireEvent.click(await screen.findByTestId('github.flow.close'));
    await act(async () => release({ devices: [] }));
    expect(screen.queryByTestId('github.flow')).toBeNull();
  });
});

describe('composer GitHub chip', () => {
  it('offers Connect GitHub while not connected and loads the state once', async () => {
    const t = await world();
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} suggestions /> }], '/');
    fireEvent.click(screen.getByTestId('composer.chip.github'));
    expect(screen.getByTestId('composer.github-hint')).toBeVisible();
    await waitFor(() => expect(t.store.getState().github.status).toBe('disconnected'));
    expect(screen.getByTestId('composer.github-hint')).toBeVisible();
    fireEvent.click(screen.getByTestId('composer.chip.github'));
    expect(t.client.of('GET /v1/me/credentials')).toHaveLength(1);
    act(() => setGithub(t.store, { status: 'connected', login: 'octo', createdAt: 'x', version: 1 }));
    expect(screen.queryByTestId('composer.github-hint')).toBeNull();
  });

  it('keeps the hint when the state cannot be loaded', async () => {
    const t = await world();
    t.client.on('GET /v1/me/credentials', () => {
      throw new HubError('unavailable', 503);
    });
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} suggestions /> }], '/');
    fireEvent.click(screen.getByTestId('composer.chip.github'));
    await waitFor(() => expect(t.client.of('GET /v1/me/credentials')).toHaveLength(1));
    expect(screen.getByTestId('composer.github-hint')).toBeVisible();
  });
});
