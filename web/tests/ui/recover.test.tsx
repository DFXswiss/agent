// `/recover` in jsdom against the simulated hub (06-web.md section 3.5): the waiting time of a refused recovery, every
// refusal and cancellation of the steps, another passkey answering, and a new recovery code that fails to save and is
// retried. The successful path runs in the browser (e2e/recover.spec.ts).
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { anonClient } from '../../src/core/account/anon';
import { register } from '../../src/core/account/register';
import { b64uEncode } from '../../src/core/encoding/b64u';
import { FakeAuthenticator, giveRecoveryCode } from '../core/account/authenticator';
import { HubFailure, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

async function recoverPage() {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  let code = '';
  let userId = '';
  const harness = await renderApp('/recover', { hub, auth }, async (made) => {
    const registration = await register(made.app.anon, auth, hub.addInvite());
    // an account with a recovery code
    code = await giveRecoveryCode(hub, registration);
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

  it('stores the new code before showing it, retries a failed save and goes on with one button', async () => {
    const { hub, auth, code, userId, router } = await recoverPage();
    submitCode(code);
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    hub.override.set('PUT /v1/me/bundles/recovery', new HubFailure(503, 'unavailable'));
    fireEvent.click(await screen.findByTestId('recover.unlock'));
    expect(await screen.findByTestId('recover.save-failed')).toHaveAttribute('data-code', 'unavailable');
    expect(screen.queryByTestId('recovery-code.code')).toBeNull();
    expect(auth.credentials).toHaveLength(2);
    hub.override.delete('PUT /v1/me/bundles/recovery');
    fireEvent.click(screen.getByTestId('recover.save-retry'));
    await screen.findByTestId('recovery-code.code');
    // stored already: the old code is gone; no retyping, no browser offer with PRF
    expect(hub.users.get(userId)!.recoveryRotation).toBe(false);
    expect(screen.getByTestId('recovery-code.explain')).toHaveTextContent('has replaced your old one');
    expect(screen.queryByTestId('recovery-code.remember')).toBeNull();
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    await waitFor(() => expect(router.state.location.pathname).not.toBe('/recover'));
  });

  it('says so when the refused new passkey cannot be removed', async () => {
    const { hub, auth, code } = await recoverPage();
    submitCode(code);
    auth.prf = false;
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    await screen.findByTestId('recover.unlock');
    const id = b64uEncode(auth.credentials.at(-1)!.id);
    hub.override.set(`DELETE /v1/passkeys/${id}`, new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('recover.unlock'));
    expect(await screen.findByTestId('prf-required.not-removed')).toBeInTheDocument();
  });

  it('does not accept a new passkey without PRF: it is removed, and the same code begins again', async () => {
    const { hub, auth, code, userId, router } = await recoverPage();
    submitCode(code);
    auth.prf = false;
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    fireEvent.click(await screen.findByTestId('recover.unlock'));
    const message = await screen.findByTestId('prf-required');
    expect(message).toHaveAttribute('data-context', 'recover');
    expect(message).toHaveTextContent('your recovery code still works');
    const user = hub.users.get(userId)!;
    expect(user.credentials.size).toBe(1);
    expect(user.bundles.has('recovery')).toBe(true);
    // the code was not replaced: it starts the recovery again, now with a passkey that has PRF
    fireEvent.click(screen.getByTestId('prf-required.retry'));
    auth.prf = true;
    fireEvent.click(screen.getByTestId('recover.submit'));
    fireEvent.click(await screen.findByTestId('recover.create-passkey'));
    fireEvent.click(await screen.findByTestId('recover.unlock'));
    fireEvent.click(await screen.findByTestId('recovery-code.continue'));
    await waitFor(() => expect(router.state.location.pathname).not.toBe('/recover'));
  });
});
