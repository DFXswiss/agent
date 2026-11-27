// Slice `modes`: the catalogue of working modes this hub offers (`GET /v1/modes`, ADR-098 section 1) as
// `core/modes/modes.ts` loaded it – `idle` until first needed, then `loading`, `ready` or `error` (loaded again when
// needed next). Listing a mode chooses nothing (E-32).
import type { ModeEntry } from '../../modes/modes';
import type { Slice, Store } from '../store';

export interface ModesState {
  readonly status: 'idle' | 'loading' | 'ready' | 'error';
  readonly catalogue: readonly ModeEntry[];
}

declare module '../store' {
  interface StoreState {
    modes: ModesState;
  }
}

export const slice: Slice<'modes'> = { name: 'modes', initial: () => ({ status: 'idle', catalogue: [] }) };

export function setModes(store: Store, state: ModesState): void {
  store.update('modes', () => state);
}
