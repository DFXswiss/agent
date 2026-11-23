import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatCode, generateCode } from '../../src/core/crypto/invite';
import { CryptoError } from '../../src/core/crypto/errors';
import { HubError } from '../../src/core/net/signedFetch';
import { forgetFragment, takeFragment } from '../../src/ui/screens/invite/fragment';
import { afterLookup, failedLookup } from '../../src/ui/screens/invite/machine';
import {
  confirmsCode,
  downloadText,
  FILE_NAME,
  lastGroup,
  RecoveryCodeStep,
} from '../../src/ui/screens/invite/RecoveryCodeStep';
import { FakeAuthenticator } from '../core/account/authenticator';
import { HubFailure, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

const CODE = 'ABCDE-FGHIJ-KLMNO-PQRST-UVWXYZ';

afterEach(() => forgetFragment());

function fakeWindow(hash: string, state: unknown = { key: 'k1' }) {
  const win = {
    location: { hash, pathname: '/invite', search: '?x=1' },
    history: { state, replaceState: vi.fn() },
  };
  win.history.replaceState.mockImplementation((next: unknown) => {
    win.location.hash = '';
    win.history.state = next;
  });
  return win;
}

describe('fragment', () => {
  it('takes the code once, removes it and returns it again for the same history entry', () => {
    const win = fakeWindow('#CODE');
    expect(takeFragment(win)).toBe('CODE');
    expect(win.history.replaceState).toHaveBeenCalledWith({ key: 'k1' }, '', '/invite?x=1');
    expect(takeFragment(win)).toBe('CODE');
    win.history.state = { key: 'k2' };
    expect(takeFragment(win)).toBe('');
  });

  it('works with a plain history state and without a fragment', () => {
    expect(takeFragment(fakeWindow('', null))).toBe('');
    const win = fakeWindow('#X', 'plain');
    expect(takeFragment(win)).toBe('X');
    expect(takeFragment(win)).toBe('X');
    forgetFragment();
    expect(takeFragment(win)).toBe('');
  });
});

describe('invitation steps', () => {
  const lookup = {
    inviteId: 'i',
    kind: 'user',
    scope: 'platform' as const,
    expiresAt: 't',
    registered: false,
    raw: {},
  };

  it('classifies lookup failures', () => {
    expect(failedLookup(new CryptoError('invite_code_length'))).toEqual({
      step: 'malformed',
      code: 'invite_code_length',
    });
    expect(failedLookup(new HubError('invite_expired', 410))).toEqual({ step: 'invalid', code: 'invite_expired' });
    expect(failedLookup(new HubError('not_found', 404))).toEqual({ step: 'invalid', code: 'invite_unknown' });
    expect(failedLookup(new HubError('unavailable', 503))).toEqual({ step: 'error', code: 'unavailable' });
    expect(failedLookup(new Error('x'))).toEqual({ step: 'error', code: 'unavailable' });
  });

  it('branches by scope, kind and registration', () => {
    expect(afterLookup({ ...lookup, scope: 'org' }).step).toBe('org');
    expect(afterLookup({ ...lookup, kind: 'device' })).toEqual({ step: 'invalid', code: 'invite_unknown' });
    expect(afterLookup({ ...lookup, registered: true })).toEqual({ step: 'invalid', code: 'invite_registered' });
    expect(afterLookup(lookup)).toEqual({ step: 'intro', lookup, notice: null });
  });
});

describe('RecoveryCodeStep', () => {
  it('confirms with the last group only', () => {
    expect(lastGroup(CODE)).toBe('UVWXYZ');
    expect(confirmsCode(CODE, ' uvwxyz ')).toBe(true);
    expect(confirmsCode(CODE, 'UVWXY')).toBe(false);
  });

  it('copies, downloads, refuses a wrong group and asks before skipping', async () => {
    const copy = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('denied'));
    const download = vi.fn();
    const onConfirmed = vi.fn();
    const onSkipped = vi.fn();
    render(
      <RecoveryCodeStep
        code={CODE}
        allowSkip
        copy={copy}
        download={download}
        onConfirmed={onConfirmed}
        onSkipped={onSkipped}
      />,
    );
    fireEvent.click(screen.getByTestId('recovery-code.copy'));
    expect(await screen.findByTestId('recovery-code.copied')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('recovery-code.copy'));
    expect(await screen.findByTestId('recovery-code.copy-failed')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('recovery-code.download'));
    expect(download).toHaveBeenCalledWith(FILE_NAME, expect.stringContaining(CODE));
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: 'nope' } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(screen.getByTestId('recovery-code.mismatch')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: 'uvwxyz' } });
    expect(screen.queryByTestId('recovery-code.mismatch')).toBeNull();
    fireEvent.click(screen.getByTestId('recovery-code.skip'));
    const dialog = screen.getByTestId('recovery-code.skip-dialog');
    expect(screen.getByTestId('recovery-code.skip-confirm')).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(screen.getByTestId('recovery-code.skip-cancel')).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(screen.getByTestId('recovery-code.skip-confirm')).toHaveFocus();
    fireEvent.keyDown(dialog, { key: 'Enter' });
    expect(screen.getByTestId('recovery-code.skip-dialog')).toBeInTheDocument();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByTestId('recovery-code.skip-dialog')).toBeNull();
    expect(screen.getByTestId('recovery-code.skip')).toHaveFocus();
    fireEvent.click(screen.getByTestId('recovery-code.skip'));
    fireEvent.click(screen.getByTestId('recovery-code.skip-cancel'));
    expect(screen.queryByTestId('recovery-code.skip-dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('recovery-code.skip'));
    fireEvent.click(screen.getByTestId('recovery-code.skip-confirm'));
    expect(onSkipped).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(onConfirmed).toHaveBeenCalledOnce();
  });

  it('offers no skip in the recovery flow and uses the browser defaults', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<RecoveryCodeStep code={CODE} allowSkip={false} onConfirmed={vi.fn()} onSkipped={vi.fn()} />);
    expect(screen.queryByTestId('recovery-code.skip')).toBeNull();
    fireEvent.click(screen.getByTestId('recovery-code.copy'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(CODE));
    fireEvent.click(screen.getByTestId('recovery-code.download'));
    expect(click).toHaveBeenCalledOnce();
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x'));
    expect(document.querySelector('a[download]')).toBeNull();
    downloadText('x.txt', 'y');
    expect(click).toHaveBeenCalledTimes(2);
  });
});

/** Opens `/invite#<code>` of a new platform invitation of `hub`. */
async function openInvite(hub = new SimHub(), auth = new FakeAuthenticator(), languages = ['en']) {
  const code = hub.addInvite();
  const id = await hub.idOf(code);
  window.history.replaceState({ key: 'invite' }, '', `/invite#${code}`);
  const harness = await renderApp(`/invite`, { hub, auth, languages });
  await screen.findByTestId('invite.display-name');
  return { ...harness, id, code };
}

async function toUnlock(harness: Awaited<ReturnType<typeof openInvite>>) {
  fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Ada' } });
  fireEvent.click(screen.getByTestId('invite.create-passkey'));
  await screen.findByTestId('invite.unlock');
  return harness;
}

describe('InviteScreen', () => {
  it('runs the platform path to the private org and remembers a skipped recovery code', async () => {
    const harness = await toUnlock(await openInvite(undefined, undefined, ['de']));
    expect(window.location.hash).toBe('');
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await screen.findByTestId('recovery-code.code');
    fireEvent.click(screen.getByTestId('recovery-code.skip'));
    fireEvent.click(screen.getByTestId('recovery-code.skip-confirm'));
    await waitFor(() => expect(harness.router.state.location.pathname).toMatch(/^\/o\//));
    const org = harness.router.state.location.pathname.slice(3);
    expect(harness.hub.orgs.get(org)).toBeDefined();
    expect(harness.app.store.getState().session.recoveryUnconfirmed).toBe(true);
    expect(await screen.findByTestId('shell.recovery-banner')).toBeInTheDocument();
    await waitFor(() => expect(harness.live).toHaveBeenCalled());
  });

  it('offers no skip when the passkey gave no PRF output (the code is the only unlock)', async () => {
    const auth = new FakeAuthenticator();
    auth.prf = false;
    const harness = await toUnlock(await openInvite(new SimHub(), auth));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await screen.findByTestId('recovery-code.code');
    expect(screen.queryByTestId('recovery-code.skip')).toBeNull();
    expect(harness.app.store.getState().session.prfMissing).toBe(true);
  });

  it('returns to the intro after a cancelled creation and reports refusals', async () => {
    const auth = new FakeAuthenticator();
    const harness = await openInvite(new SimHub(), auth);
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    expect(screen.getByTestId('invite.display-name.invalid')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('invite.display-name'), { target: { value: 'Ada' } });
    expect(screen.queryByTestId('invite.display-name.invalid')).toBeNull();
    auth.refuse = 'AbortError';
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('Passkey creation was cancelled');
    harness.hub.override.set('POST /v1/passkeys/register/finish', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Service unavailable'));
    harness.hub.override.set('POST /v1/passkeys/register/finish', new HubFailure(404, 'invite_unknown'));
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    expect(await screen.findByTestId('invite.invalid')).toHaveAttribute('data-code', 'invite_unknown');
  });

  it('refuses another account at the second gesture and reports unlock errors', async () => {
    const hub = new SimHub();
    const auth = new FakeAuthenticator();
    const harness = await toUnlock(await openInvite(hub, auth));
    const mine = auth.credentials[0]!;
    // another registered account on the same authenticator
    const { register } = await import('../../src/core/account/register');
    await register(harness.app.anon, auth, hub.addInvite());
    auth.use = auth.credentials[1]!;
    fireEvent.click(screen.getByTestId('invite.unlock'));
    expect(await screen.findByTestId('invite.notice')).toHaveTextContent('belongs to another account');
    auth.use = mine;
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Sign-in was cancelled'));
    hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await waitFor(() => expect(screen.getByTestId('invite.notice')).toHaveTextContent('Service unavailable'));
    hub.override.delete('GET /v1/me');
    fireEvent.click(screen.getByTestId('invite.unlock'));
    await screen.findByTestId('recovery-code.code');
  });

  it('retries a failed genesis with the prepared request and reports a failed preparation', async () => {
    const hub = new SimHub();
    const harness = await toUnlock(await openInvite(hub));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    const code = (await screen.findByTestId('recovery-code.code')).textContent!;
    hub.override.set('POST /v1/orgs', new HubFailure(503, 'unavailable'));
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: code.slice(-6) } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('invite.accept-failed')).toHaveAttribute('data-code', 'unavailable');
    const first = hub.log.filter((entry) => entry.target === '/v1/orgs').map((entry) => entry.body);
    hub.override.delete('POST /v1/orgs');
    fireEvent.click(screen.getByTestId('invite.accept-retry'));
    await waitFor(() => expect(harness.router.state.location.pathname).toMatch(/^\/o\//));
    const sent = hub.log.filter((entry) => entry.target === '/v1/orgs').map((entry) => entry.body);
    expect(sent).toEqual([first[0], first[0]]);
    expect(harness.app.store.getState().session.recoveryUnconfirmed).toBe(false);
  });

  it('reports a genesis that cannot be prepared', async () => {
    const hub = new SimHub();
    await toUnlock(await openInvite(hub));
    fireEvent.click(screen.getByTestId('invite.unlock'));
    const code = (await screen.findByTestId('recovery-code.code')).textContent!;
    // the display name was valid at the intro; an empty one now makes the preparation fail
    const genesis = await import('../../src/core/account/genesis');
    const spy = vi.spyOn(genesis, 'preparePrivateOrg').mockRejectedValueOnce(new CryptoError('header_invalid'));
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: code.slice(-6) } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('invite.accept-failed')).toHaveAttribute('data-code', 'header_invalid');
    spy.mockRestore();
  });

  it('shows the lookup error and retries; org invitations branch off', async () => {
    const hub = new SimHub();
    const code = formatCode(generateCode());
    hub.override.set('POST /v1/invites/lookup', new HubFailure(503, 'unavailable'));
    window.history.replaceState({ key: 'e' }, '', `/invite#${code}`);
    await renderApp('/invite', { hub });
    expect(await screen.findByTestId('invite.error')).toHaveAttribute('data-code', 'unavailable');
    hub.override.set('POST /v1/invites/lookup', () => ({
      kind: 'user',
      scope: 'org',
      expires_at: '2030-01-01T00:00:00.000Z',
      registered: false,
    }));
    await act(async () => fireEvent.click(screen.getByTestId('invite.retry')));
    expect(await screen.findByTestId('invite.org')).toBeInTheDocument();
  });

  it('ignores a lookup that fails after the screen was left', async () => {
    const hub = new SimHub();
    let release: () => void = () => undefined;
    hub.override.set(
      'POST /v1/invites/lookup',
      () => new Promise((_resolve, reject) => (release = () => reject(new HubFailure(503, 'unavailable')))),
    );
    window.history.replaceState({ key: 'late-error' }, '', `/invite#${formatCode(generateCode())}`);
    const harness = await renderApp('/invite', { hub });
    await screen.findByTestId('invite.lookup');
    await act(async () => harness.router.navigate('/login'));
    await act(async () => release());
    expect(screen.queryByTestId('invite.error')).toBeNull();
  });

  it('asks to check a malformed code', async () => {
    window.history.replaceState({ key: 'bad' }, '', '/invite#ABCDE');
    await renderApp('/invite');
    expect(await screen.findByTestId('invite.malformed')).toHaveAttribute('data-code', 'invite_code_length');
  });

  it('ignores a lookup that finishes after the screen was left', async () => {
    const hub = new SimHub();
    let release: () => void = () => undefined;
    hub.override.set(
      'POST /v1/invites/lookup',
      () => new Promise((resolve) => (release = () => resolve({ scope: 'platform', kind: 'user', registered: false }))),
    );
    window.history.replaceState({ key: 'late' }, '', `/invite#${formatCode(generateCode())}`);
    const harness = await renderApp('/invite', { hub });
    await screen.findByTestId('invite.lookup');
    await act(async () => harness.router.navigate('/login'));
    await act(async () => release());
    expect(screen.queryByTestId('invite.display-name')).toBeNull();
    expect(screen.getByTestId('login.page')).toBeInTheDocument();
  });
});
