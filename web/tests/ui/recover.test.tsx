// `/recover` in jsdom against the simulated hub (06-web.md section 3.5): the waiting time of a refused recovery, every
// refusal and cancellation of the steps, another passkey answering, and a new recovery code that fails to save and is
// retried. The successful path runs in the browser (e2e/recover.spec.ts).
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { anonClient } from '../../src/core/account/anon';
import { register } from '../../src/core/account/register';
import { FakeAuthenticator } from '../core/account/authenticator';
import { HubFailure, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

async function recoverPage() {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  let code = '';
  let userId = '';
  const harness = await renderApp('/recover', { hub, auth }, async (made) => {
    const registration = await register(made.app.anon, auth, hub.addInvite());
    code = registration.recoveryCode;
    userId = registration.userId;
    await made.app.restore();
  });
  await screen.findByTestId('recover.code');
  return { ...harness, code, userId };
}

const submitCode = (code: string) => {
  fireEvent.change(screen.getByTestId('recover.code'), { target: { value: code } });
  fireEvent.click(screen.getByTestId('recover.submit'));
};

describe('RecoverScreen', () => {
  it('shows the waiting time of a refused recovery, or "later" without one, and other refusals', async () => {
    const { hub, code } = await recoverPage();
    hub.retryAfter = '90';
    submitCode(code);
    expect(await screen.findByTestId('recover.notice')).toHaveTextContent(
      'Too many recovery attempts. Try again in 2 minutes.',
    );
    hub.retryAfter = '';
    fireEvent.click(screen.getByTestId('recover.submit'));
    await waitFor(() =>
      expect(screen.getByTestId('recover.notice')).toHaveTextContent('Too many recovery attempts. Try again later.'),
    );
    hub.retryAfter = null;
    hub.override.set('POST /v1/recovery/begin', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('recover.submit'));
    await waitFor(() => expect(screen.getByTestId('recover.notice')).toHaveTextContent('Service unavailable'));
    hub.override.delete('POST /v1/recovery/begin');
    submitCode('AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA');
    await waitFor(() => expect(screen.getByTestId('recover.notice')).toHaveTextContent('No account was found'));
    submitCode('AAAAA');
    await waitFor(() => expect(screen.getByTestId('recover.notice')).not.toHaveTextContent('No account'));
  });

  it('begins again after a cancelled passkey and returns to the code when that begin fails', async () => {
    const { hub, auth, code } = await recoverPage();
    submitCode(code);
    auth.refuse = 'NotAllowedError';
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    expect(await screen.findByTestId('recover.notice')).toHaveTextContent('Passkey creation was cancelled');
    hub.override.set('POST /v1/recovery/begin', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('recover.create-passkey'));
    expect(await screen.findByTestId('recover.code')).toBeInTheDocument();
    expect(screen.getByTestId('recover.notice')).toHaveTextContent('Service unavailable');
  });

  it('reports a refused finish, another passkey answering and a refused sign-in', async () => {
    const { hub, auth, code } = await recoverPage();
    submitCode(code);
    hub.override.set('POST /v1/recovery/finish', new HubFailure(503, 'unavailable'));
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    expect(await screen.findByTestId('recover.notice')).toHaveTextContent('Service unavailable');
    hub.override.delete('POST /v1/recovery/finish');
    fireEvent.click(screen.getByTestId('recover.create-passkey'));
    await screen.findByTestId('recover.unlock');
    // another account's passkey answers the sign-in
    const other = await register(anonClient(hub.fetch), auth, hub.addInvite());
    expect(other.userId).toBeTruthy();
    auth.use = auth.credentials[auth.credentials.length - 1]!;
    fireEvent.click(screen.getByTestId('recover.unlock'));
    expect(await screen.findByTestId('recover.notice')).toHaveTextContent('Another passkey answered');
    auth.use = auth.credentials[1]!;
    hub.override.set('POST /v1/passkeys/login/finish', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('recover.unlock'));
    await waitFor(() => expect(screen.getByTestId('recover.notice')).toHaveTextContent('Service unavailable'));
    auth.refuse = 'AbortError';
    fireEvent.click(screen.getByTestId('recover.unlock'));
    await waitFor(() => expect(screen.getByTestId('recover.notice')).toHaveTextContent('Sign-in was cancelled'));
  });

  it('stores the new code before showing it, retries a failed save and keeps the reminder until confirmed', async () => {
    const { hub, auth, code, userId, router, app } = await recoverPage();
    submitCode(code);
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    hub.override.set('PUT /v1/me/bundles/recovery', new HubFailure(503, 'unavailable'));
    fireEvent.click(await screen.findByTestId('recover.unlock'));
    expect(await screen.findByTestId('recover.save-failed')).toHaveAttribute('data-code', 'unavailable');
    expect(screen.queryByTestId('recovery-code.code')).toBeNull();
    expect(auth.credentials).toHaveLength(2);
    hub.override.delete('PUT /v1/me/bundles/recovery');
    fireEvent.click(screen.getByTestId('recover.save-retry'));
    const fresh = (await screen.findByTestId('recovery-code.code')).textContent!;
    // stored already: the old code is gone, the reminder holds until the new code is confirmed
    expect(hub.users.get(userId)!.recoveryRotation).toBe(false);
    expect(app.store.getState().session.recoveryUnconfirmed).toBe(true);
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: fresh.slice(-6) } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    await waitFor(() => expect(router.state.location.pathname).not.toBe('/recover'));
    expect(app.store.getState().session.recoveryUnconfirmed).toBe(false);
  });
});
