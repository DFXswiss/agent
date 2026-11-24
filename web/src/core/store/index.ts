// The application store instance and the public API of `core/store`.
import { createStore, type Store } from './store';

export { createStore, Store, subscribe, type Listener, type Selector } from './store';
export type { Slice, StoreState } from './store';

/** The one store of the application (06-web.md section 2.3). */
export const appStore: Store = createStore();
