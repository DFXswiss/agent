// Navigation entries discovered at `src/ui/screens/<area>/nav.ts` (each exports `nav: NavEntry[]`, ADR-027
// section 4), sorted by `order`, then `id`. An entry may hide itself and carry a badge count from the store
// (12-community.md section 6: the community's unread messages); `useNavState` reads both for the shell.
import { useMemo } from 'react';
import { collect, hasShape, ordered, type ModuleMap, type Ordered } from '../core/registry';
import type { Store, StoreState } from '../core/store';
import { useStore } from './hooks/useStore';

export interface NavEntry extends Ordered {
  /** Route path, e.g. `/notifications`; `:org` is filled in by the shell. */
  readonly to: string;
  /** Message key `<area>.<key>` of the label. */
  readonly labelKey: string;
  /** Hides the entry while true (e.g. signed out, or the hub lacks the area). */
  readonly hidden?: (state: StoreState) => boolean;
  /** A count shown as a badge ("99+" above 99); none at 0. */
  readonly badge?: (state: StoreState) => number;
}

const navModules: ModuleMap = import.meta.glob('./screens/*/nav.ts', { eager: true });

const isNavList = (value: unknown): value is NavEntry[] =>
  Array.isArray(value) &&
  value.every((entry) => hasShape(entry, { id: 'string', order: 'number', to: 'string', labelKey: 'string' }));

export function navEntries(modules: ModuleMap = navModules): NavEntry[] {
  return ordered(collect(modules, 'nav', isNavList).flatMap((entry) => entry.value));
}

/** The badge text of a count: none at 0, "99+" above 99. */
export function badgeText(count: number): string | null {
  if (count <= 0) return null;
  return count > 99 ? '99+' : String(count);
}

/** The visible entries with their badge counts, re-rendered when either changes. */
export function useNavState(
  entries: readonly NavEntry[],
  store: Store,
): { readonly visible: readonly NavEntry[]; readonly badges: ReadonlyMap<string, number> } {
  // one primitive per render (stable for unchanged state): `-` for a hidden entry, else its count
  const key = useStore(
    (state) => entries.map((entry) => (entry.hidden?.(state) ? '-' : String(entry.badge?.(state) ?? 0))).join(','),
    store,
  );
  return useMemo(() => {
    const parts = key.split(',');
    const visible = entries.filter((_, index) => parts[index] !== '-');
    const badges = new Map(visible.map((entry) => [entry.id, Number(parts[entries.indexOf(entry)])]));
    return { visible, badges };
  }, [key, entries]);
}
