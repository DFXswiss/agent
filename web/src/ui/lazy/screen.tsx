// Screens loaded on first use (06-web.md section 12.2; HOT-16): a route's screen is a chunk of its own, so the start
// bundle carries only what the sign-in page needs. While the chunk loads, the screen area shows its frame at once
// (`screen.loading`, never an empty page). A chunk that cannot be loaded – the link to the hub is down, or the bytes
// fail their integrity check (`core/assets/integrity.ts`, fail closed) – shows "page could not be loaded" with a reload
// in place of the screen, inside the shell, so the connection banner stays (ACC-FINAL decision 11); a browser keeps a
// failed module as failed until the page reloads, so nothing is retried. Any other error passes on to the route's
// error page.
import { type ComponentType, Suspense } from 'react';
import { useI18n } from '../i18n';
import { ERROR_BUTTON, ErrorLayout } from '../screens/errors/ErrorLayout';
import { ChunkBoundary } from './ChunkBoundary';

const loaders = new Set<() => Promise<void>>();

/** Loads every lazy screen now (the UI tests render screens synchronously after this). */
export async function preloadScreens(): Promise<void> {
  await Promise.all([...loaders].map((load) => load()));
}

/** The frame of a screen whose chunk is still loading. */
export function ScreenLoading() {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-3 p-4" role="status" aria-busy="true" data-testid="screen.loading">
      <span className="sr-only">{t('app.loading')}</span>
      <div className="h-7 w-1/3 rounded bg-gray-200" />
      <div className="h-4 w-2/3 rounded bg-gray-100" />
      <div className="h-4 w-1/2 rounded bg-gray-100" />
    </div>
  );
}

/** In place of a screen whose chunk cannot be loaded. */
export function ScreenUnavailable({ onReload = () => window.location.reload() }: { readonly onReload?: () => void }) {
  const { t } = useI18n();
  return (
    <ErrorLayout
      testId="screen.unavailable"
      prefix="screen-unavailable"
      title={t('app.screen-unavailable.title')}
      body={<p>{t('app.screen-unavailable.body')}</p>}
      action={t('app.screen-unavailable.action')}
    >
      <button type="button" className={ERROR_BUTTON} onClick={onReload} data-testid="error.reload">
        {t('app.error.reload')}
      </button>
    </ErrorLayout>
  );
}

/** The export `name` of the module `load` imports, as a component that loads it on first render. */
export function lazyScreen<N extends string>(
  load: () => Promise<Readonly<Record<N, ComponentType>>>,
  name: N,
): ComponentType {
  let Screen: ComponentType | undefined;
  let failure: { readonly error: unknown } | undefined;
  let pending: Promise<void> | undefined;
  const request = () =>
    (pending ??= load().then(
      (module) => {
        Screen = module[name];
      },
      (error: unknown) => {
        failure = { error };
      },
    ));
  loaders.add(request);
  function Loaded() {
    if (Screen !== undefined) return <Screen />;
    if (failure !== undefined) throw failure.error;
    throw request();
  }
  return function LazyScreen() {
    return (
      <ChunkBoundary fallback={<ScreenUnavailable />}>
        <Suspense fallback={<ScreenLoading />}>
          <Loaded />
        </Suspense>
      </ChunkBoundary>
    );
  };
}
