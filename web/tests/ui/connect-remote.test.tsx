// A pairing from another computer (ADR-058) in jsdom with the simulated hub: `/connect#…&m=remote&…` shows the
// warning and asks for the agent code (normalised; a wrong code keeps the passkey button disabled; the page never
// shows the code), confirms with the handover sealed to the agent instead of the loopback form, shows the return code
// derived from the same `r` and waits for the claim; a mode that link and hub disagree on is refused both ways; the
// invitation asks for the code before the passkey is created and `/connect` resumes without asking again; the shell
// banner of a remote main orchestrator (not for a loopback one) with "Disconnect"; the settings marker.
import { configure, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { preparePrivateOrg } from '../../src/core/account/genesis';
import { signIn } from '../../src/core/account/login';
import { register } from '../../src/core/account/register';
import { agentCode, fingerprint, formatCode, openHandover, returnCode } from '../../src/core/crypto/agent';
import { Identity } from '../../src/core/crypto/keys';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { formatTime } from '../../src/core/encoding/time';
import { forgetFragment } from '../../src/ui/screens/invite/fragment';
import { FakeAuthenticator } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

// the passkey flows run real WebCrypto: allow them time on a loaded machine
configure({ asyncUtilTimeout: 10_000 });

const PAIRING = '01a1ee56-5e00-7242-9e24-f87629ad2977';
const OLD = '01a1ee56-5e00-7aaa-8d68-1fafbfc5d917';
const NONCE = 'yw1CyUW_rzOg-hZzWeorVQ';
const T = '2026-11-23T12:00:00.000Z';
const LABEL = 'A'.repeat(64);
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

const row = (agentId: string, state: string, remote?: boolean) => ({
  agent_id: agentId,
  state,
  label_ct: LABEL,
  created_at: T,
  confirmed_at: T,
  claimed_at: state === 'active' ? T : null,
  last_used_at: null,
  revoked_at: null,
  replaces: null,
  ...(remote === undefined ? {} : { remote }),
});

interface World {
  readonly hub: SimHub;
  readonly auth: FakeAuthenticator;
  readonly agent: Identity;
  readonly fp: string;
  readonly code: string;
  readonly confirmations: Record<string, string>[];
  /** What `GET /v1/me/agents` answers. */
  agents: unknown[];
}

async function world(remoteAtHub = true): Promise<World> {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  const agent = await Identity.generate();
  const fp = await fingerprint(agent.kemPublic(), agent.sigPublic());
  const made: World = {
    hub,
    auth,
    agent,
    fp,
    code: await agentCode(PAIRING, fp, b64uDecode(NONCE), ORIGIN),
    confirmations: [],
    agents: [],
  };
  hub.override.set(`GET /v1/agent-pairings/${PAIRING}`, () => ({
    pairing_id: PAIRING,
    pub: agent.public(),
    state: 'open',
    expires_at: formatTime(Date.now() + 240_000),
    ...(remoteAtHub ? { remote: true, nonce: NONCE } : {}),
  }));
  hub.override.set('POST /v1/me/agents', (body) => {
    made.confirmations.push(body as Record<string, string>);
    made.agents = [row(PAIRING, 'confirmed', true)];
    return { agent_id: PAIRING };
  });
  hub.override.set('GET /v1/me/agents', () => ({ agents: made.agents }));
  return made;
}

/** A user with a private org, signed out (or signed in and unlocked) before the page renders. */
async function account(w: World, path: string, signedIn = false) {
  return renderApp(path, { hub: w.hub, auth: w.auth }, async (made) => {
    const invite = w.hub.addInvite();
    const registration = await register(made.app.anon, w.auth, invite);
    const login = await signIn({ anon: made.app.anon, credentials: w.auth, origin: ORIGIN });
    await made.app.begin(login);
    await made.app.unlock(registration.identity);
    await made.app.uploadPasskeySlot(login, registration.identity);
    const prepared = await preparePrivateOrg({
      userId: registration.userId,
      identity: registration.identity,
      displayName: 'Ada',
      locale: 'en',
      platformInviteId: await w.hub.idOf(invite),
    });
    await made.app.client.request('POST', '/v1/orgs', { body: prepared.body });
    await made.app.installOrg(prepared);
    if (!signedIn) await made.app.signOut();
  });
}

const remoteFragment = (w: World) => `p=${PAIRING}&f=${w.fp}&m=remote&a=Claude+Code&h=build-server`;

describe('ConnectScreen for an agent on another computer', () => {
  it('asks for the code, confirms with the handover, shows the return code and waits for the claim', async () => {
    const w = await world();
    window.history.replaceState(null, '', `/connect#${remoteFragment(w)}`);
    const { app } = await account(w, '/connect');
    expect(await screen.findByTestId('connect.remote-warning')).toHaveTextContent(
      'This AI agent runs on another computer (build-server).',
    );
    expect(screen.queryByTestId('connect.warning')).toBeNull();
    expect(screen.getByTestId('connect.host')).toHaveTextContent('build-server');
    expect(screen.getByTestId('connect.age')).toHaveTextContent('Requested 1 min ago');
    // the page never shows the agent code
    expect(document.body.textContent).not.toContain(w.code.slice(0, 5));
    const input = screen.getByTestId('connect.code.input');
    const button = screen.getByTestId('connect.confirm');
    expect(button).toBeDisabled();
    fireEvent.change(input, { target: { value: 'ABCDE-FGHJK' } });
    expect(button).toBeDisabled();
    expect(screen.getByTestId('connect.code')).toHaveTextContent('This is not the code of this request.');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    fireEvent.change(input, { target: { value: formatCode(w.code).toLowerCase() } });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    const shown = await screen.findByTestId('connect.return-code');
    expect(screen.getByTestId('connect.returned')).toHaveTextContent('Give this code to your AI agent');
    expect(screen.getByTestId('connect.outcome.waiting')).toHaveTextContent('Waiting for your agent');
    expect(submitted).toHaveLength(0);
    expect(w.confirmations).toHaveLength(1);
    const handover = await openHandover(w.agent.kem, PAIRING, app.userId!, w.confirmations[0]!.handover_ct!);
    expect(handover.rootKey).toBe(app.rootKey);
    expect(shown).toHaveTextContent(formatCode(await returnCode(handover.secret, PAIRING, app.userId!)));
    w.agents = [row(PAIRING, 'active', true)];
    expect(await screen.findByTestId('connect.outcome.connected')).toHaveTextContent('Your agent is connected.');
    expect(screen.getByTestId('connect.returned')).toHaveAttribute('data-outcome', 'connected');
  });

  it('reports a claim that did not happen', async () => {
    const w = await world();
    window.history.replaceState(null, '', `/connect#${remoteFragment(w)}`);
    await account(w, '/connect', true);
    fireEvent.change(await screen.findByTestId('connect.code.input'), { target: { value: w.code } });
    w.hub.override.set('POST /v1/me/agents', (body) => {
      w.confirmations.push(body as Record<string, string>);
      w.agents = [row(PAIRING, 'expired', true)];
      return { agent_id: PAIRING };
    });
    fireEvent.click(screen.getByTestId('connect.confirm'));
    expect(await screen.findByTestId('connect.outcome.failed')).toHaveTextContent('did not complete');
  });

  it('refuses a mode that link and hub do not agree on, both ways', async () => {
    const loopbackHub = await world(false);
    window.history.replaceState(null, '', `/connect#${remoteFragment(loopbackHub)}`);
    const first = await renderApp('/connect', { hub: loopbackHub.hub });
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_mismatch');
    expect(screen.getByTestId('connect.error')).toHaveTextContent('This link is not valid');
    first.view.unmount();
    const remoteHub = await world(true);
    window.history.replaceState(null, '', `/connect#p=${PAIRING}&f=${remoteHub.fp}&c=40123&a=Claude+Code&h=Mac`);
    await renderApp('/connect', { hub: remoteHub.hub });
    expect(await screen.findByTestId('connect.error')).toHaveAttribute('data-code', 'pairing_mismatch');
  });
});

describe('InviteScreen with an agent on another computer', () => {
  async function invite(w: World) {
    const code = w.hub.addInvite();
    window.history.replaceState({ key: 'invite' }, '', `/invite#${code}&${remoteFragment(w)}&u=Ada`);
    return renderApp('/invite', { hub: w.hub, auth: w.auth });
  }

  it('asks for the code before the passkey is created, then confirms on /connect without asking again', async () => {
    const w = await world();
    const { router, app } = await invite(w);
    expect(await screen.findByTestId('invite.agent.remote-warning')).toHaveTextContent('build-server');
    expect(screen.queryByTestId('invite.agent.warning')).toBeNull();
    const create = screen.getByTestId('invite.create-passkey');
    expect(create).toBeDisabled();
    fireEvent.submit(create.closest('form')!);
    fireEvent.change(screen.getByTestId('invite.agent.code.input'), { target: { value: 'ABCDE-FGHJK' } });
    expect(create).toBeDisabled();
    expect(w.hub.users.size).toBe(0);
    fireEvent.change(screen.getByTestId('invite.agent.code.input'), { target: { value: ` ${w.code} ` } });
    expect(create).toBeEnabled();
    fireEvent.click(create);
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    expect(await screen.findByTestId('connect.return-code')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/connect');
    expect(screen.queryByTestId('connect.code')).toBeNull();
    expect(w.confirmations).toHaveLength(1);
    const handover = await openHandover(w.agent.kem, PAIRING, app.userId!, w.confirmations[0]!.handover_ct!);
    expect(screen.getByTestId('connect.return-code')).toHaveTextContent(
      formatCode(await returnCode(handover.secret, PAIRING, app.userId!)),
    );
    expect(submitted).toHaveLength(0);
  });
});

describe('the banner of a main orchestrator on another computer', () => {
  it('shows a remote main orchestrator on every page and disconnects it', async () => {
    const w = await world();
    w.agents = [row(OLD, 'active', true)];
    let revoked = 0;
    w.hub.override.set(`DELETE /v1/me/agents/${OLD}`, () => {
      revoked += 1;
      w.agents = [{ ...row(OLD, 'revoked', true), revoked_at: T }];
      return {};
    });
    await account(w, '/settings', true);
    const banner = await screen.findByTestId('shell.remote-agent');
    expect(banner).toHaveTextContent('Your main orchestrator runs on another computer (unknown computer), connected');
    expect(banner).toHaveAttribute('data-agent', OLD);
    expect(await screen.findByTestId('agents.item.remote')).toHaveTextContent('Runs on another computer');
    fireEvent.click(screen.getByTestId('shell.remote-agent.disconnect'));
    await waitFor(() => expect(screen.queryByTestId('shell.remote-agent')).toBeNull());
    expect(revoked).toBe(1);
  });

  it('reports a failed disconnect and shows nothing for a loopback main orchestrator', async () => {
    const w = await world();
    w.agents = [row(OLD, 'active', true)];
    w.hub.override.set(`DELETE /v1/me/agents/${OLD}`, new HubFailure(503, 'unavailable'));
    const first = await account(w, '/settings', true);
    fireEvent.click(await screen.findByTestId('shell.remote-agent.disconnect'));
    expect(await screen.findByTestId('shell.remote-agent.failed')).toBeInTheDocument();
    first.view.unmount();
    const loopback = await world();
    loopback.agents = [row(OLD, 'active', false)];
    await account(loopback, '/settings', true);
    expect(await screen.findByTestId('agents.item')).toBeInTheDocument();
    expect(screen.queryByTestId('shell.remote-agent')).toBeNull();
    expect(screen.queryByTestId('agents.item.remote')).toBeNull();
  });
});
