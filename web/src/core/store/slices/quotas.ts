// Slice `quotas` (06-web.md section 8.2): the subscription quota bars of the user as `core/credentials/quotas.ts` last
// loaded them (`null` until loaded; an empty list means "not available").
import type { Slice, Store } from '../store';

export interface QuotaBar {
  /** `five_hour`, `seven_day` or `weekly:<model>`. */
  readonly window: string;
  /** The model of a per-model window, else null. */
  readonly model: string | null;
  readonly usedPercent: number;
  /** Unix ms of the reset, null when unknown. */
  readonly resetsAt: number | null;
  readonly severity: string | null;
  /** Unix ms of the device's read. */
  readonly fetchedAt: number;
}

export interface QuotasState {
  readonly bars: readonly QuotaBar[] | null;
}

declare module '../store' {
  interface StoreState {
    quotas: QuotasState;
  }
}

export const slice: Slice<'quotas'> = { name: 'quotas', initial: () => ({ bars: null }) };

export function setQuotas(store: Store, bars: readonly QuotaBar[]): void {
  store.update('quotas', () => ({ bars }));
}
