// The settings section "Passkeys and recovery" in jsdom against the simulated hub (06-web.md sections 3.3 and 3.6):
// adding on this device, another passkey answering the confirmation, refusals, a passkey without PRF, removal with a
// failing reload, the dialog closed with Escape, and the device key of this browser when IndexedDB refuses.
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { signIn } from '../../src/core/account/login';
import { register } from '../../src/core/account/register';
import { parseRecoveryCode, recoveryId } from '../../src/core/crypto/recovery';
import { section } from '../../src/ui/settings/sections/passkeys';
import { deviceKeys } from '../../src/ui/prf-missing/device';
import { FakeAuthenticator } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

afterEach(() => vi.unstubAllGlobals());

async function settingsPage(options: { factory?: IDBFactory } = {}) {
  if (options.factory) vi.stubGlobal('indexedDB', options.factory);
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  let userId = '';
  const harness = await renderApp('/settings', { hub, auth }, async (made) => {
    const registration = await register(made.app.anon, auth, hub.addInvite());
    userId = registration.userId;
    await made.app.begin(await signIn({ anon: made.app.anon, credentials: auth, origin: ORIGIN }));
    await made.app.unlock(registration.identity);
  });
  await screen.findByTestId('passkeys.list');
  return { ...harness, userId };
}

describe('passkeys section', () => {
  it('is registered first in the settings', () => {
    expect(section).toMatchObject({ id: 'passkeys', order: 10, titleKey: 'passkeys.title' });
  });

  it('adds a passkey on this device; another passkey answering and refusals are reported', async () => {
    const { hub, auth, userId } = await settingsPage();
    hub.override.set('POST /v1/passkeys/register/begin', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('passkeys.add.this-device'));
    expect(await screen.findByTestId('passkeys.add.notice')).toHaveTextContent('Service unavailable');
    hub.override.delete('POST /v1/passkeys/register/begin');
    fireEvent.click(screen.getByTestId('passkeys.add.this-device'));
    await waitFor(() => expect(screen.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'confirm'));
    expect(auth.lastCreate!.authenticatorSelection!.authenticatorAttachment).toBeUndefined();
    // the first passkey answers instead of the new one
    auth.use = auth.credentials[0]!;
    fireEvent.click(screen.getByTestId('passkeys.add.confirm'));
    expect(await screen.findByTestId('passkeys.add.notice')).toHaveTextContent('Another passkey answered');
    auth.use = null;
    hub.override.set('POST /v1/passkeys/login/finish', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('passkeys.add.confirm'));
    await waitFor(() => expect(screen.getByTestId('passkeys.add.notice')).toHaveTextContent('Service unavailable'));
    hub.override.delete('POST /v1/passkeys/login/finish');
    // a passkey without PRF still signs in, but has no slot; ending the previous session may fail
    hub.override.set('POST /v1/session/end', new HubFailure(503, 'unavailable'));
    auth.prf = false;
    fireEvent.click(screen.getByTestId('passkeys.add.confirm'));
    await waitFor(() => expect(screen.getByTestId('passkeys.add.notice')).toHaveTextContent('cannot unlock your keys'));
    expect(screen.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'idle');
    expect(await screen.findAllByTestId('passkeys.item')).toHaveLength(2);
    expect([...hub.users.get(userId)!.bundles.keys()]).toEqual(['recovery']);
  });

  it('closes the removal dialog with Escape and reports a refused removal even when the reload fails', async () => {
    const { hub, auth, app } = await settingsPage();
    fireEvent.click(screen.getByTestId('passkeys.add.this-device'));
    fireEvent.click(await screen.findByTestId('passkeys.add.confirm'));
    await waitFor(() => expect(screen.getAllByTestId('passkeys.item')).toHaveLength(2));
    const other = screen.getAllByTestId('passkeys.item').find((item) => !item.textContent!.includes('this sign-in'))!;
    fireEvent.click(other.querySelector('[data-testid="passkeys.remove"]')!);
    fireEvent.keyDown(await screen.findByTestId('passkeys.remove-dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('passkeys.remove-dialog')).toBeNull();
    const id = other.getAttribute('data-credential')!;
    hub.override.set(`DELETE /v1/passkeys/${id}`, new HubFailure(503, 'unavailable'));
    hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    fireEvent.click(other.querySelector('[data-testid="passkeys.remove"]')!);
    fireEvent.click(await screen.findByTestId('passkeys.remove.confirm'));
    expect(await screen.findByTestId('passkeys.notice')).toHaveTextContent('Service unavailable');
    // a removal whose reload fails keeps the screen
    hub.override.delete(`DELETE /v1/passkeys/${id}`);
    fireEvent.click(other.querySelector('[data-testid="passkeys.remove"]')!);
    fireEvent.click(await screen.findByTestId('passkeys.remove.confirm'));
    await waitFor(() => expect(screen.getByTestId('passkeys.notice')).toHaveTextContent('The passkey was removed.'));
    expect(app.store.getState().session.phase).toBe('unlocked');
    expect(auth.credentials).toHaveLength(2);
  });

  it('shows the browser state when IndexedDB is missing or refuses', async () => {
    await settingsPage();
    expect(await screen.findByTestId('passkeys.browser.failed')).toBeInTheDocument();
  });

  it('reports a device key that cannot be stored or forgotten', async () => {
    const factory = new IDBFactory();
    const { userId } = await settingsPage({ factory });
    fireEvent.click(await screen.findByTestId('passkeys.browser.remember'));
    expect(await screen.findByTestId('passkeys.browser.remembered')).toBeInTheDocument();
    // the database goes away under the open screen
    (await deviceKeys(factory)).close();
    fireEvent.click(screen.getByTestId('passkeys.browser.forget'));
    expect(await screen.findByTestId('passkeys.browser.failed')).toBeInTheDocument();
    expect(userId).toBeTruthy();
  });

  it('reports a device key that cannot be stored', async () => {
    const factory = new IDBFactory();
    await settingsPage({ factory });
    await screen.findByTestId('passkeys.browser.none');
    (await deviceKeys(factory)).close();
    fireEvent.click(screen.getByTestId('passkeys.browser.remember'));
    expect(await screen.findByTestId('passkeys.browser.failed')).toBeInTheDocument();
  });
});

describe('a new recovery code (KOR-01)', () => {
  it('shows a new code, requires its confirmation and replaces the old one after a passkey gesture', async () => {
    const { hub, auth, userId, app } = await settingsPage();
    const user = hub.users.get(userId)!;
    const before = user.recoveryId;
    app.setRecoveryUnconfirmed(true);
    fireEvent.click(screen.getByTestId('passkeys.recovery-new.start'));
    const shown = (await screen.findByTestId('recovery-code.code')).textContent!.trim();
    expect(shown).toMatch(/^[A-Z2-7]{5}(-[A-Z2-7]{5}){3}-[A-Z2-7]{6}$/);
    // no skip: the confirmation of the onboarding is required
    expect(screen.queryByTestId('recovery-code.skip')).toBeNull();
    // a cancelled gesture keeps the code and changes nothing
    auth.refuse = 'NotAllowedError';
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: shown.slice(-6) } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('passkeys.recovery-new.notice')).toHaveTextContent('cancelled');
    expect(user.recoveryId).toBe(before);
    expect(screen.getByTestId('recovery-code.code')).toHaveTextContent(shown);
    // a refusal of the hub is reported
    auth.refuse = null;
    hub.override.set('PUT /v1/me/bundles/recovery', new HubFailure(503, 'unavailable'));
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: shown.slice(-6) } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    await waitFor(() =>
      expect(screen.getByTestId('passkeys.recovery-new.notice')).toHaveTextContent('Service unavailable'),
    );
    hub.override.delete('PUT /v1/me/bundles/recovery');
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: shown.slice(-6).toLowerCase() } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('passkeys.recovery-new.notice')).toHaveTextContent('new recovery code is saved');
    expect(screen.getByTestId('passkeys.recovery-new')).toHaveAttribute('data-step', 'idle');
    expect(user.recoveryId).not.toBe(before);
    expect(app.store.getState().session.recoveryUnconfirmed).toBe(false);
    // the new code is the one the hub now knows
    expect(user.recoveryId).toBe(await recoveryId(parseRecoveryCode(shown)));
  });

  it('can be cancelled before anything is sent', async () => {
    const { hub } = await settingsPage();
    fireEvent.click(screen.getByTestId('passkeys.recovery-new.start'));
    await screen.findByTestId('recovery-code.code');
    fireEvent.click(screen.getByTestId('passkeys.recovery-new.cancel'));
    expect(screen.getByTestId('passkeys.recovery-new')).toHaveAttribute('data-step', 'idle');
    expect(hub.log.some((entry) => entry.target === '/v1/me/recovery/begin')).toBe(false);
  });
});
