// Router (06-web.md section 2.4) built from the screens discovered at `src/ui/screens/<area>/routes.tsx` (each
// exports `routes: RouteObject[]`, ADR-027 section 4). The optional app shell `src/ui/shell/layout.tsx` (export
// `Layout`, renders an `<Outlet />`) wraps every screen.
import type { ComponentType } from 'react';
import { createBrowserRouter, Outlet, type RouteObject } from 'react-router-dom';
import { collect, hasShape, isRecord, type ModuleMap } from '../core/registry';
import { RouteError } from './app/RouteError';

const screenModules: ModuleMap = import.meta.glob('./screens/*/routes.tsx', { eager: true });
const layoutModules: ModuleMap = import.meta.glob('./shell/layout.tsx', { eager: true });

const isRouteList = (value: unknown): value is RouteObject[] => Array.isArray(value) && value.every(isRecord);
const isLayout = (value: unknown): value is ComponentType => hasShape({ value }, { value: 'component' });

/** React Router v7 behaviour, opted in now so that the upgrade changes nothing. */
export const ROUTER_FUTURE = {
  v7_relativeSplatPath: true,
  v7_fetcherPersist: true,
  v7_normalizeFormMethod: true,
  v7_partialHydration: true,
  v7_skipActionErrorRevalidation: true,
} as const;
export const PROVIDER_FUTURE = { v7_startTransition: true } as const;

export function screenRoutes(modules: ModuleMap = screenModules): RouteObject[] {
  return collect(modules, 'routes', isRouteList).flatMap((entry) => entry.value);
}

export function appRoutes(screens: ModuleMap = screenModules, layouts: ModuleMap = layoutModules): RouteObject[] {
  const Layout = collect(layouts, 'Layout', isLayout)[0]?.value ?? Outlet;
  return [{ element: <Layout />, errorElement: <RouteError />, children: screenRoutes(screens) }];
}

export function createAppRouter(routes: RouteObject[] = appRoutes()) {
  return createBrowserRouter(routes, { future: ROUTER_FUTURE });
}
