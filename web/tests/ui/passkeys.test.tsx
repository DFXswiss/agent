// The settings section "Passkeys and recovery" in jsdom against the simulated hub (06-web.md sections 3.3 and 3.6):
// adding on this device, another passkey answering the confirmation, refusals, a passkey without PRF, removal with a
// failing reload, the dialog closed with Escape, the device key of this browser when IndexedDB refuses, and the
// recovery code: created or renewed with one "Done" (KOR-01, KOR-22).
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { signIn } from '../../src/core/account/login';
import { b64uEncode } from '../../src/core/encoding/b64u';
import { register } from '../../src/core/account/register';
import { parseRecoveryCode, recoveryId } from '../../src/core/crypto/recovery';
import { section } from '../../src/ui/settings/sections/passkeys';
import { deviceKeys } from '../../src/ui/prf-missing/device';
import { FakeAuthenticator, giveRecoveryCode } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

afterEach(() => vi.unstubAllGlobals());

async function settingsPage(options: { factory?: IDBFactory; withCode?: boolean } = {}) {
  if (options.factory) vi.stubGlobal('indexedDB', options.factory);
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  let userId = '';
  const harness = await renderApp('/settings', { hub, auth }, async (made) => {
    const registration = await register(made.app.anon, auth, hub.addInvite());
    userId = registration.userId;
    if (options.withCode) await giveRecoveryCode(hub, registration);
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
    // a passkey without PRF is not accepted: removed again, the PRF message, the session of this browser stays
    auth.prf = false;
    fireEvent.click(screen.getByTestId('passkeys.add.confirm'));
    const message = await screen.findByTestId('prf-required');
    expect(message).toHaveAttribute('data-context', 'add');
    expect(message).toHaveTextContent('The passkey was not added.');
    expect(screen.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'prf_required');
    expect(hub.users.get(userId)!.credentials.size).toBe(1);
    expect(await screen.findAllByTestId('passkeys.item')).toHaveLength(1);
    expect([...hub.users.get(userId)!.bundles.keys()].filter((slot) => slot.startsWith('passkey:'))).toHaveLength(0);
    // another try with a passkey that has PRF; ending the previous session may fail
    fireEvent.click(screen.getByTestId('prf-required.retry'));
    auth.prf = true;
    hub.override.set('POST /v1/session/end', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('passkeys.add.this-device'));
    fireEvent.click(await screen.findByTestId('passkeys.add.confirm'));
    expect(await screen.findByTestId('passkeys.add.notice')).toHaveTextContent('The passkey was added');
    expect(await screen.findAllByTestId('passkeys.item')).toHaveLength(2);
  });

  it('says so when a refused passkey cannot be removed from the account', async () => {
    const { hub, auth, userId } = await settingsPage();
    auth.prf = false;
    fireEvent.click(screen.getByTestId('passkeys.add.this-device'));
    await waitFor(() => expect(screen.getByTestId('passkeys.add')).toHaveAttribute('data-step', 'confirm'));
    const id = b64uEncode(auth.credentials.at(-1)!.id);
    hub.override.set(`DELETE /v1/passkeys/${id}`, new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('passkeys.add.confirm'));
    expect(await screen.findByTestId('prf-required.not-removed')).toHaveTextContent('could not be removed');
    expect(hub.users.get(userId)!.credentials.has(id)).toBe(true);
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

describe('the recovery code in the settings (KOR-01, KOR-22)', () => {
  it('says quietly that none exists, creates one with one "Done" and a passkey gesture', async () => {
    const { hub, auth, userId } = await settingsPage();
    const user = hub.users.get(userId)!;
    const before = user.recoveryId;
    await waitFor(() => expect(screen.getByTestId('passkeys.recovery')).toHaveAttribute('data-status', 'none'));
    expect(screen.getByTestId('passkeys.recovery.none')).toHaveTextContent('You have no recovery code yet.');
    expect(screen.queryByTestId('shell.recovery-banner')).toBeNull();
    fireEvent.click(screen.getByTestId('passkeys.recovery.create'));
    const shown = (await screen.findByTestId('recovery-code.code')).textContent!.trim();
    expect(shown).toMatch(/^[A-Z2-7]{5}(-[A-Z2-7]{5}){3}-[A-Z2-7]{6}$/);
    // nothing to retype, one button
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByTestId('recovery-code.continue')).toHaveTextContent('Done');
    expect(screen.getByTestId('recovery-code.explain')).not.toHaveTextContent('current code');
    // a cancelled gesture keeps the code and changes nothing
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('passkeys.recovery.notice')).toHaveTextContent('cancelled');
    expect(user.recoveryId).toBe(before);
    expect(screen.getByTestId('recovery-code.code')).toHaveTextContent(shown);
    // a refusal of the hub is reported
    hub.override.set('PUT /v1/me/bundles/recovery', new HubFailure(503, 'unavailable'));
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    await waitFor(() =>
      expect(screen.getByTestId('passkeys.recovery.notice')).toHaveTextContent('Service unavailable'),
    );
    hub.override.delete('PUT /v1/me/bundles/recovery');
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('passkeys.recovery.done')).toHaveTextContent('Your recovery code is saved.');
    expect(screen.getByTestId('passkeys.recovery')).toHaveAttribute('data-step', 'idle');
    expect(screen.getByTestId('passkeys.recovery')).toHaveAttribute('data-status', 'set');
    expect(screen.queryByTestId('passkeys.recovery.none')).toBeNull();
    expect(screen.getByTestId('passkeys.recovery.create')).toHaveTextContent('Create a new recovery code');
    // the shown code is the one the hub now knows
    expect(user.recoveryId).toBe(await recoveryId(parseRecoveryCode(shown)));
  });

  it('renews an existing code; the old one stays valid until the new one is saved', async () => {
    const { hub, userId } = await settingsPage({ withCode: true });
    await waitFor(() => expect(screen.getByTestId('passkeys.recovery')).toHaveAttribute('data-status', 'set'));
    expect(screen.queryByTestId('passkeys.recovery.none')).toBeNull();
    fireEvent.click(screen.getByTestId('passkeys.recovery.create'));
    await screen.findByTestId('recovery-code.code');
    expect(screen.getByTestId('recovery-code.explain')).toHaveTextContent('Until then your current code stays valid.');
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    expect(await screen.findByTestId('passkeys.recovery.done')).toHaveTextContent('The old one no longer works.');
    expect(hub.users.get(userId)!.recoveryId).toBeTruthy();
  });

  it('shows no hint when the slot cannot be read, and can be cancelled before anything is sent', async () => {
    const hub = new SimHub();
    hub.override.set('GET /v1/me/bundles/recovery', new HubFailure(503, 'unavailable'));
    const auth = new FakeAuthenticator();
    await renderApp('/settings', { hub, auth }, async (made) => {
      const registration = await register(made.app.anon, auth, hub.addInvite());
      await made.app.begin(await signIn({ anon: made.app.anon, credentials: auth, origin: ORIGIN }));
      await made.app.unlock(registration.identity);
    });
    await waitFor(() => expect(screen.getByTestId('passkeys.recovery')).toHaveAttribute('data-status', 'unknown'));
    expect(screen.queryByTestId('passkeys.recovery.none')).toBeNull();
    // a code not known to exist is never called "current"
    expect(screen.getByTestId('passkeys.recovery.create')).toHaveTextContent('Create recovery code');
    fireEvent.click(screen.getByTestId('passkeys.recovery.create'));
    await screen.findByTestId('recovery-code.code');
    expect(screen.getByTestId('recovery-code.explain')).not.toHaveTextContent('current code');
    fireEvent.click(screen.getByTestId('passkeys.recovery.cancel'));
    expect(screen.getByTestId('passkeys.recovery')).toHaveAttribute('data-step', 'idle');
    expect(hub.log.some((entry) => entry.target === '/v1/me/recovery/begin')).toBe(false);
  });
});
