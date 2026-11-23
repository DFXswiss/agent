// Header items discovered at `src/ui/shell/header/<area>.tsx` (export `headerItem`, ADR-027 §4), e.g. the org
// switcher and the notification bell.
import type { ComponentType } from 'react';
import { collect, hasShape, ordered, type ModuleMap, type Ordered } from '../../core/registry';

export interface HeaderItem extends Ordered {
  readonly Component: ComponentType;
}

const headerModules: ModuleMap = import.meta.glob('../shell/header/*.tsx', { eager: true });

const isHeaderItem = (value: unknown): value is HeaderItem =>
  hasShape(value, { id: 'string', order: 'number', Component: 'component' });

export function headerItems(modules: ModuleMap = headerModules): HeaderItem[] {
  return ordered(collect(modules, 'headerItem', isHeaderItem).map((entry) => entry.value));
}
