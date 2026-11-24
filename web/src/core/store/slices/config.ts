// Slice `config`: the checked hub configuration (`GET /v1/config`, `core/config/check.ts`).
import type { PlatformConfig } from '../../config/check';
import type { Slice, Store } from '../store';

export interface ConfigState {
  readonly config: PlatformConfig | null;
}

declare module '../store' {
  interface StoreState {
    config: ConfigState;
  }
}

export const slice: Slice<'config'> = { name: 'config', initial: () => ({ config: null }) };

export function setConfig(store: Store, config: PlatformConfig): void {
  store.update('config', () => ({ config }));
}
