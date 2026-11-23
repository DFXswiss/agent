// React's read access to the store (06-web.md section 2.3): `useStore(selector)` re-renders when the selected value
// changes. Selectors must return stable values for unchanged state (see `core/store/selectors.ts`, `memoize`).
import { useSyncExternalStore } from 'react';
import { appStore, type Selector, type Store } from '../../core/store';

export function useStore<T>(selector: Selector<T>, store: Store = appStore): T {
  return useSyncExternalStore(store.subscribe, () => selector(store.getState()));
}
