// Registration point of store slices (ADR-027 section 4): every file `src/core/store/slices/<area>.ts` exports
// `slice` = `{ name, initial }` (its state type joins `StoreState` by module augmentation of `../state`). The store
// (`../store.ts`) is built from this list; packages add files and never edit this one.
import { collect, isNamed, type ModuleMap } from '../../registry';

export interface SliceRegistration {
  readonly name: string;
  /** A fresh initial state (also used to reset the slice on sign-out). */
  readonly initial: () => unknown;
}

const isSlice = (value: unknown): value is SliceRegistration =>
  isNamed(value) && typeof (value as { initial?: unknown }).initial === 'function';

const sliceModules: ModuleMap = import.meta.glob(['./*.ts', '!./index.ts'], { eager: true });

/** Registered slices, sorted by module path; duplicate names are refused. */
export function storeSlices(modules: ModuleMap = sliceModules): SliceRegistration[] {
  const slices = collect(modules, 'slice', isSlice).map((entry) => entry.value);
  const names = new Set(slices.map((slice) => slice.name));
  if (names.size !== slices.length) throw new Error('duplicate store slice name');
  return slices;
}
