import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { preparePrivateOrg } from '../../src/core/account/genesis';
import { signIn } from '../../src/core/account/login';
import { register, type Registration } from '../../src/core/account/register';
import { FakeAuthenticator, giveRecoveryCode } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { type makeApp, renderApp } from './harness';

type Made = Awaited<ReturnType<typeof makeApp>>;

/** A registered user with a private org, signed out again (the hub knows the user; the browser does not). */
async function registeredUser(hub: SimHub, auth: FakeAuthenticator, made: Made, options: { slot: boolean }) {
  const invite = hub.addInvite();
  const registered = await register(made.app.anon, auth, invite);
  // an account with a recovery code (the prf_missing guidance unlocks with it)
  const registration = { ...registered, recoveryCode: await giveRecoveryCode(hub, registered) };
  const login = await signIn({ anon: made.app.anon, credentials: auth, origin: ORIGIN });
  await made.app.begin(login);
  await made.app.unlock(registration.identity);
  if (options.slot) await made.app.uploadPasskeySlot(login, registration.identity);
  const prepared = await preparePrivateOrg({
    userId: registration.userId,
    identity: registration.identity,
    displayName: 'Ada',
    locale: 'en',
    platformInviteId: await hub.idOf(invite),
  });
  await made.app.client.request('POST', '/v1/orgs', { body: prepared.body });
  await made.app.installOrg(prepared);
  await made.app.signOut();
  return { registration, org: prepared.org };
}

async function loginPage(options: { slot?: boolean; prf?: boolean; path?: string } = {}) {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  let user: { registration: Registration & { recoveryCode: string }; org: string } | null = null;
  const harness = await renderApp(options.path ?? '/login', { hub, auth }, async (made) => {
    user = await registeredUser(hub, auth, made, { slot: options.slot ?? true });
    auth.prf = options.prf ?? true;
  });
  await screen.findByTestId('login.submit');
  return { ...harness, user: user! };
}

describe('LoginScreen', () => {
  it('unlocks with PRF and goes to the local next target', async () => {
    const { router, user } = await loginPage({ path: `/login?next=${encodeURIComponent('/settings')}` });
    fireEvent.click(screen.getByTestId('login.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe('/settings'));
    expect(user.org).toBeTruthy();
  });

  it('ignores a foreign next target and starts at the last org', async () => {
    const { router, user } = await loginPage({ path: '/login?next=//evil.example' });
    fireEvent.click(screen.getByTestId('login.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${user.org}`));
  });

  it('reports a cancelled gesture and a refused sign-in', async () => {
    const { auth, hub } = await loginPage();
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('login.error')).toHaveTextContent('Sign-in was cancelled');
    hub.override.set('POST /v1/passkeys/login/finish', new HubFailure(400, 'credential_invalid'));
    fireEvent.click(screen.getByTestId('login.submit'));
    await waitFor(() => expect(screen.getByTestId('login.error')).not.toHaveTextContent('cancelled'));
  });

  it('reports an unlock that fails after the sign-in', async () => {
    const { hub } = await loginPage();
    hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('login.error')).toHaveTextContent('Service unavailable');
  });

  it('unlocks with the recovery code without PRF and records prf_missing', async () => {
    const { router, user, app } = await loginPage({ prf: false });
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
    expect(app.store.getState().session.prfMissing).toBe(true);
    fireEvent.change(screen.getByTestId('prf-missing.recovery.input'), { target: { value: 'AAAAA' } });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    expect(await screen.findByTestId('prf-missing.error')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('prf-missing.recovery.input'), {
      target: { value: user.registration.recoveryCode },
    });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${user.org}`));
  });

  it('offers the recovery code when the passkey slot does not open', async () => {
    const { hub, user } = await loginPage();
    const bundles = hub.users.get(user.registration.userId)!.bundles;
    for (const slot of bundles.keys()) if (slot.startsWith('passkey:')) bundles.set(slot, bundles.get('recovery')!);
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
  });

  it('offers the recovery code when the unlocked identity is not the registered one', async () => {
    const { hub, user } = await loginPage();
    const real = hub.me.bind(hub);
    hub.override.set('GET /v1/me', () => ({
      ...real(hub.users.get(user.registration.userId)!),
      identity: { kem_pub: 'A'.repeat(43), sig_pub: 'A'.repeat(43) },
    }));
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
  });

  it('uploads the missing passkey slot after a recovery-code unlock with PRF', async () => {
    const { router, user, hub } = await loginPage({ slot: false });
    fireEvent.click(screen.getByTestId('login.submit'));
    fireEvent.change(await screen.findByTestId('prf-missing.recovery.input'), {
      target: { value: user.registration.recoveryCode },
    });
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${user.org}`));
    expect(
      [...hub.users.get(user.registration.userId)!.bundles.keys()].some((slot) => slot.startsWith('passkey:')),
    ).toBe(true);
  });

  it('continues when the slot upload fails, and the recovery prompt can be left', async () => {
    const { router, user, hub } = await loginPage({ slot: false });
    fireEvent.click(screen.getByTestId('login.submit'));
    await screen.findByTestId('prf-missing.recovery.input');
    fireEvent.click(screen.getByTestId('prf-missing.cancel'));
    fireEvent.click(await screen.findByTestId('login.submit'));
    fireEvent.change(await screen.findByTestId('prf-missing.recovery.input'), {
      target: { value: user.registration.recoveryCode },
    });
    // the hub refuses every passkey-slot upload
    const bundles = hub.users.get(user.registration.userId)!.bundles;
    const set = bundles.set.bind(bundles);
    bundles.set = (slot: string, value: string) => {
      if (slot.startsWith('passkey:')) throw new HubFailure(503, 'unavailable');
      return set(slot, value);
    };
    fireEvent.click(screen.getByTestId('prf-missing.recovery.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${user.org}`));
    expect([...bundles.keys()]).toEqual(['recovery']);
  });

  it('offers "Stay signed in", on by default, and keeps the choice for this browser (rule E-37)', async () => {
    const { app } = await loginPage();
    const stay = screen.getByTestId('login.stay') as HTMLInputElement;
    expect(stay.checked).toBe(true);
    expect(screen.getByTestId('login.stay.hint')).toHaveTextContent('until you sign out');
    fireEvent.click(stay);
    expect(stay.checked).toBe(false);
    expect(app.prefs.staySignedIn()).toBe(false);
    fireEvent.click(stay);
    expect(app.prefs.staySignedIn()).toBe(true);
  });

  it('sends an unlocked user on', async () => {
    const harness = await renderApp('/login?next=/settings', {}, async (made) => {
      const hub = made.hub;
      const invite = hub.addInvite();
      const registration = await register(made.app.anon, made.auth, invite);
      await made.app.begin(await signIn({ anon: made.app.anon, credentials: made.auth, origin: ORIGIN }));
      await made.app.unlock(registration.identity);
    });
    await waitFor(() => expect(harness.router.state.location.pathname).toBe('/settings'));
  });
});
