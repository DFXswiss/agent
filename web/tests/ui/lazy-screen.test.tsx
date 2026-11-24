// U-WEB-ui-lazy-screen (06-web.md section 12.2, HOT-16): a screen loaded on first use shows its frame at once – also on
// the first switch to it – then the screen; a chunk that cannot be loaded shows "page could not be loaded" with a reload
// inside the shell; any other error reaches the route's error page; the tests load every screen up front.
import { fireEvent, render, screen } from '@testing-library/react';
import type { ComponentType, ReactElement } from 'react';
import { createMemoryRouter, Link, Outlet, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChunkUnavailable } from '../../src/core/assets/integrity';
import { createI18n } from '../../src/core/i18n';
import { I18nProvider } from '../../src/ui/i18n';
import { lazyScreen, preloadScreens, ScreenUnavailable } from '../../src/ui/lazy/screen';
import { PROVIDER_FUTURE, ROUTER_FUTURE } from '../../src/ui/router';

afterEach(() => vi.restoreAllMocks());

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** The lazy screen at `/b` inside a shell, with a start page at `/` that links to it. */
function mount(Lazy: ComponentType, { at = '/b', locale = 'en' as 'en' | 'de' } = {}) {
  const router = createMemoryRouter(
    [
      {
        element: (
          <>
            <p>shell</p>
            <Outlet />
          </>
        ),
        errorElement: <p>error page</p>,
        children: [
          {
            path: '/',
            element: (
              <Link to="/b" data-testid="to-b">
                start
              </Link>
            ),
          },
          { path: '/b', element: <Lazy /> },
        ],
      },
    ],
    { initialEntries: [at], future: ROUTER_FUTURE },
  );
  return render(
    <I18nProvider value={createI18n(locale)}>
      <RouterProvider router={router} future={PROVIDER_FUTURE} />
    </I18nProvider>,
  );
}

const demo = () => ({ Demo: (): ReactElement => <p>demo screen</p> });

describe('lazy screens', () => {
  it('shows the frame of the screen at once, then the screen', async () => {
    const chunk = deferred<ReturnType<typeof demo>>();
    mount(
      lazyScreen(() => chunk.promise, 'Demo'),
      { locale: 'de' },
    );
    const frame = screen.getByTestId('screen.loading');
    expect(frame).toHaveAttribute('aria-busy', 'true');
    expect(frame).toHaveTextContent('Wird geladen…');
    expect(screen.getByText('shell')).toBeTruthy();
    chunk.resolve(demo());
    expect(await screen.findByText('demo screen')).toBeTruthy();
    expect(screen.queryByTestId('screen.loading')).toBeNull();
  });

  it('shows the frame on the first switch to the screen', async () => {
    const chunk = deferred<ReturnType<typeof demo>>();
    mount(
      lazyScreen(() => chunk.promise, 'Demo'),
      { at: '/' },
    );
    fireEvent.click(screen.getByTestId('to-b'));
    expect(await screen.findByTestId('screen.loading')).toBeTruthy();
    expect(screen.queryByTestId('to-b')).toBeNull();
    chunk.resolve(demo());
    expect(await screen.findByText('demo screen')).toBeTruthy();
  });

  it('loads the chunk once and renders a loaded screen at once', async () => {
    const load = vi.fn(() => Promise.resolve(demo()));
    const Demo = lazyScreen(load, 'Demo');
    await preloadScreens();
    mount(Demo);
    expect(screen.getByText('demo screen')).toBeTruthy();
    expect(screen.queryByTestId('screen.loading')).toBeNull();
    await preloadScreens();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('shows "page could not be loaded" inside the shell when the chunk cannot be loaded', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined); // React reports the caught error
    const load = vi.fn(() => Promise.reject(new ChunkUnavailable('integrity check failed for /assets/Demo-x.js')));
    mount(lazyScreen(load, 'Demo'));
    expect(screen.getByTestId('screen.loading')).toBeTruthy();
    expect(await screen.findByTestId('screen.unavailable')).toBeTruthy();
    expect(screen.getByTestId('screen-unavailable.title')).toHaveTextContent('This page could not be loaded');
    expect(screen.getByTestId('error.reload')).toHaveTextContent('Reload');
    expect(screen.getByText('shell')).toBeTruthy();
    expect(screen.queryByText('error page')).toBeNull();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('passes any other error on to the error page', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    mount(lazyScreen(() => Promise.reject(new Error('boom')), 'Demo'));
    expect(await screen.findByText('error page')).toBeTruthy();
    expect(screen.queryByTestId('screen.unavailable')).toBeNull();
  });

  it('reloads the page from "page could not be loaded"', () => {
    const onReload = vi.fn();
    render(
      <I18nProvider value={createI18n('de')}>
        <ScreenUnavailable onReload={onReload} />
      </I18nProvider>,
    );
    expect(screen.getByTestId('screen-unavailable.title')).toHaveTextContent('Diese Seite konnte nicht geladen werden');
    fireEvent.click(screen.getByTestId('error.reload'));
    expect(onReload).toHaveBeenCalledOnce();
  });

  it('reloads the document by default', () => {
    render(
      <I18nProvider value={createI18n('en')}>
        <ScreenUnavailable />
      </I18nProvider>,
    );
    // jsdom cannot navigate; as for the error page (tests/ui/app.test.tsx), the default handler runs without failing
    expect(() => fireEvent.click(screen.getByTestId('error.reload'))).not.toThrow();
  });
});
