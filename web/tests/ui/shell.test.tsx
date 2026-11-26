import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { signIn } from '../../src/core/account/login';
import { register } from '../../src/core/account/register';
import { preparePrivateOrg } from '../../src/core/account/genesis';
import { CryptoError } from '../../src/core/crypto/errors';
import { HubError } from '../../src/core/net/signedFetch';
import { createStore } from '../../src/core/store';
import { noteSession, setLiveState, setOnline } from '../../src/core/store/slices/connection';
import { blockOrg } from '../../src/core/store/slices/orgs';
import { boot } from '../../src/ui/app/boot';
import { useAppEnv } from '../../src/ui/shell/context';
import { errorCode } from '../../src/ui/shell/errors';
import { loginTarget, needsUnlock, safeNext } from '../../src/ui/shell/guard';
import { navTarget } from '../../src/ui/shell/Nav';
import { browserSupported, type SupportScope } from '../../src/ui/shell/support';
import { FakeAuthenticator } from '../core/account/authenticator';
import { CONFIG, ORIGIN, SimHub } from '../core/account/hub';
import { type makeApp, renderApp } from './harness';

type Made = Awaited<ReturnType<typeof makeApp>>;

/** Registration, sign-in and a private org through the real flows; the app ends unlocked. */
async function onboard(made: Made, options: { org?: boolean } = {}) {
  const invite = made.hub.addInvite();
  const registration = await register(made.app.anon, made.auth, invite);
  const login = await signIn({ anon: made.app.anon, credentials: made.auth, origin: ORIGIN });
  await made.app.begin(login);
  await made.app.unlock(registration.identity);
  if (options.org === false) return { registration, org: null };
  const prepared = await preparePrivateOrg({
    userId: registration.userId,
    identity: registration.identity,
    displayName: 'Ada',
    locale: 'en',
    platformInviteId: await made.hub.idOf(invite),
  });
  await made.app.client.request('POST', '/v1/orgs', { body: prepared.body });
  await made.app.installOrg(prepared);
  return { registration, org: prepared.org };
}

describe('support', () => {
  const subtle = { generateKey: vi.fn(async () => ({}) as CryptoKey) } as unknown as SubtleCrypto;
  const full: SupportScope = { PublicKeyCredential: class {}, ReadableStream: class {}, crypto: { subtle } };

  it('needs WebAuthn, streams, X25519 and Ed25519', async () => {
    expect(await browserSupported(full)).toBe(true);
    expect(await browserSupported({ ...full, PublicKeyCredential: undefined })).toBe(false);
    expect(await browserSupported({ ...full, ReadableStream: undefined })).toBe(false);
    expect(await browserSupported({ ...full, crypto: {} })).toBe(false);
    const { crypto: _crypto, ...withoutCrypto } = full;
    expect(await browserSupported(withoutCrypto)).toBe(false);
    const refusing = (name: string) => ({
      generateKey: async (algorithm: { name: string }) => {
        if (algorithm.name === name) throw new Error('unsupported');
        return {} as CryptoKey;
      },
    });
    expect(await browserSupported({ ...full, crypto: { subtle: refusing('X25519') as never } })).toBe(false);
    expect(await browserSupported({ ...full, crypto: { subtle: refusing('Ed25519') as never } })).toBe(false);
    // jsdom has no WebAuthn
    expect(await browserSupported()).toBe(false);
  });
});

describe('helpers', () => {
  it('guards the account routes and keeps sign-in targets local', () => {
    expect(needsUnlock('/o/x')).toBe(true);
    expect(needsUnlock('/settings')).toBe(true);
    expect(needsUnlock('/settingsx')).toBe(false);
    expect(needsUnlock('/notifications')).toBe(true);
    expect(needsUnlock('/platform/invites')).toBe(true);
    expect(needsUnlock('/support')).toBe(true);
    expect(needsUnlock('/support/o/w')).toBe(true);
    expect(needsUnlock('/login')).toBe(false);
    expect(loginTarget('/o/x', '?a=1')).toBe('/login?next=%2Fo%2Fx%3Fa%3D1');
    expect(safeNext('/o/x')).toBe('/o/x');
    expect(safeNext('//evil.example')).toBeNull();
    expect(safeNext('/\\evil.example')).toBeNull();
    expect(safeNext('https://evil.example')).toBeNull();
    expect(safeNext(null)).toBeNull();
  });

  it('maps errors onto catalog codes', () => {
    expect(errorCode(new CryptoError('decrypt_failed'))).toBe('decrypt_failed');
    expect(errorCode(new HubError('forbidden', 403))).toBe('forbidden');
    expect(errorCode(new TypeError('x'))).toBe('unavailable');
  });

  it('fills the org into navigation targets', () => {
    const entry = { id: 'home', order: 1, to: '/o/:org', labelKey: 'x' };
    expect(navTarget(entry, 'a b')).toBe('/o/a%20b');
    expect(navTarget(entry, null)).toBeNull();
    expect(navTarget({ ...entry, to: '/notifications' }, null)).toBe('/notifications');
  });

  it('needs the app environment', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(() => renderHook(() => useAppEnv())).toThrow('useAppEnv outside <App>');
  });
});

describe('boot', () => {
  const base = {
    fetch: async () => Response.json(CONFIG),
    rootKey: CONFIG.platform_root_key,
    origin: ORIGIN,
    languages: ['en'],
    credentials: new FakeAuthenticator(),
    storage: null,
  };

  it('shows the unsupported-browser page without the platform features or without IndexedDB', async () => {
    const container = document.createElement('div');
    await boot({ ...base, container, indexedDB: new IDBFactory(), missing: async () => ['webauthn', 'x25519'] });
    await waitFor(() => expect(container.textContent).toContain('This browser is not supported'));
    const broken = {
      open: () => {
        throw new Error('blocked');
      },
    } as unknown as IDBFactory;
    const second = document.createElement('div');
    await boot({ ...base, container: second, indexedDB: broken, missing: async () => [] });
    await waitFor(() => expect(second.textContent).toContain('This browser is not supported'));
  });

  it('checks the browser itself by default', async () => {
    const container = document.createElement('div');
    await boot({ ...base, container, indexedDB: new IDBFactory() });
    await waitFor(() => expect(container.textContent).toContain('This browser is not supported'));
  });

  it('restores into the given store', async () => {
    const store = createStore();
    const container = document.createElement('div');
    window.history.replaceState(null, '', '/login');
    await boot({ ...base, container, indexedDB: new IDBFactory(), missing: async () => [], store });
    expect(store.getState().session.phase).toBe('anonymous');
    expect(store.getState().config.config).toEqual(CONFIG);
    await waitFor(() => expect(container.textContent).toContain('Sign in with passkey'));
  });
});

describe('layout', () => {
  it('sends anonymous visitors of account routes to the sign-in and shows the sign-in link', async () => {
    const harness = await renderApp('/settings');
    await screen.findByTestId('login.page');
    expect(harness.router.state.location.search).toBe('?next=%2Fsettings');
    expect(screen.getByTestId('shell.signin')).toBeInTheDocument();
    expect(screen.queryByTestId('shell.connection.live')).toBeNull();
  });

  it('activates the org of the route and shows the blocking state of a blocked org', async () => {
    let org = '';
    const harness = await renderApp('/settings', {}, async (made) => {
      org = (await onboard(made)).org!;
    });
    await screen.findByTestId('settings.page');
    await act(async () => harness.router.navigate(`/o/${org}/x`));
    await waitFor(() => expect(harness.live).toHaveBeenCalledOnce());
    expect(harness.app.store.getState().session.activeOrg).toBe(org);
    act(() =>
      blockOrg(harness.app.store, org, { code: 'log_fork', stored: { seq: 1, hash: 'h' }, served: null, lh: 7 }),
    );
    expect(screen.getByTestId('shell.blocked.evidence')).toHaveTextContent(
      'stored head 1:h, served head none, event head 7',
    );
    expect(screen.getByTestId('shell.blocked.code')).toHaveTextContent('log_fork');
    act(() =>
      blockOrg(harness.app.store, 'x', { code: 'log_rollback', stored: null, served: { seq: 2, hash: 'g' }, lh: null }),
    );
  });

  it('makes the landing org the active one on a page outside an org opened directly (KOR-68)', async () => {
    let org = '';
    const harness = await renderApp('/settings', {}, async (made) => {
      org = (await onboard(made)).org!;
    });
    await screen.findByTestId('settings.page');
    await waitFor(() => expect(harness.app.store.getState().session.activeOrg).toBe(org));
    await waitFor(() => expect(harness.live).toHaveBeenCalledOnce());
  });

  it('asks to reload instead of showing evidence for an org written by a newer version (ADR-060)', async () => {
    let org = '';
    const harness = await renderApp('/settings', {}, async (made) => {
      org = (await onboard(made)).org!;
    });
    await screen.findByTestId('settings.page');
    await act(async () => harness.router.navigate(`/o/${org}/x`));
    act(() =>
      blockOrg(harness.app.store, org, { code: 'log_type', stored: { seq: 1, hash: 'h' }, served: null, lh: null }),
    );
    expect(screen.getByTestId('shell.blocked.outdated')).toHaveTextContent('Reload the page to continue.');
    expect(screen.getByTestId('shell.blocked.title')).toHaveTextContent('A newer version is available');
    expect(screen.getByTestId('shell.blocked.code')).toHaveTextContent('log_type');
    expect(screen.queryByTestId('shell.blocked.evidence')).toBeNull();
  });

  it('follows the browser connectivity and shows the channel state', async () => {
    const harness = await renderApp('/settings', {}, async (made) => {
      await onboard(made);
    });
    await screen.findByTestId('settings.page');
    const store = harness.app.store;
    act(() => setLiveState(store, { status: 'live', retryAt: null, attempt: 0, error: null }));
    expect(screen.getByTestId('shell.connection.live')).toHaveTextContent('Live');
    act(() => setLiveState(store, { status: 'connecting', retryAt: null, attempt: 0, error: null }));
    expect(screen.getByTestId('shell.connection.connecting')).toBeInTheDocument();
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    act(() => setLiveState(store, { status: 'reconnecting', retryAt: Date.now() + 3000, attempt: 1, error: null }));
    expect(screen.getByTestId('shell.connection.reconnecting')).toHaveTextContent('(3 s)');
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByTestId('shell.connection.reconnecting')).toHaveTextContent('(2 s)');
    vi.useRealTimers();
    act(() => setLiveState(store, { status: 'idle', retryAt: null, attempt: 0, error: null }));
    expect(screen.queryByTestId('shell.connection.connecting')).toBeNull();
    Object.defineProperty(navigator, 'onLine', { value: false, configurable: true });
    act(() => {
      window.dispatchEvent(new Event('offline'));
    });
    expect(screen.getByTestId('shell.connection.offline')).toBeInTheDocument();
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
    act(() => {
      window.dispatchEvent(new Event('online'));
    });
    expect(screen.queryByTestId('shell.connection.offline')).toBeNull();
    act(() => setOnline(store, true));
    harness.view.unmount();
  });

  it('renders discovered header items and navigation while unlocked', async () => {
    const { Header } = await import('../../src/ui/shell/Header');
    const { Nav } = await import('../../src/ui/shell/Nav');
    const { AppEnvProvider } = await import('../../src/ui/shell/context');
    const { MemoryRouter } = await import('react-router-dom');
    const harness = await renderApp('/login');
    const env = { app: harness.app, credentials: harness.auth, origin: ORIGIN };
    const items = [{ id: 'bell', order: 1, Component: () => <span data-testid="bell">bell</span> }];
    const entries = [
      { id: 'home', order: 1, to: '/o/:org', labelKey: 'shell.brand' },
      { id: 'notes', order: 2, to: '/notifications', labelKey: 'shell.settings' },
    ];
    const page = render(
      <AppEnvProvider value={env}>
        <MemoryRouter>
          <Header items={items} />
          <Nav entries={entries} />
          <Nav entries={[entries[0]!]} />
        </MemoryRouter>
      </AppEnvProvider>,
    );
    expect(page.queryByTestId('bell')).toBeNull();
    expect(page.getAllByTestId('shell.nav')).toHaveLength(1);
    expect(page.queryByTestId('shell.nav.home')).toBeNull();
    act(() => {
      harness.app.store.update('session', (session) => ({ ...session, phase: 'unlocked', activeOrg: 'o1' }));
    });
    expect(page.getByTestId('bell')).toBeInTheDocument();
    expect(page.getAllByTestId('shell.nav.home')[0]).toHaveAttribute('href', '/o/o1');
  });

  it('starts at the last org, at the settings without org, and at the sign-in when locked', async () => {
    let org = '';
    const harness = await renderApp('/', {}, async (made) => {
      org = (await onboard(made)).org!;
    });
    await waitFor(() => expect(harness.router.state.location.pathname).toBe(`/o/${org}`));
    const lonely = await renderApp('/', {}, async (made) => {
      await onboard(made, { org: false });
    });
    await waitFor(() => expect(lonely.router.state.location.pathname).toBe('/settings'));
    expect(await screen.findByTestId('settings.no-org')).toBeInTheDocument();
  });
});

describe('renewal notice', () => {
  it('renews the session key in place, reports cancellation and errors', async () => {
    const auth = new FakeAuthenticator();
    const harness = await renderApp('/settings', { auth }, async (made) => {
      await onboard(made);
    });
    await screen.findByTestId('settings.page');
    const store = harness.app.store;
    act(() => noteSession(store, 'expiring'));
    expect(screen.getByTestId('shell.renew.text')).toHaveTextContent('ends in a few minutes');
    auth.refuse = 'NotAllowedError';
    fireEvent.click(screen.getByTestId('shell.renew.submit'));
    expect(await screen.findByTestId('shell.renew.message')).toHaveTextContent('Sign-in was cancelled');
    harness.hub.override.set('POST /v1/passkeys/login/begin', () => 'x');
    fireEvent.click(screen.getByTestId('shell.renew.submit'));
    await waitFor(() => expect(screen.getByTestId('shell.renew.message')).toHaveTextContent('Invalid header'));
    harness.hub.override.delete('POST /v1/passkeys/login/begin');
    const before = store.getState().me.session!.keyId;
    fireEvent.click(screen.getByTestId('shell.renew.submit'));
    await waitFor(() => expect(screen.queryByTestId('shell.renew')).toBeNull());
    expect(store.getState().me.session!.keyId).not.toBe(before);
    expect(store.getState().session.phase).toBe('unlocked');
  });

  it('signs out when the passkey belongs to another account', async () => {
    const auth = new FakeAuthenticator();
    const hub = new SimHub();
    const harness = await renderApp('/settings', { auth, hub }, async (made) => {
      await onboard(made);
    });
    await screen.findByTestId('settings.page');
    await register(harness.app.anon, auth, hub.addInvite());
    act(() => noteSession(harness.app.store, 'expired'));
    expect(screen.getByTestId('shell.renew.text')).toHaveTextContent('Your session has ended.');
    fireEvent.click(screen.getByTestId('shell.renew.submit'));
    await waitFor(() => expect(harness.router.state.location.pathname).toBe('/login'));
    expect(harness.app.store.getState().session.phase).toBe('anonymous');
  });
});
