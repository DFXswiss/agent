// Screens loaded on first use (06-web.md section 12.2; HOT-16): a route's screen is a chunk of its own, so the start
// bundle carries only what the sign-in page needs. While the chunk loads, the screen area shows its frame at once
// (`screen.loading`, never an empty page); a chunk that cannot be loaded or fails its integrity check
// (`core/assets/integrity.ts`) passes the error on to the route's error page with its reload button – a browser keeps
// a failed module as failed until the page reloads, so nothing is retried.
import { type ComponentType, Suspense } from 'react';
import { useI18n } from '../i18n';

const loaders = new Set<() => Promise<void>>();

/** Loads every lazy screen now (tests render screens synchronously after this). */
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

/** The export `name` of the module `load` imports, as a component that loads it on first render. */
export function lazyScreen<N extends string>(
  load: () => Promise<Readonly<Record<N, ComponentType>>>,
  name: N,
): ComponentType {
  let Screen: ComponentType | undefined;
  let failure: { readonly error: unknown } | undefined;
  let pending: Promise<void> | undefined;
  const fetch = () =>
    (pending ??= load().then(
      (module) => {
        Screen = module[name];
      },
      (error: unknown) => {
        failure = { error };
      },
    ));
  loaders.add(fetch);
  function Loaded() {
    if (Screen !== undefined) return <Screen />;
    if (failure !== undefined) throw failure.error;
    throw fetch();
  }
  return function LazyScreen() {
    return (
      <Suspense fallback={<ScreenLoading />}>
        <Loaded />
      </Suspense>
    );
  };
}
