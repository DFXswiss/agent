// Start of the client (06-web.md sections 2.2, 2.6, 10.4, 12.6): locale, configuration check against the hub, browser
// support, the persistence and the stored session (a reload is `locked`, or `unlocked` by the device key of a browser
// that stays signed in, rule E-37), then the app
// with its router – or the blocking error page / the unsupported-browser page. Once the hub is known, the long-task
// early warning starts (ADR-086 section 5), signed in or not; its code is a lazy chunk (not in the first load).
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import type { Credentials } from '../../core/account/ceremony';
import { Prefs, type PrefStorage } from '../../core/account/prefs';
import { AppSession, exclusiveOf } from '../../core/account/session';
import { checkConfig, type ConfigResult } from '../../core/config/check';
import { createI18n, pickLocale } from '../../core/i18n';
import { Persist } from '../../core/persist';
import { appStore, type Store } from '../../core/store';
import { setConfig } from '../../core/store/slices/config';
import { I18nProvider } from '../i18n';
import { deviceKeys } from '../prf-missing/device';
import { App } from '../shell/App';
import { type Feature, missingFeatures } from '../shell/support';
import { Unsupported } from '../screens/errors/Unsupported';
import { ErrorPage } from './ErrorPage';

export interface BootInput {
  readonly container: Element;
  readonly fetch: typeof globalThis.fetch;
  readonly rootKey: string;
  readonly origin: string;
  readonly languages: readonly string[];
  /** `navigator.credentials`. */
  readonly credentials: Credentials;
  /** IndexedDB of the page. */
  readonly indexedDB: IDBFactory;
  /** `localStorage` of the page (null if unavailable). */
  readonly storage: PrefStorage | null;
  /** `navigator.locks` of the page: one tab renews a session that stays signed in (absent: each tab on its own). */
  readonly locks?: Parameters<typeof exclusiveOf>[0];
  /** What the browser lacks (default: `missingFeatures` of the page). */
  readonly missing?: () => Promise<readonly Feature[]>;
  readonly store?: Store;
  /** Starts the long-task early warning (default: `startLongTaskMonitor` on the page's window). */
  readonly longTasks?: (hub: LongTaskHub) => void;
}

export interface LongTaskHub {
  readonly origin: string;
  readonly version: string;
  readonly fetch: typeof globalThis.fetch;
}

/** Nothing: the page then runs without the long-task early warning. */
const withoutMonitor = (): void => undefined;

/** The long-task early warning of the page, loaded after the start (not part of the first load, 06-web.md 12.2); a
 * chunk that cannot be loaded leaves the page without it. */
const pageLongTasks = (hub: LongTaskHub): void =>
  void import('../../core/monitoring/longtasks').then(
    (monitor) => void monitor.startLongTaskMonitor(monitor.browserMonitorOptions(window, hub)),
    withoutMonitor,
  );

export async function boot(input: BootInput): Promise<ConfigResult> {
  const { container, fetch, rootKey, origin, languages } = input;
  const i18n = createI18n(pickLocale(null, languages));
  document.documentElement.lang = i18n.locale;
  const root = createRoot(container);
  const page = (content: React.ReactNode) =>
    root.render(
      <StrictMode>
        <I18nProvider value={i18n}>{content}</I18nProvider>
      </StrictMode>,
    );
  const result = await checkConfig({ fetch, rootKey, origin });
  if (!result.ok) {
    page(<ErrorPage code={result.code} />);
    return result;
  }
  (input.longTasks ?? pageLongTasks)({ origin: result.config.origin, version: result.config.version, fetch });
  const missing = await (input.missing ?? missingFeatures)();
  const persist = missing.length === 0 ? await Persist.open(input.indexedDB).catch(() => null) : null;
  if (persist === null) {
    page(<Unsupported missing={missing.length === 0 ? ['indexeddb'] : missing} />);
    return result;
  }
  const store = input.store ?? appStore;
  setConfig(store, result.config);
  const app = new AppSession({
    store,
    fetch,
    config: result.config,
    rootKey,
    persist,
    prefs: new Prefs(input.storage),
    device: () => deviceKeys(input.indexedDB),
    exclusive: exclusiveOf(input.locks),
  });
  await app.restore();
  root.render(
    <StrictMode>
      <App env={{ app, credentials: input.credentials, origin }} languages={languages} />
    </StrictMode>,
  );
  return result;
}
