// Shared setup of the chat, sessions and terminal screen tests: the core task world (verified org, keys, scriptable
// client) mounted in a memory router with the app environment, and builders for verified `events` entries.
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import type { EventEntry } from '../../src/core/store/slices/events';
import type { TerminalContext } from '../../src/core/terminal/watch';
import { I18nProvider } from '../../src/ui/i18n';
import { AppEnvProvider } from '../../src/ui/shell/context';
import type { TaskWorld } from '../core/tasks/world';

export const TASK = '0190f000-0000-7000-8000-000000000001';
export const MAIN = '0190f000-0000-7000-8000-0000000000a1';

export interface Mounted {
  readonly router: ReturnType<typeof createMemoryRouter>;
  readonly terminal: Omit<TerminalContext, 'watch' | 'read'> & {
    watch: ReturnType<typeof vi.fn>;
    read: ReturnType<typeof vi.fn<TerminalContext['read']>>;
  };
}

export function mount(
  t: TaskWorld,
  routes: { path: string; element: ReactNode }[],
  path: string,
  locale: 'en' | 'de' = 'en',
): Mounted {
  const router = createMemoryRouter([...routes, { path: '*', element: <p data-testid="elsewhere">elsewhere</p> }], {
    initialEntries: [path],
  });
  const terminal = {
    store: t.store,
    client: t.client,
    now: () => t.clock.now,
    read: vi.fn<TerminalContext['read']>(async () => []),
    watch: vi.fn(() => () => undefined),
  };
  const app = { store: t.store, tasks: t.context, terminal } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n(locale)}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return { router, terminal };
}

let next = 100;

/** A verified event of the task in the world's workgroup. */
export function verified(
  t: TaskWorld,
  kind: string,
  body: unknown,
  options: { session?: string; author?: string; ts?: string; seq?: number } = {},
): EventEntry {
  const seq = options.seq ?? next++;
  const header = {
    id: `id-${seq}`,
    kind,
    task: TASK,
    session: options.session ?? '',
    author: options.author ?? t.me.id,
    ts: options.ts ?? '2026-11-21T12:00:05.000Z',
  };
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status: 'verified',
    code: null,
    header,
    body,
  };
}
