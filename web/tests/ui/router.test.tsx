import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider, type RouteObject } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { appRoutes, createAppRouter, PROVIDER_FUTURE, ROUTER_FUTURE, screenRoutes } from '../../src/ui/router';
import { globPatterns } from '../helpers';

const fixtureScreens = import.meta.glob('../fixtures/registry/screens/*/routes.tsx', { eager: true });
const fixtureLayouts = import.meta.glob('../fixtures/registry/shell/layout.tsx', { eager: true });

function renderAt(path: string, routes: RouteObject[]) {
  const router = createMemoryRouter(routes, { initialEntries: [path], future: ROUTER_FUTURE });
  return render(<RouterProvider router={router} future={PROVIDER_FUTURE} />);
}

describe('router registration (src/ui/screens/<area>/routes.tsx)', () => {
  it('uses the specified globs', () => {
    expect(globPatterns('src/ui/router.tsx')).toEqual(["'./screens/*/routes.tsx'", "'./shell/layout.tsx'"]);
  });

  it('discovers the screens of the packages, among them the not-found screen', () => {
    expect(screenRoutes().map((route) => route.path)).toEqual(
      expect.arrayContaining(['*', '/', '/login', '/invite', '/settings']),
    );
  });

  it('discovers fixture screens with the same glob shape', () => {
    expect(screenRoutes(fixtureScreens).map((route) => route.path)).toEqual(['/demo']);
  });

  it('refuses a routes export that is not a list of routes', () => {
    expect(() => screenRoutes({ './screens/x/routes.tsx': { routes: [1] } })).toThrow('invalid export "routes"');
  });

  it('renders the not-found screen for unknown paths without a layout', () => {
    renderAt('/nowhere', appRoutes(undefined, {}));
    expect(screen.getByTestId('notfound.title')).toHaveTextContent('Page not found');
    expect(screen.queryByTestId('demo.layout')).toBeNull();
  });

  it('wraps every screen in the shell layout when one is registered', () => {
    renderAt(
      '/demo',
      appRoutes(
        { ...fixtureScreens, ...import.meta.glob('../../src/ui/screens/*/routes.tsx', { eager: true }) },
        fixtureLayouts,
      ),
    );
    expect(screen.getByTestId('demo.layout')).toHaveTextContent('demo screen');
  });

  it('shows the generic error page when a screen throws', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const quiet = (event: ErrorEvent) => event.preventDefault();
    window.addEventListener('error', quiet);
    const Broken = () => {
      throw new Error('boom');
    };
    renderAt(
      '/broken',
      appRoutes({ './screens/x/routes.tsx': { routes: [{ path: '/broken', element: <Broken /> }] } }, {}),
    );
    expect(screen.getByTestId('error.title')).toHaveTextContent('Unexpected error');
    expect(screen.getByTestId('error.code')).toHaveTextContent('ui_error');
    window.removeEventListener('error', quiet);
  });

  it('creates a browser router from the discovered routes', () => {
    window.history.replaceState(null, '', '/somewhere');
    const router = createAppRouter();
    expect(router.state.matches.at(-1)?.route.path).toBe('*');
    router.dispose();
  });
});
