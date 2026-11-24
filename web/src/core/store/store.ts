// The single application store (06-web.md section 2.3): one immutable state object of named slices, replaced slice by
// slice. Only core modules update it (through the action functions of the slice files); React reads it through
// `useStore(selector)` (`ui/hooks/useStore.ts`). Listeners run synchronously after every update.
import { type SliceRegistration, storeSlices } from './slices';

// The shape of the store: every slice file adds its part by module augmentation
//
//   declare module '../store' {
//     interface StoreState { myArea: MyAreaState }
//   }
//
// so packages add slices as new files (ADR-027 section 4) and this interface stays empty here.
// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface StoreState {}

/** A slice registration with its typed initial state. */
export interface Slice<K extends keyof StoreState> {
  readonly name: K;
  readonly initial: () => StoreState[K];
}

export type Listener = () => void;
export type Selector<T> = (state: StoreState) => T;

export class Store {
  #state: StoreState;
  #generation = 0;
  readonly #slices: readonly SliceRegistration[];
  readonly #listeners = new Set<Listener>();

  constructor(slices: readonly SliceRegistration[]) {
    this.#slices = slices;
    this.#state = Store.#initial(slices);
  }

  static #initial(slices: readonly SliceRegistration[]): StoreState {
    return Object.fromEntries(slices.map((slice) => [slice.name, slice.initial()])) as unknown as StoreState;
  }

  /** The current state (stable between updates, as `useSyncExternalStore` requires). */
  readonly getState = (): StoreState => this.#state;

  /** Replaces one slice with `update(previous)`; no notification if the result is the same object. */
  update<K extends keyof StoreState>(name: K, update: (previous: StoreState[K]) => StoreState[K]): void {
    if (!Object.hasOwn(this.#state, name)) throw new Error(`unknown store slice "${String(name)}"`);
    const previous = this.#state[name];
    const next = update(previous);
    if (next === previous) return;
    this.#state = { ...this.#state, [name]: next };
    for (const listener of [...this.#listeners]) listener();
  }

  /** Increases with every `reset`: asynchronous work started before a sign-out must not write afterwards. */
  get generation(): number {
    return this.#generation;
  }

  /** Every slice back to its initial state (sign-out: decrypted content and keys leave memory). */
  reset(): void {
    this.#generation += 1;
    this.#state = Store.#initial(this.#slices);
    for (const listener of [...this.#listeners]) listener();
  }

  /** Registers a listener for every update; returns the unsubscribe function. */
  readonly subscribe = (listener: Listener): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
}

/** A store with the discovered slices. */
export function createStore(slices: readonly SliceRegistration[] = storeSlices()): Store {
  return new Store(slices);
}

/**
 * Calls `listener(next, previous)` whenever the selected value changes (by `equals`, default `Object.is`); returns
 * the unsubscribe function.
 */
export function subscribe<T>(
  store: Store,
  selector: Selector<T>,
  listener: (next: T, previous: T) => void,
  equals: (a: T, b: T) => boolean = Object.is,
): () => void {
  let current = selector(store.getState());
  return store.subscribe(() => {
    const next = selector(store.getState());
    if (equals(next, current)) return;
    const previous = current;
    current = next;
    listener(next, previous);
  });
}
