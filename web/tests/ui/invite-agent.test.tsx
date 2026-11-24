// The invitation of the user's AI agent (11-agent-access.md section 5.2) in jsdom with the simulated hub: the pairing
// after the code, the agent named on the page, the proposed display name instead of the name field, the
// pairing-tagged sign-in after the new passkey and the confirmation on `/connect` without a further gesture; broken
// links and expired requests; a request that expires before the sign-in; `/connect` with a pairing in the route state.
import { configure, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fingerprint } from '../../src/core/crypto/agent';
import { Identity } from '../../src/core/crypto/keys';
import { formatTime } from '../../src/core/encoding/time';
import { forgetFragment } from '../../src/ui/screens/invite/fragment';
import { FakeAuthenticator } from '../core/account/authenticator';
import { HubFailure, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

// the passkey flows run real WebCrypto: allow them time on a loaded machine
configure({ asyncUtilTimeout: 10_000 });

const PAIRING = '01a1ee56-5e00-7242-9e24-f87629ad2977';
let submitted: HTMLFormElement[] = [];

beforeEach(() => {
  submitted = [];
  vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(function (this: HTMLFormElement) {
    submitted.push(this);
  });
});
afterEach(() => {
  forgetFragment();
  window.history.replaceState(null, '', '/');
});

interface Options {
  /** The `u` parameter; null leaves it out. */
  readonly name?: string | null;
  /** Text appended to the fragment. */
  readonly extra?: string;
  readonly state?: string;
}

async function agentInvite(options: Options = {}) {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  const agent = await Identity.generate();
  const confirmations: unknown[] = [];
  hub.override.set(`GET /v1/agent-pairings/${PAIRING}`, () => ({
    pairing_id: PAIRING,
    pub: agent.public(),
    state: options.state ?? 'open',
    expires_at: formatTime(Date.now() + 600_000),
  }));
  hub.override.set('POST /v1/me/agents', (body) => (confirmations.push(body), { agent_id: PAIRING }));
  hub.override.set('GET /v1/me/agents', () => ({ agents: [] }));
  const code = hub.addInvite();
  const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
  const name = options.name === undefined ? '&u=Ada+Lovelace' : options.name === null ? '' : options.name;
  const fragment = `${code}&p=${PAIRING}&f=${fp}&c=40123&a=Claude+Code&h=Mac${name}${options.extra ?? ''}`;
  window.history.replaceState({ key: 'invite' }, '', `/invite#${fragment}`);
  const harness = await renderApp('/invite', { hub, auth });
  return { ...harness, agent, auth, confirmations };
}

const loginBodies = (hub: SimHub) =>
  hub.log.filter((entry) => entry.target === '/v1/passkeys/login/begin').map((entry) => JSON.parse(entry.body));

describe('InviteScreen with the pairing of an agent', () => {
  it('registers with the proposed name and connects the agent: the user only creates the passkey', async () => {
    const { hub, app, router, confirmations } = await agentInvite();
    expect(await screen.findByTestId('invite.agent')).toBeInTheDocument();
    expect(window.location.hash).toBe('');
    expect(screen.getByTestId('invite.agent.name')).toHaveTextContent('Claude Code');
    expect(screen.getByTestId('invite.agent.host')).toHaveTextContent('Mac');
    expect(screen.getByTestId('invite.agent.warning')).toHaveTextContent('Only confirm if you have just asked');
    expect(screen.queryByTestId('invite.agent.message')).toBeNull();
    expect(screen.getByTestId('invite.proposed-name')).toHaveTextContent('Your display name: Ada Lovelace');
    expect(screen.queryByTestId('invite.display-name')).toBeNull();
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(router.state.location.pathname).toBe('/connect');
    expect(screen.getByTestId('connect.handover')).toBeInTheDocument();
    expect(loginBodies(hub)).toEqual([{ pairing_id: PAIRING }]);
    expect(confirmations).toHaveLength(1);
    expect(new FormData(submitted[0]!).get('u')).toBe(app.userId);
    expect(hub.orgs.size).toBe(1);
  });

  it("quotes the agent's personal message in the agent block, and none without it (E-28)", async () => {
    await agentInvite({ extra: `&t=${encodeURIComponent('Bitte <b>bestätigen</b>: https://example.com')}` });
    expect(await screen.findByTestId('invite.agent.message.text')).toHaveTextContent(
      'Bitte <b>bestätigen</b>: https://example.com',
    );
    expect(screen.getByTestId('invite.agent.message.text').querySelector('b')).toBeNull();
  });

  it('shows the name field without a proposed name', async () => {
    await agentInvite({ name: null });
    expect(await screen.findByTestId('invite.display-name')).toHaveValue('');
    expect(screen.getByTestId('invite.agent')).toBeInTheDocument();
    expect(screen.queryByTestId('invite.proposed-name')).toBeNull();
  });

  it('refuses a broken link and an expired request before anything is created', async () => {
    const broken = await agentInvite({ extra: '&x=1' });
    expect(await screen.findByTestId('invite.invalid')).toHaveAttribute('data-code', 'pairing_mismatch');
    expect(screen.getByTestId('invite.invalid')).toHaveTextContent('This link is not valid');
    expect(broken.hub.users.size).toBe(0);
    broken.view.unmount();
    forgetFragment();
    const expired = await agentInvite({ state: 'confirmed' });
    expect(await screen.findByTestId('invite.invalid')).toHaveAttribute('data-code', 'pairing_unknown');
    expect(expired.hub.users.size).toBe(0);
  });

  it('finishes the account without the agent when its request expired before the sign-in', async () => {
    const { hub, router, confirmations } = await agentInvite();
    fireEvent.click(await screen.findByTestId('invite.create-passkey'));
    hub.override.set('POST /v1/passkeys/login/begin', new HubFailure(404, 'pairing_unknown'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('Connection request expired');
    hub.override.delete('POST /v1/passkeys/login/begin');
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/o\//));
    expect(loginBodies(hub).at(-1)).toEqual({});
    expect(confirmations).toHaveLength(0);
    expect(submitted).toHaveLength(0);
  });

  it('reports a cancelled sign-in and keeps the pairing', async () => {
    const { hub, auth } = await agentInvite();
    fireEvent.click(await screen.findByTestId('invite.create-passkey'));
    await screen.findByTestId('invite.unlock');
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('invite.unlock'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('Sign-in was cancelled');
    auth.refuse = null;
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(loginBodies(hub).at(-1)).toEqual({ pairing_id: PAIRING });
  });
});

describe('ConnectScreen with a pairing in the route state', () => {
  it('waits for the gesture when no identity is unlocked, and ignores a broken or foreign state', async () => {
    const hub = new SimHub();
    const agent = await Identity.generate();
    hub.override.set(`GET /v1/agent-pairings/${PAIRING}`, () => ({
      pairing_id: PAIRING,
      pub: agent.public(),
      state: 'open',
      expires_at: formatTime(Date.now() + 600_000),
    }));
    const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
    const resume = { pairingId: PAIRING, fingerprint: fp, port: 40123, name: 'Claude Code', host: 'Mac' };
    const harness = await renderApp('/', { hub });
    await harness.router.navigate('/connect', { state: { resume } });
    expect(await screen.findByTestId('connect.confirm')).toBeEnabled();
    expect(screen.getByTestId('connect.name')).toHaveTextContent('Claude Code');
    harness.view.unmount();
    const broken = await renderApp('/', { hub });
    await broken.router.navigate('/connect', { state: { resume: { ...resume, port: 1 } } });
    expect(await screen.findByTestId('connect.invalid')).toBeInTheDocument();
    broken.view.unmount();
    const foreign = await renderApp('/', { hub });
    await foreign.router.navigate('/connect', { state: { other: 1 } });
    expect(await screen.findByTestId('connect.invalid')).toBeInTheDocument();
    foreign.view.unmount();
    const empty = await renderApp('/', { hub });
    await empty.router.navigate('/connect', { state: { resume: null } });
    expect(await screen.findByTestId('connect.invalid')).toBeInTheDocument();
  });
});
