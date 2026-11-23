// Navigation entries discovered at `src/ui/screens/<area>/nav.ts` (each exports `nav: NavEntry[]`, ADR-027
// section 4), sorted by `order`, then `id`.
import { collect, hasShape, ordered, type ModuleMap, type Ordered } from '../core/registry';

export interface NavEntry extends Ordered {
  /** Route path, e.g. `/notifications`; `:org` is filled in by the shell. */
  readonly to: string;
  /** Message key `<area>.<key>` of the label. */
  readonly labelKey: string;
}

const navModules: ModuleMap = import.meta.glob('./screens/*/nav.ts', { eager: true });

const isNavList = (value: unknown): value is NavEntry[] =>
  Array.isArray(value) &&
  value.every((entry) => hasShape(entry, { id: 'string', order: 'number', to: 'string', labelKey: 'string' }));

export function navEntries(modules: ModuleMap = navModules): NavEntry[] {
  return ordered(collect(modules, 'nav', isNavList).flatMap((entry) => entry.value));
}
