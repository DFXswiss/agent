// U-WEB-ui-lazy-screen (06-web.md section 12.2, HOT-16): a screen loaded on first use shows its frame at once, then the
// screen; a chunk that cannot be loaded reaches the route's error page; the tests load every screen up front.
import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChunkUnavailable } from '../../src/core/assets/integrity';
import { createI18n } from '../../src/core/i18n';
import { I18nProvider } from '../../src/ui/i18n';
import { lazyScreen, preloadScreens } from '../../src/ui/lazy/screen';
import { PROVIDER_FUTURE, ROUTER_FUTURE } from '../../src/ui/router';

afterEach(() => vi.restoreAllMocks());

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function mount(element: React.ReactElement, locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter([{ path: '/', element, errorElement: <p>error page</p> }], {
    future: ROUTER_FUTURE,
  });
  return render(
    <I18nProvider value={createI18n(locale)}>
      <RouterProvider router={router} future={PROVIDER_FUTURE} />
    </I18nProvider>,
  );
}

describe('lazy screens', () => {
  it('shows the frame of the screen at once, then the screen', async () => {
    const chunk = deferred<{ Demo: () => React.ReactElement }>();
    const Demo = lazyScreen(() => chunk.promise, 'Demo');
    mount(<Demo />, 'de');
    const frame = screen.getByTestId('screen.loading');
    expect(frame).toHaveAttribute('aria-busy', 'true');
    expect(frame).toHaveTextContent('Wird geladen…');
    chunk.resolve({ Demo: () => <p>demo screen</p> });
    expect(await screen.findByText('demo screen')).toBeTruthy();
    expect(screen.queryByTestId('screen.loading')).toBeNull();
  });

  it('loads the chunk once and renders a loaded screen at once', async () => {
    const load = vi.fn(() => Promise.resolve({ Demo: () => <p>demo screen</p> }));
    const Demo = lazyScreen(load, 'Demo');
    await preloadScreens();
    mount(<Demo />);
    expect(screen.getByText('demo screen')).toBeTruthy();
    expect(screen.queryByTestId('screen.loading')).toBeNull();
    await preloadScreens();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('passes a chunk that cannot be loaded on to the error page', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined); // React reports the caught error
    const load = vi.fn(() => Promise.reject(new ChunkUnavailable('integrity check failed for /assets/Demo-x.js')));
    const Demo = lazyScreen(load, 'Demo');
    mount(<Demo />);
    expect(screen.getByTestId('screen.loading')).toBeTruthy();
    expect(await screen.findByText('error page')).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(1);
  });
});
