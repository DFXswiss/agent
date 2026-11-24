// U-WEB-ui-responsive: the phone layout and the error pages of 06-web.md section 12 – the bottom navigation with
// "Tasks" and "More", the sidebar workgroups, the on-screen keyboard (visual viewport), the skip link and the
// connection announcements, the unsupported-browser page with what is missing, and the error pages of a screen
// (not available, session expired with a new sign-in, other load errors with "Try again").
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { useRef } from 'react';
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { CeremonyCancelled } from '../../src/core/account/ceremony';
import { preparePrivateOrg } from '../../src/core/account/genesis';
import * as login from '../../src/core/account/login';
import { register } from '../../src/core/account/register';
import { createI18n } from '../../src/core/i18n';
import { createStore } from '../../src/core/store';
import { setLiveState } from '../../src/core/store/slices/connection';
import { I18nProvider } from '../../src/ui/i18n';
import { BottomNav } from '../../src/ui/responsive/BottomNav';
import { KEYBOARD_MIN, keyboardInset, useAboveKeyboard, useKeyboardInset } from '../../src/ui/responsive/keyboard';
import { LoadError } from '../../src/ui/screens/errors/pages';
import { PositionTime } from '../../src/ui/terminal/TerminalView';
import { chunkAt } from '../../src/core/terminal/chunks';
import { Unsupported } from '../../src/ui/screens/errors/Unsupported';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { missingFeatures, type SupportScope } from '../../src/ui/shell/support';
import { ORIGIN } from '../core/account/hub';
import { type makeApp, renderApp } from './harness';

type Made = Awaited<ReturnType<typeof makeApp>>;

/** Registration, sign-in and a private org through the real flows; the app ends unlocked. */
async function onboard(made: Made): Promise<{ org: string; wg: string }> {
  const invite = made.hub.addInvite();
  const registration = await register(made.app.anon, made.auth, invite);
  const signedIn = await login.signIn({ anon: made.app.anon, credentials: made.auth, origin: ORIGIN });
  await made.app.begin(signedIn);
  await made.app.unlock(registration.identity);
  const prepared = await preparePrivateOrg({
    userId: registration.userId,
    identity: registration.identity,
    displayName: 'Ada',
    locale: 'en',
    platformInviteId: await made.hub.idOf(invite),
  });
  await made.app.client.request('POST', '/v1/orgs', { body: prepared.body });
  await made.app.installOrg(prepared);
  return { org: prepared.org, wg: prepared.defaultWg };
}

class FakeViewport extends EventTarget {
  height = 800;
  offsetTop = 0;
}

describe('on-screen keyboard', () => {
  it('measures the covered part of the layout viewport', () => {
    const viewport = new FakeViewport();
    expect(keyboardInset({ innerHeight: 800 })).toBe(0);
    expect(keyboardInset({ innerHeight: 800, visualViewport: null })).toBe(0);
    expect(keyboardInset({ innerHeight: 800, visualViewport: viewport })).toBe(0);
    viewport.height = 800 - KEYBOARD_MIN + 1;
    expect(keyboardInset({ innerHeight: 800, visualViewport: viewport })).toBe(0);
    viewport.height = 500;
    viewport.offsetTop = 20;
    expect(keyboardInset({ innerHeight: 800, visualViewport: viewport })).toBe(280);
  });

  it('follows the visual viewport and keeps a focused composer in view', () => {
    const viewport = new FakeViewport();
    const scope = { innerHeight: 800, visualViewport: viewport };
    const scrolled = vi.fn();
    const { result } = renderHook(() => {
      const box = useRef<HTMLFormElement>(null);
      useAboveKeyboard(box, scope);
      return { inset: useKeyboardInset(scope), box };
    });
    expect(result.current.inset).toBe(0);
    // focus outside the box: nothing scrolls
    const form = document.createElement('form');
    const input = document.createElement('textarea');
    form.append(input);
    form.scrollIntoView = scrolled;
    document.body.append(form);
    (result.current.box as { current: HTMLFormElement | null }).current = form;
    act(() => {
      viewport.height = 400;
      viewport.dispatchEvent(new Event('resize'));
    });
    expect(result.current.inset).toBe(400);
    expect(scrolled).not.toHaveBeenCalled();
    input.focus();
    act(() => {
      viewport.offsetTop = 10;
      viewport.dispatchEvent(new Event('scroll'));
    });
    expect(result.current.inset).toBe(390);
    expect(scrolled).toHaveBeenCalledWith({ block: 'nearest' });
    form.remove();
  });

  it('does nothing without a composer box', () => {
    const viewport = new FakeViewport();
    viewport.height = 300;
    const scope = { innerHeight: 800, visualViewport: viewport };
    const { result } = renderHook(() => {
      const box = useRef<HTMLFormElement>(null);
      useAboveKeyboard(box, scope);
      return useKeyboardInset(scope);
    });
    expect(result.current).toBe(500);
  });

  it('pads the shell and hides the bottom navigation while the keyboard is open', async () => {
    const viewport = new FakeViewport();
    Object.defineProperty(window, 'visualViewport', { value: viewport, configurable: true });
    Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true });
    let ids = { org: '', wg: '' };
    await renderApp('/settings', {}, async (made) => {
      ids = await onboard(made);
    });
    await screen.findByTestId('settings.page');
    act(() => {
      viewport.height = 500;
      viewport.dispatchEvent(new Event('resize'));
    });
    expect(screen.getByTestId('shell.layout')).toHaveAttribute('data-keyboard', '300');
    expect(screen.getByTestId('shell.layout').style.paddingBottom).toBe('300px');
    expect(screen.queryByTestId('shell.bottom')).toBeNull();
    Reflect.deleteProperty(window, 'visualViewport');
    expect(ids.org).not.toBe('');
  });
});

describe('terminal position time', () => {
  it('shows the absolute time of a position when the chunks know it', () => {
    const at = Date.parse('2026-11-22T10:00:01.500Z');
    const chunk = chunkAt(1, { offsetMs: 0, frames: [[1000, 'o', 'x']], cols: 80, rows: 24 }, at);
    const { container, rerender } = render(
      <I18nProvider value={createI18n('en')}>
        <PositionTime chunks={[]} position={0} />
      </I18nProvider>,
    );
    expect(container.innerHTML).toBe('');
    rerender(
      <I18nProvider value={createI18n('en')}>
        <PositionTime chunks={[chunk]} position={500} />
      </I18nProvider>,
    );
    expect(screen.getByTestId('terminal.position.time')).toHaveAttribute('dateTime', '2026-11-22T10:00:01.000Z');
    expect(screen.getByTestId('terminal.position.time').textContent).toMatch(/\d{2}:\d{2}:01\.000$/);
  });
});

describe('bottom navigation', () => {
  it('shows Home, Tasks, Notifications, Monitoring and More with the other entries', async () => {
    let ids = { org: '', wg: '' };
    const harness = await renderApp('/settings', {}, async (made) => {
      ids = await onboard(made);
    });
    await screen.findByTestId('settings.page');
    await act(async () => harness.router.navigate(`/o/${ids.org}/members`));
    const bar = await screen.findByTestId('shell.bottom');
    expect(within(bar).getByTestId('shell.bottom.tasks')).toHaveAttribute('href', `/o/${ids.org}/tasks`);
    expect(within(bar).getByTestId('shell.bottom.home')).toHaveAttribute('href', `/o/${ids.org}`);
    fireEvent.click(within(bar).getByTestId('shell.bottom.more'));
    const sheet = screen.getByTestId('shell.more');
    expect(sheet).toHaveAttribute('role', 'dialog');
    expect(within(sheet).getByTestId('shell.more.members')).toHaveAttribute('aria-current', 'page');
    expect(within(sheet).queryByTestId('shell.more.home')).toBeNull();
    expect(within(sheet).queryByTestId('shell.more.tasks')).toBeNull();
    expect(within(sheet).getByTestId('shell.more.security')).toHaveAttribute('href', '/security');
    fireEvent.click(within(sheet).getByTestId('shell.more.settings'));
    expect(harness.router.state.location.pathname).toBe('/settings');
    expect(screen.queryByTestId('shell.more')).toBeNull();
    fireEvent.click(screen.getByTestId('shell.bottom.more'));
    fireEvent.click(screen.getByTestId('shell.more.close'));
    expect(screen.queryByTestId('shell.more')).toBeNull();
    // the sidebar lists the readable workgroups of the active org and marks the one in view
    await act(async () => harness.router.navigate(`/o/${ids.org}/w/${ids.wg}`));
    const workgroup = await screen.findByTestId('shell.sidebar.workgroup');
    expect(workgroup).toHaveAttribute('aria-current', 'page');
    expect(workgroup).toHaveTextContent(/Default|Unnamed workgroup/);
  });

  it('is not shown without an active org', () => {
    const app = { store: createStore() };
    const { container } = render(
      <AppEnvProvider value={{ app: app as never, credentials: {} as never, origin: ORIGIN }}>
        <MemoryRouter>
          <BottomNav entries={[]} />
        </MemoryRouter>
      </AppEnvProvider>,
    );
    expect(container.innerHTML).toBe('');
  });
});

describe('connection announcements', () => {
  it('announces each state once, not every second of the countdown', async () => {
    const harness = await renderApp('/settings', {}, async (made) => {
      await onboard(made);
    });
    await screen.findByTestId('settings.page');
    act(() => setLiveState(harness.app.store, { status: 'live', retryAt: null, attempt: 0, error: null }));
    const region = screen.getByTestId('shell.connection.announce');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toHaveTextContent('Connection is live');
    act(() =>
      setLiveState(harness.app.store, { status: 'reconnecting', retryAt: Date.now() + 5000, attempt: 1, error: null }),
    );
    expect(screen.getByTestId('shell.connection.announce')).toHaveTextContent('Connection lost, reconnecting');
    expect(screen.getByTestId('shell.skip')).toHaveAttribute('href', '#content');
    expect(document.getElementById('content')).not.toBeNull();
  });
});

describe('unsupported browser', () => {
  const subtle = { generateKey: vi.fn(async () => ({}) as CryptoKey) } as unknown as SubtleCrypto;
  const full: SupportScope = { PublicKeyCredential: class {}, ReadableStream: class {}, crypto: { subtle } };

  it('names what is missing', async () => {
    expect(await missingFeatures(full)).toEqual([]);
    expect(await missingFeatures({ ...full, PublicKeyCredential: undefined, ReadableStream: undefined })).toEqual([
      'webauthn',
      'streams',
    ]);
    expect(await missingFeatures({ ...full, crypto: {} })).toEqual(['x25519', 'ed25519']);
  });

  it('lists the missing features and the supported browsers', () => {
    const { rerender } = render(
      <I18nProvider value={createI18n('de')}>
        <Unsupported missing={['webauthn', 'indexeddb']} />
      </I18nProvider>,
    );
    expect(screen.getByTestId('unsupported.missing')).toHaveTextContent(
      'In diesem Browser fehlt: Passkeys (WebAuthn), IndexedDB-Speicher',
    );
    expect(within(screen.getByTestId('unsupported.browsers')).getAllByRole('listitem')).toHaveLength(4);
    rerender(
      <I18nProvider value={createI18n('en')}>
        <Unsupported missing={[]} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId('unsupported.missing')).toBeNull();
    expect(screen.getByTestId('unsupported.title')).toHaveTextContent('This browser is not supported');
  });
});

describe('error pages of a screen', () => {
  function show(code: string, extra: { userId?: string } = {}) {
    const retry = vi.fn();
    const app = { anon: {}, userId: extra.userId ?? 'u1', begin: vi.fn(async () => undefined), signOut: vi.fn() };
    const router = createMemoryRouter(
      [
        { path: '/x', element: <LoadError code={code} testId="screen.error" retry={retry} /> },
        { path: '*', element: <p data-testid="elsewhere" /> },
      ],
      { initialEntries: ['/x'] },
    );
    render(
      <AppEnvProvider value={{ app: app as never, credentials: {} as never, origin: ORIGIN }}>
        <I18nProvider value={createI18n('en')}>
          <RouterProvider router={router} />
        </I18nProvider>
      </AppEnvProvider>,
    );
    return { retry, app, router };
  }

  it('shows "not available" alike for not found and forbidden', () => {
    for (const code of ['not_found', 'forbidden']) {
      const { router } = show(code);
      expect(screen.getByTestId('screen.error')).toHaveTextContent('This page does not exist or you cannot see it.');
      expect(screen.queryByTestId('not-available.code')).toBeNull();
      fireEvent.click(screen.getByTestId('not-available.home'));
      expect(router.state.location.pathname).toBe('/');
      document.body.innerHTML = '';
    }
  });

  it('shows other errors with their code and "Try again"', () => {
    const { retry } = show('unavailable');
    expect(screen.getByTestId('load-error.title')).toHaveTextContent('Service unavailable');
    expect(screen.getByTestId('load-error.code')).toHaveTextContent('Error code: unavailable');
    fireEvent.click(screen.getByTestId('load-error.retry'));
    expect(retry).toHaveBeenCalledOnce();
  });

  it('signs in again after the session expired and loads the screen again', async () => {
    const signIn = vi.spyOn(login, 'signIn');
    signIn.mockRejectedValueOnce(new CeremonyCancelled());
    const { retry, app } = show('session_expired');
    expect(screen.getByTestId('session-expired.title')).toHaveTextContent('Session expired');
    fireEvent.click(screen.getByTestId('session-expired.renew'));
    expect(await screen.findByTestId('session-expired.message')).toHaveTextContent('cancelled');
    signIn.mockResolvedValueOnce({ userId: 'u1' } as never);
    fireEvent.click(screen.getByTestId('session-expired.renew'));
    await waitFor(() => expect(retry).toHaveBeenCalledOnce());
    expect(app.begin).toHaveBeenCalledOnce();
  });
});
