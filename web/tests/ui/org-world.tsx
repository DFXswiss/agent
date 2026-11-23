// The org world of tests/core/org/world.ts for the screens of WEB-06: an app stub over its store, client and context,
// mounted with a memory router, and verified profile names put straight into the events slice.
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { putEvents } from '../../src/core/store/slices/events';
import { I18nProvider } from '../../src/ui/i18n';
import { AppEnvProvider } from '../../src/ui/shell/context';
import type { OrgWorld } from '../core/org/world';

export interface Mounted {
  readonly router: ReturnType<typeof createMemoryRouter>;
  readonly app: Record<string, unknown>;
}

/** Renders `routes` at `path` with an app stub over the world's store, client and context. */
export function mount(w: OrgWorld, routes: { path: string; element: ReactNode }[], path: string, extra: object = {}) {
  const router = createMemoryRouter([...routes, { path: '*', element: <p data-testid="elsewhere">elsewhere</p> }], {
    initialEntries: [path],
  });
  const app = {
    store: w.store,
    tasks: w.context,
    client: w.client,
    userId: w.me.id,
    config: { hub_key: { id: 'hub-test', public_key: 'A'.repeat(43) } },
    loadMe: vi.fn(async () => ({})),
    installOrg: vi.fn(async () => undefined),
    ...extra,
  };
  render(
    <AppEnvProvider value={{ app: app as never, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return { router, app };
}

/** Puts verified `member.profile`/`wg.profile` names into the events slice (what the pulled profiles give). */
export function names(w: OrgWorld, members: Record<string, string>, workgroups: Record<string, string> = {}) {
  const entries = [
    ...Object.entries(members).map(([id, name]) => ({
      kind: 'member.profile',
      ref: id,
      author: id,
      wg: '',
      body: { display_name: name },
    })),
    ...Object.entries(workgroups).map(([wg, name]) => ({
      kind: 'wg.profile',
      ref: '',
      author: w.me.id,
      wg,
      body: { name },
    })),
  ];
  putEvents(
    w.store,
    w.org,
    entries.map((item, i) => ({
      org: w.org,
      scopeId: w.org,
      scopeSeq: i + 1,
      receivedAt: null,
      event: {},
      status: 'verified' as const,
      code: null,
      header: { kind: item.kind, ref: item.ref, author: item.author, wg: item.wg, id: `e${i}` },
      body: item.body,
    })),
  );
}
