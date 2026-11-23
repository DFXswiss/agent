// The real app (shell, screens, AppSession, account flows) in jsdom against the simulated hub of the account-flow
// tests and the fake authenticator: a memory router at `path`, the live channel stubbed (no stream in jsdom).
import { render } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { createMemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import { Prefs, type PrefStorage } from '../../src/core/account/prefs';
import { AppSession } from '../../src/core/account/session';
import { Persist } from '../../src/core/persist';
import { createStore } from '../../src/core/store';
import { appRoutes, ROUTER_FUTURE } from '../../src/ui/router';
import { App } from '../../src/ui/shell/App';
import { FakeAuthenticator } from '../core/account/authenticator';
import { CONFIG, ORIGIN, SimHub } from '../core/account/hub';

export class MemoryStorage implements PrefStorage {
  readonly items = new Map<string, string>();
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}

export interface Harness {
  readonly hub: SimHub;
  readonly auth: FakeAuthenticator;
  readonly app: AppSession;
  readonly storage: MemoryStorage;
  readonly persist: Persist;
  readonly router: ReturnType<typeof createMemoryRouter>;
  readonly live: ReturnType<typeof vi.fn>;
}

export async function makeApp(options: { hub?: SimHub; auth?: FakeAuthenticator; persist?: Persist } = {}) {
  const hub = options.hub ?? new SimHub();
  hub.now = Date.now();
  const auth = options.auth ?? new FakeAuthenticator();
  const persist = options.persist ?? (await Persist.open(new IDBFactory()));
  const storage = new MemoryStorage();
  const live = vi.fn(async () => ({ channel: {} as never, stop: () => undefined }));
  const app = new AppSession({
    store: createStore(),
    fetch: hub.fetch,
    config: CONFIG,
    rootKey: CONFIG.platform_root_key,
    persist,
    prefs: new Prefs(storage),
    live,
  });
  return { hub, auth, app, storage, persist, live };
}

/** Renders the app at `path`; `before` runs on the app before the first render (e.g. restore, sign-in). */
export async function renderApp(
  path: string,
  options: { hub?: SimHub; auth?: FakeAuthenticator; languages?: string[]; persist?: Persist } = {},
  before?: (made: Awaited<ReturnType<typeof makeApp>>) => Promise<void>,
): Promise<Harness & { view: ReturnType<typeof render> }> {
  const made = await makeApp(options);
  if (before) await before(made);
  else await made.app.restore();
  const router = createMemoryRouter(appRoutes(), { initialEntries: [path], future: ROUTER_FUTURE });
  const view = render(
    <App
      env={{ app: made.app, credentials: made.auth, origin: ORIGIN }}
      languages={options.languages ?? ['en']}
      router={router}
    />,
  );
  return { ...made, router, view };
}
