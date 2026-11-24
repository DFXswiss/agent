import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { signIn } from '../../src/core/account/login';
import { register } from '../../src/core/account/register';
import { section } from '../../src/ui/settings/sections/language';
import { HubFailure, ORIGIN } from '../core/account/hub';
import { renderApp } from './harness';

async function settingsPage() {
  const harness = await renderApp('/settings', {}, async (made) => {
    const registration = await register(made.app.anon, made.auth, made.hub.addInvite());
    await made.app.begin(await signIn({ anon: made.app.anon, credentials: made.auth, origin: ORIGIN }));
    await made.app.unlock(registration.identity);
  });
  await screen.findByTestId('settings.page');
  return harness;
}

describe('settings', () => {
  it('registers the language section', () => {
    expect(section).toMatchObject({ id: 'language', titleKey: 'settings.language.title' });
  });

  it('saves the language and switches the interface', async () => {
    const harness = await settingsPage();
    fireEvent.change(screen.getByTestId('settings.language.select'), { target: { value: 'de' } });
    expect(await screen.findByTestId('settings.language.saved')).toHaveTextContent('Gespeichert');
    expect(document.documentElement.lang).toBe('de');
    expect(harness.app.store.getState().session.locale).toBe('de');
  });

  it('reports a refused save', async () => {
    const harness = await settingsPage();
    harness.hub.override.set('PUT /v1/me/settings', new HubFailure(503, 'unavailable'));
    fireEvent.change(screen.getByTestId('settings.language.select'), { target: { value: 'de' } });
    expect(await screen.findByTestId('settings.language.error')).toHaveTextContent('Service unavailable');
  });

  it('signs out to the sign-in page', async () => {
    const harness = await settingsPage();
    fireEvent.click(screen.getByTestId('settings.sign-out'));
    await waitFor(() => expect(harness.router.state.location.pathname).toBe('/login'));
    expect(await harness.persist.loadSession()).toBeNull();
  });
});
