// Settings sections discovered at `src/ui/settings/sections/<area>/index.tsx` (export `section`, ADR-027 §4).
import type { ComponentType } from 'react';
import { collect, hasShape, ordered, type ModuleMap, type Ordered } from '../../core/registry';

export interface SettingsSection extends Ordered {
  readonly titleKey: string;
  readonly Component: ComponentType;
}

const sectionModules: ModuleMap = import.meta.glob('../settings/sections/*/index.tsx', { eager: true });

const isSection = (value: unknown): value is SettingsSection =>
  hasShape(value, { id: 'string', order: 'number', titleKey: 'string', Component: 'component' });

export function settingsSections(modules: ModuleMap = sectionModules): SettingsSection[] {
  return ordered(collect(modules, 'section', isSection).map((entry) => entry.value));
}
