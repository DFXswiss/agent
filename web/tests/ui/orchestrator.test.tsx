// U-WEB-ui (11-agent-access.md section 11, ADR-082 sections 6 and 9.6-9.9): "Make this session my main orchestrator"
// only for the task owner's running main session that the hub admits; the dialog names session, device and the
// replaced orchestrator, says what the session gets, waits for the session's keys, checks the device's signature under
// the device key of the verified platform log, runs the pairing-tagged ceremony and the confirmation, then sends the
// signed handover and waits for the claim; refusals and retries; the settings list names the session on DFX hardware.
import { act, configure, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CeremonyCancelled } from '../../src/core/account/ceremony';
import {
  fingerprint,
  orchestratorHandoverText,
  orchestratorKeyText,
  sealLabel,
  signText,
  verifyText,
} from '../../src/core/crypto/agent';
import { Identity } from '../../src/core/crypto/keys';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { putSession, putTasks, type SessionEntry } from '../../src/core/store/slices/tasks';
import { I18nProvider } from '../../src/ui/i18n';
import { OrchestratorAction } from '../../src/ui/screens/session/OrchestratorAction';
import { AgentsSection } from '../../src/ui/settings/sections/agents';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN } from './chat-world';

configure({ asyncUtilTimeout: 10_000 });

const mocks = vi.hoisted(() => ({
  signIn: vi.fn(),
  unlockSignIn: vi.fn(),
  confirmPairing: vi.fn(),
  pairingGen: vi.fn(),
}));
vi.mock('../../src/core/account/login', () => ({ signIn: mocks.signIn }));
vi.mock('../../src/ui/prf-missing/device', () => ({ unlockSignIn: mocks.unlockSignIn }));
vi.mock('../../src/core/agents/pairing', async (original) => ({
  ...(await original<typeof import('../../src/core/agents/pairing')>()),
  confirmPairing: mocks.confirmPairing,
  pairingGen: mocks.pairingGen,
}));

const PATH = `/v1/sessions/${MAIN}/orchestrator`;
const OLD = '01a1ee56-5e00-7aaa-8d68-1fafbfc5d917';
const T = '2026-11-21T12:00:00.000Z';

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.signIn.mockResolvedValue({ userId: 'u', keyId: 'session.y' });
  mocks.pairingGen.mockResolvedValue(2);
  mocks.confirmPairing.mockResolvedValue(undefined);
});

interface Scene {
  readonly t: TaskWorld;
  readonly agent: Identity;
  readonly pairing: string;
  readonly view: Record<string, unknown>;
  readonly entry: SessionEntry;
  readonly app: Record<string, unknown>;
  readonly begin: ReturnType<typeof vi.fn>;
  readonly unlock: ReturnType<typeof vi.fn>;
}

async function scene(
  options: { state?: string; owner?: string; signer?: Identity; row?: Record<string, unknown> } = {},
): Promise<Scene> {
  const t = await taskWorld();
  const device = t.devices[0]!;
  const agent = await Identity.generate();
  const pairing = '01a1ee56-5e00-7242-9e24-f87629ad2977';
  const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
  const signer = options.signer ?? device.identity;
  const deviceSig = await signText(signer.sig.privateKey, orchestratorKeyText(fp, pairing, MAIN));
  const row = sessionRow(t, { device_id: device.id, state: options.state ?? 'working', ...options.row });
  putTasks(t.store, [taskRow(t, { owner_id: options.owner ?? t.me.id }) as never]);
  putSession(t.store, row as never, [], 0);
  const entry = t.store.getState().tasks.sessions[MAIN]!;
  const view = {
    session_id: MAIN,
    device_id: device.id,
    available: true,
    reason: null,
    requested_at: T,
    pairing: {
      pairing_id: pairing,
      pub: agent.public(),
      fp,
      device_sig: deviceSig,
      state: 'open',
      expires_at: '2026-11-21T12:10:00.000Z',
      handover: false,
    },
  };
  const begin = vi.fn(async () => undefined);
  const unlock = vi.fn(async () => undefined);
  const app = {
    store: t.store,
    tasks: t.context,
    client: t.client,
    anon: vi.fn(),
    userId: t.me.id,
    rootKey: 'A'.repeat(43),
    prefs: undefined,
    begin,
    unlock,
  };
  return { t, agent, pairing, view, entry, app, begin, unlock };
}

function mount(s: Scene, element: ReactNode, locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter([{ path: '/', element }], { initialEntries: ['/'] });
  render(
    <AppEnvProvider value={{ app: s.app as never, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n(locale)}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
}

async function agentRow(s: Scene, id: string, state: string, extra: Record<string, unknown> = {}) {
  return {
    agent_id: id,
    state,
    label_ct: await sealLabel(s.t.me.identity.kemPublic(), id, s.t.me.id, { name: 'Old Agent', host: 'Mac' }),
    created_at: T,
    confirmed_at: T,
    claimed_at: T,
    last_used_at: null,
    revoked_at: null,
    replaces: null,
    ...extra,
  };
}

/** The hub of the dialog: request, view, handover, agent list. */
function hub(s: Scene, agents: unknown[] = []) {
  const handovers: { ct: string; sig: string }[] = [];
  s.t.client.on(`GET ${PATH}`, () => s.view);
  s.t.client.on(`POST ${PATH}`, () => ({ ...s.view, pairing: null }));
  s.t.client.on(`POST /v1/agent-pairings/${s.pairing}/orchestrator-handover`, (body) => {
    handovers.push(body as { ct: string; sig: string });
    return { pairing_id: s.pairing, session_id: MAIN };
  });
  s.t.client.on('GET /v1/me/agents', () => ({ agents }));
  return handovers;
}

async function openDialog() {
  fireEvent.click(await screen.findByTestId('orchestrator.make'));
  await waitFor(() => expect(screen.getByTestId('orchestrator.confirm')).toBeEnabled());
}

describe('OrchestratorAction', () => {
  it('is offered for a running main session of the owner that the hub admits', async () => {
    const s = await scene();
    hub(s);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    expect(await screen.findByTestId('orchestrator.make')).toHaveTextContent('Make this session my main orchestrator');
    expect(s.t.client.of(`GET ${PATH}`)).toHaveLength(1);
  });

  it('is offered in German', async () => {
    const s = await scene();
    hub(s);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />, 'de');
    expect(await screen.findByTestId('orchestrator.make')).toHaveTextContent('Diese Session als Hauptorchestrator');
  });

  it('is not offered to another user, for a subagent, a session that does not run or one the hub refuses', async () => {
    for (const [options, depth] of [
      [{ owner: '01a1ee56-5e00-7000-8000-000000000999' }, 0],
      [{}, 1],
      [{ state: 'suspended' }, 0],
    ] as const) {
      const s = await scene(options);
      hub(s);
      mount(s, <OrchestratorAction entry={s.entry} depth={depth} />);
      await act(async () => undefined);
      expect(screen.queryByTestId('orchestrator.make')).toBeNull();
      expect(s.t.client.of(`GET ${PATH}`)).toHaveLength(0);
      document.body.innerHTML = '';
    }
    const refused = await scene();
    refused.t.client.on(`GET ${PATH}`, () => ({ ...refused.view, available: false, reason: 'not_private' }));
    mount(refused, <OrchestratorAction entry={refused.entry} depth={0} />);
    await waitFor(() => expect(refused.t.client.of(`GET ${PATH}`)).toHaveLength(1));
    expect(screen.queryByTestId('orchestrator.make')).toBeNull();
    document.body.innerHTML = '';
    const failing = await scene();
    failing.t.client.on(`GET ${PATH}`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(failing, <OrchestratorAction entry={failing.entry} depth={0} />);
    await waitFor(() => expect(failing.t.client.of(`GET ${PATH}`)).toHaveLength(1));
    expect(screen.queryByTestId('orchestrator.make')).toBeNull();
  });

  it('says so when the session is the main orchestrator already', async () => {
    const s = await scene();
    s.t.client.on(`GET ${PATH}`, () => ({
      ...s.view,
      pairing: { ...(s.view.pairing as object), state: 'active' },
    }));
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    expect(await screen.findByTestId('orchestrator.current')).toHaveTextContent(
      'This session is your main orchestrator.',
    );
  });
});

describe('OrchestratorDialog', { timeout: 20_000 }, () => {
  it('names session, device and the replaced orchestrator, confirms and hands over signed', async () => {
    const s = await scene();
    // the hub's list after the confirmation: the session's pairing waits for its claim
    const agents: unknown[] = [await agentRow(s, OLD, 'active'), await agentRow(s, s.pairing, 'confirmed')];
    const handovers = hub(s, agents);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    await openDialog();
    expect(s.t.client.of(`POST ${PATH}`)).toHaveLength(1);
    expect(screen.getByTestId('orchestrator.session')).toHaveTextContent('This session');
    expect(screen.getByTestId('orchestrator.device')).toBeInTheDocument();
    expect(screen.getByTestId('orchestrator.access')).toHaveTextContent('full access');
    expect(screen.getByTestId('orchestrator.key')).toHaveTextContent('never leaves it');
    expect(await screen.findByTestId('orchestrator.replaces')).toHaveTextContent('Replaces “Old Agent”');
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    await screen.findByTestId('orchestrator.outcome.waiting');
    expect(mocks.signIn).toHaveBeenCalledWith(expect.objectContaining({ pairingId: s.pairing }));
    expect(s.begin).toHaveBeenCalled();
    expect(mocks.unlockSignIn).not.toHaveBeenCalled();
    const prepared = mocks.confirmPairing.mock.calls[0]![1];
    expect(prepared.pairingId).toBe(s.pairing);
    expect(prepared.agent.gen).toBe(2);
    expect(prepared.body).not.toHaveProperty('handover_ct');
    const { ct, sig } = handovers[0]!;
    await verifyText(s.t.me.identity.sigPublic(), sig, orchestratorHandoverText(ct, s.pairing, MAIN));
    // the session claims: the list says connected
    agents[1] = await agentRow(s, s.pairing, 'active', { session_id: MAIN });
    expect(await screen.findByTestId('orchestrator.outcome.connected')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('orchestrator.close'));
    await waitFor(() => expect(screen.queryByTestId('orchestrator.dialog')).toBeNull());
  });

  it('names a session without a device and a replaced orchestrator whose label does not open', async () => {
    const s = await scene();
    const other = await Identity.generate();
    const unreadable = await sealLabel(other.kemPublic(), OLD, s.t.me.id, { name: 'Hidden', host: 'Mac' });
    hub(s, [await agentRow(s, OLD, 'active', { label_ct: unreadable })]);
    const entry = { ...s.entry, row: { ...s.entry.row, device_id: null } } as SessionEntry;
    mount(s, <OrchestratorAction entry={entry} depth={0} />);
    fireEvent.click(await screen.findByTestId('orchestrator.make'));
    expect(screen.getByTestId('orchestrator.device')).toHaveTextContent('No device');
    expect(await screen.findByTestId('orchestrator.replaces')).toHaveTextContent(
      'Replaces your current main orchestrator (an unknown agent).',
    );
    await waitFor(() => expect(screen.getByTestId('orchestrator.confirm')).toBeEnabled());
  });

  it('reports a session that did not claim', async () => {
    const s = await scene();
    const agents: unknown[] = [];
    hub(s, agents);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    await openDialog();
    expect(screen.queryByTestId('orchestrator.replaces')).toBeNull();
    agents.push(await agentRow(s, s.pairing, 'revoked'));
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    expect(await screen.findByTestId('orchestrator.outcome.failed')).toBeInTheDocument();
  });

  it('unlocks the identity after the ceremony, or names the missing PRF', async () => {
    const s = await scene();
    hub(s);
    const held = s.t.me.identity;
    s.unlock.mockImplementation(async () => {
      s.t.store.update('me', (me) => ({ ...me, identity: held }));
    });
    s.t.store.update('me', (me) => ({ ...me, identity: null }));
    mocks.unlockSignIn.mockResolvedValueOnce(null).mockResolvedValueOnce(held);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    await openDialog();
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    expect(await screen.findByTestId('orchestrator.error')).toHaveAttribute('data-code', 'prf_missing');
    fireEvent.click(screen.getByTestId('orchestrator.close'));
    await openDialog();
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    await screen.findByTestId('orchestrator.outcome.waiting');
    expect(s.unlock).toHaveBeenCalledWith(held);
  });

  it('offers the gesture again after a slow or cancelled ceremony with the same confirmation', async () => {
    const s = await scene();
    hub(s);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    await openDialog();
    mocks.confirmPairing.mockRejectedValueOnce(new HubError('pairing_gesture_required', 403));
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    expect(await screen.findByTestId('orchestrator.notice')).toHaveTextContent('took too long');
    mocks.signIn.mockRejectedValueOnce(new CeremonyCancelled());
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    expect(await screen.findByTestId('orchestrator.notice')).toHaveTextContent('Not confirmed');
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    await screen.findByTestId('orchestrator.outcome.waiting');
    expect(mocks.pairingGen).toHaveBeenCalledTimes(1);
    const [first, last] = [mocks.confirmPairing.mock.calls[0]![1], mocks.confirmPairing.mock.calls.at(-1)![1]];
    expect(last).toBe(first);
  });

  it('refuses keys the device did not sign and a session the hub refuses', async () => {
    const s = await scene({ signer: await Identity.generate() });
    hub(s);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    fireEvent.click(await screen.findByTestId('orchestrator.make'));
    expect(await screen.findByTestId('orchestrator.error')).toHaveAttribute('data-code', 'pairing_mismatch');
    expect(screen.queryByTestId('orchestrator.confirm')).toBeNull();
    expect(mocks.signIn).not.toHaveBeenCalled();
    document.body.innerHTML = '';
    const refused = await scene();
    hub(refused);
    refused.t.client.on(`POST ${PATH}`, () => {
      throw new HubError('orchestrator_unavailable', 409, { reason: 'not_running' });
    });
    mount(refused, <OrchestratorAction entry={refused.entry} depth={0} />);
    fireEvent.click(await screen.findByTestId('orchestrator.make'));
    const alert = await screen.findByTestId('orchestrator.error');
    expect(alert).toHaveAttribute('data-code', 'orchestrator_unavailable');
    expect(alert).toHaveTextContent('Not possible for this session');
  });

  it('shows a failed handover', async () => {
    const s = await scene();
    hub(s);
    s.t.client.on(`POST /v1/agent-pairings/${s.pairing}/orchestrator-handover`, () => {
      throw new HubError('signature_invalid', 401);
    });
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    await openDialog();
    fireEvent.click(screen.getByTestId('orchestrator.confirm'));
    expect(await screen.findByTestId('orchestrator.error')).toHaveAttribute('data-code', 'signature_invalid');
  });

  it('stops waiting for keys when it is closed', async () => {
    const s = await scene();
    hub(s);
    s.t.client.on(`GET ${PATH}`, () => ({ ...s.view, pairing: null }));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
      fireEvent.click(await screen.findByTestId('orchestrator.make'));
      expect(await screen.findByTestId('orchestrator.preparing')).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('orchestrator.close'));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(screen.queryByTestId('orchestrator.dialog')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('OrchestratorDialog checks', { timeout: 20_000 }, () => {
  it('refuses a session without a device and names no device', async () => {
    const s = await scene({ row: { device_id: null } });
    (s.view as Record<string, unknown>).device_id = null;
    hub(s);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    fireEvent.click(await screen.findByTestId('orchestrator.make'));
    expect(await screen.findByTestId('orchestrator.error')).toHaveAttribute('data-code', 'pairing_mismatch');
    expect(screen.getByTestId('orchestrator.device')).toHaveTextContent('No device');
  });

  it('refuses an organization this browser has not verified as the private one of the user', async () => {
    const s = await scene({ row: { org_id: '01a1ee56-5e00-7000-8000-00000000abcd' } });
    hub(s);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    fireEvent.click(await screen.findByTestId('orchestrator.make'));
    expect(await screen.findByTestId('orchestrator.error')).toHaveAttribute('data-code', 'orchestrator_unavailable');
  });

  it('names an unknown main orchestrator it replaces', async () => {
    const s = await scene();
    const stranger = await Identity.generate();
    const row = await agentRow(s, OLD, 'active');
    hub(s, [{ ...row, label_ct: await sealLabel(stranger.kemPublic(), OLD, s.t.me.id, { name: 'X', host: 'Y' }) }]);
    mount(s, <OrchestratorAction entry={s.entry} depth={0} />);
    await openDialog();
    expect(await screen.findByTestId('orchestrator.replaces')).toHaveTextContent('an unknown agent');
  });
});

describe('the settings list', () => {
  it('names the session on DFX hardware', async () => {
    const s = await scene();
    hub(s, [await agentRow(s, s.pairing, 'active', { session_id: MAIN, own_identity: true })]);
    mount(s, <AgentsSection />);
    const where = await screen.findByTestId('agents.item.session');
    expect(where).toHaveTextContent('On DFX hardware, in a session of one of your tasks');
    expect(where).toHaveAttribute('data-session', MAIN);
    expect(screen.queryByTestId('agents.item.runs')).toBeNull();
  });
});
