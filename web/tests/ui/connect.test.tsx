// `/connect` in jsdom with the simulated hub (11-agent-access.md section 5.1): the fragment read once and removed, the
// request shown with its age, the pairing-tagged gesture, the confirmation body, the form handover in the same tab,
// a cancelled gesture, `pairing_gesture_required` with the same body sent again, other refusals, a lookup that fails,
// the unlock without PRF through the recovery code, a screen left before the lookup answered, and the replacement of
// the current main orchestrator (named after the gesture and asked about once; named before it in an unlocked tab).
import { act, configure, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { preparePrivateOrg } from '../../src/core/account/genesis';
import { signIn } from '../../src/core/account/login';
import { register } from '../../src/core/account/register';
import { fingerprint, sealLabel } from '../../src/core/crypto/agent';
import { Identity } from '../../src/core/crypto/keys';
import { formatTime } from '../../src/core/encoding/time';
import { takeConnectFragment } from '../../src/ui/screens/connect/fragment';
import { FakeAuthenticator, giveRecoveryCode } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

// the passkey flows run real WebCrypto: allow them time on a loaded machine
configure({ asyncUtilTimeout: 10_000 });

const PAIRING = '01a1ee56-5e00-7242-9e24-f87629ad2977';
const OLD = '01a1ee56-5e00-7aaa-8d68-1fafbfc5d917';
const T = '2026-11-23T12:00:00.000Z';
let submitted: HTMLFormElement[] = [];

beforeEach(() => {
  submitted = [];
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function (this: HTMLFormElement) {
    submitted.push(this);
  });
});
afterEach(() => window.history.replaceState(null, '', '/'));

async function fragmentOf(agent: Identity, extra = '') {
  const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
  return `p=${PAIRING}&f=${fp}&c=40123&a=Claude+Code&h=Mac${extra}`;
}

interface Options {
  readonly prf?: boolean;
  readonly lookup?: (body: unknown) => unknown;
  readonly expiresIn?: number;
  readonly signedIn?: boolean;
  /** The user's current main orchestrator: its label sealed to the user (`known`) or to someone else. */
  readonly current?: 'known' | 'unknown';
  /** Further fragment parameters (e.g. the agent's message `t`). */
  readonly extra?: string;
}

async function oldAgent(identity: Identity, userId: string, current: 'known' | 'unknown') {
  const kem = current === 'known' ? identity.kemPublic() : (await Identity.generate()).kemPublic();
  return {
    agent_id: OLD,
    state: 'active',
    label_ct: await sealLabel(kem, OLD, userId, { name: 'Old Agent', host: 'Mac' }),
    created_at: T,
    confirmed_at: T,
    claimed_at: T,
    last_used_at: null,
    revoked_at: null,
    replaces: null,
  };
}

async function connectPage(options: Options = {}) {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  const agent = await Identity.generate();
  const confirmations: unknown[] = [];
  hub.override.set(
    `GET /v1/agent-pairings/${PAIRING}`,
    options.lookup ??
      (() => ({
        pairing_id: PAIRING,
        pub: agent.public(),
        state: 'open',
        gen: 1,
        expires_at: formatTime(Date.now() + (options.expiresIn ?? 600_000)),
      })),
  );
  hub.override.set('POST /v1/me/agents', (body) => (confirmations.push(body), { agent_id: PAIRING }));
  const agents: unknown[] = [];
  hub.override.set('GET /v1/me/agents', () => ({ agents }));
  let recoveryCode = '';
  window.history.replaceState(null, '', `/connect#${await fragmentOf(agent, options.extra)}`);
  const harness = await renderApp('/connect', { hub, auth }, async (made) => {
    const invite = hub.addInvite();
    const registration = await register(made.app.anon, auth, invite);
    recoveryCode = await giveRecoveryCode(hub, registration);
    if (options.current !== undefined) {
      agents.push(await oldAgent(registration.identity, registration.userId, options.current));
    }
    const login = await signIn({ anon: made.app.anon, credentials: auth, origin: ORIGIN });
    await made.app.begin(login);
    await made.app.unlock(registration.identity);
    await made.app.uploadPasskeySlot(login, registration.identity);
    const prepared = await preparePrivateOrg({
      userId: registration.userId,
      identity: registration.identity,
      displayName: 'Ada',
      locale: 'en',
      platformInviteId: await hub.idOf(invite),
    });
    await made.app.client.request('POST', '/v1/orgs', { body: prepared.body });
    await made.app.installOrg(prepared);
    if (!options.signedIn) await made.app.signOut();
    auth.prf = options.prf ?? true;
  });
  return { ...harness, agent, confirmations, recoveryCode };
}

describe('ConnectScreen', () => {
  it('shows the request, confirms with one gesture and hands over in the same tab', async () => {
    const { hub, confirmations, app } = await connectPage({ expiresIn: 420_000 });
    expect(window.location.hash).toBe('');
    expect(await screen.findByTestId('connect.name')).toHaveTextContent('Claude Code');
    expect(screen.getByTestId('connect.host')).toHaveTextContent('Mac');
    expect(screen.getByTestId('connect.age')).toHaveTextContent('Requested 3 min ago');
    expect(screen.getByTestId('connect.main')).toHaveTextContent('It becomes your main orchestrator.');
    expect(screen.queryByTestId('connect.replaces')).toBeNull();
    expect(screen.queryByTestId('connect.message')).toBeNull();
    fireEvent.click(screen.getByTestId('connect.confirm'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(screen.getByTestId('connect.handover')).toBeInTheDocument();
    const form = submitted[0]!;
    expect(form.method).toBe('post');
    expect(form.action).toBe('http://127.0.0.1:40123/agent-pairing');
    expect([...new FormData(form).keys()]).toEqual(['p', 'u', 'r', 'k']);
    expect(new FormData(form).get('k')).toBe(app.rootKey);
    expect(new FormData(form).get('u')).toBe(app.userId);
    expect(confirmations).toHaveLength(1);
    const begin = hub.log.filter((entry) => entry.target === '/v1/passkeys/login/begin').at(-1)!;
    expect(JSON.parse(begin.body)).toEqual({ pairing_id: PAIRING });
  });

  it("quotes the agent's personal message as plain text (E-28)", async () => {
    const text = '<a href="https://evil.example">Klick</a> **bold**';
    await connectPage({ extra: `&t=${encodeURIComponent(text)}` });
    const quote = await screen.findByTestId('connect.message.text');
    expect(quote).toHaveTextContent(text);
    expect(quote.querySelector('a')).toBeNull();
    expect(screen.getByTestId('connect.message')).toHaveTextContent('Personal message of the agent');
  });

  it('offers the gesture again after a cancel and after pairing_gesture_required, with the same body', async () => {
    const { hub, auth, confirmations } = await connectPage();
    await screen.findByTestId('connect.confirm');
    expect(screen.getByTestId('connect.age')).toHaveTextContent('Requested just now');
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.notice')).toHaveTextContent('Not confirmed.');
    let refused = false;
    hub.override.set('POST /v1/me/agents', (body) => {
      confirmations.push(body);
      if (!refused) {
        refused = true;
        throw new HubFailure(403, 'pairing_gesture_required');
      }
      return { agent_id: PAIRING };
    });
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.notice')).toHaveTextContent('The confirmation took too long.');
    fireEvent.click(screen.getByTestId('connect.confirm'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(confirmations).toHaveLength(2);
    // the fixed values are the same (a lost answer is answered alike); the batches are built from the logs each time
    const fixed = (body: unknown) => {
      const { batches: _b, cred_wraps: _c, ...rest } = body as Record<string, unknown>;
      return rest;
    };
    expect(fixed(confirmations[1])).toEqual(fixed(confirmations[0]));
    expect(Object.keys(fixed(confirmations[0])).sort()).toEqual(
      ['cert', 'claim_hash', 'gen', 'label_ct', 'pairing_id', 'user_ct'].sort(),
    );
  });

  it('shows refusals of the confirmation and of the sign-in', async () => {
    const { hub } = await connectPage();
    await screen.findByTestId('connect.confirm');
    hub.override.set('POST /v1/me/agents', new HubFailure(404, 'pairing_unknown'));
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_unknown');
    expect(screen.getByTestId('connect.error')).toHaveTextContent('Connection request expired');
    expect(screen.queryByTestId('connect.request')).toBeNull();
  });

  it('shows a refused sign-in', async () => {
    const { hub } = await connectPage();
    await screen.findByTestId('connect.confirm');
    hub.override.set('POST /v1/passkeys/login/begin', new HubFailure(404, 'pairing_unknown'));
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_unknown');
  });

  it('unlocks with the recovery code without PRF, then confirms', async () => {
    const { recoveryCode } = await connectPage({ prf: false });
    fireEvent.click(await screen.findByTestId('connect.confirm'));
    fireEvent.change(await screen.findByTestId('prf-missing.recovery.input'), { target: { value: recoveryCode } });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(submitted).toHaveLength(1));
  });

  it('keeps the unlocked identity of the same user', async () => {
    const { hub } = await connectPage({ signedIn: true });
    fireEvent.click(await screen.findByTestId('connect.confirm'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(hub.log.filter((entry) => entry.target === '/v1/me').length).toBeGreaterThan(0);
  });

  it('falls back to the recovery code when the unlock refuses the identity', async () => {
    const { hub, app } = await connectPage();
    await screen.findByTestId('connect.confirm');
    hub.override.set('GET /v1/me', () => ({ user_id: app.userId, identity: { kem_pub: 'x', sig_pub: 'y' } }));
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('prf-missing.page')).toBeInTheDocument();
  });

  it('shows another unlock failure', async () => {
    const { hub } = await connectPage();
    await screen.findByTestId('connect.confirm');
    hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'unavailable');
  });

  it('names the main orchestrator it replaces and asks once more before confirming', async () => {
    const { confirmations } = await connectPage({ current: 'known' });
    fireEvent.click(await screen.findByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.replace')).toHaveTextContent('You already have a main orchestrator.');
    expect(screen.getByTestId('connect.replaces')).toHaveTextContent(
      'Replaces “Old Agent”, your current main orchestrator. “Old Agent” is disconnected at once.',
    );
    expect(screen.getByTestId('connect.replaces')).toHaveAttribute('data-agent', OLD);
    expect(confirmations).toHaveLength(0);
    fireEvent.click(screen.getByTestId('connect.replace.confirm'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(confirmations).toHaveLength(1);
  });

  it('cancels the replacement; a second gesture after it was shown confirms at once', async () => {
    const { confirmations } = await connectPage({ current: 'known' });
    fireEvent.click(await screen.findByTestId('connect.confirm'));
    fireEvent.click(await screen.findByTestId('connect.replace.cancel'));
    expect(await screen.findByTestId('connect.notice')).toHaveTextContent('Not confirmed.');
    expect(screen.getByTestId('connect.replaces')).toHaveTextContent('Replaces “Old Agent”');
    expect(confirmations).toHaveLength(0);
    fireEvent.click(screen.getByTestId('connect.confirm'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(screen.queryByTestId('connect.replace')).toBeNull();
  });

  it('names the replaced agent before the gesture in an unlocked tab, an unknown one neutrally', async () => {
    const { confirmations } = await connectPage({ signedIn: true, current: 'unknown' });
    expect(await screen.findByTestId('connect.replaces')).toHaveTextContent(
      'Replaces your current main orchestrator (an unknown agent).',
    );
    fireEvent.click(screen.getByTestId('connect.confirm'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(confirmations).toHaveLength(1);
  });

  it('does not confirm when the agents do not load', async () => {
    const { hub, confirmations } = await connectPage();
    await screen.findByTestId('connect.confirm');
    hub.override.set('GET /v1/me/agents', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'unavailable');
    expect(confirmations).toHaveLength(0);
  });

  it('shows a malformed link', async () => {
    window.history.replaceState(null, '', '/connect#p=x');
    await renderApp('/connect');
    expect(await screen.findByTestId('connect.invalid')).toHaveTextContent('This link is not valid');
  });

  it('refuses keys that do not match the link', async () => {
    const other = await Identity.generate();
    await connectPage({
      lookup: () => ({
        pairing_id: PAIRING,
        pub: other.public(),
        state: 'open',
        gen: 1,
        expires_at: formatTime(Date.now()),
      }),
    });
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_mismatch');
  });

  it('ignores a lookup that answers after the screen was left', async () => {
    let answer: (value: unknown) => void = () => undefined;
    let fail: (error: unknown) => void = () => undefined;
    let asked = 0;
    const pending = () => new Promise((resolve, reject) => ((asked += 1), (answer = resolve), (fail = reject)));
    const one = await connectPage({ lookup: pending });
    one.view.unmount();
    // the request reaches the hub only after the screen was left
    await waitFor(() => expect(asked).toBe(1));
    const open = {
      pairing_id: PAIRING,
      pub: one.agent.public(),
      state: 'open',
      gen: 1,
      expires_at: formatTime(Date.now()),
    };
    await act(async () => answer(open));
    const two = await connectPage({ lookup: pending });
    two.view.unmount();
    await waitFor(() => expect(asked).toBe(2));
    await act(async () => fail(new HubFailure(503, 'unavailable')));
    expect(screen.queryByTestId('connect.error')).toBeNull();
  });
});

describe('takeConnectFragment', () => {
  it('returns the taken text again for the same history entry only', () => {
    const replaced: string[] = [];
    const win = (hash: string, state: unknown) => ({
      location: { hash, pathname: '/connect', search: '?x=1' },
      history: { state, replaceState: (_s: unknown, _t: string, url: string) => void replaced.push(url) },
    });
    expect(takeConnectFragment(win('#abc', { key: 'k1' }) as never)).toBe('abc');
    expect(replaced).toEqual(['/connect?x=1']);
    expect(takeConnectFragment(win('', { key: 'k1' }) as never)).toBe('abc');
    expect(takeConnectFragment(win('', { key: 'k2' }) as never)).toBe('');
    expect(takeConnectFragment(win('#', null) as never)).toBe('');
  });
});
