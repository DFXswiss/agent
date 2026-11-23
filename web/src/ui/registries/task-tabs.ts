// Task tabs discovered at `src/ui/task-tabs/<area>/index.tsx` (export `tab`, ADR-027 §4, 06-web.md section 2.4).
import type { ComponentType } from 'react';
import { collect, hasShape, ordered, type ModuleMap, type Ordered } from '../../core/registry';

export interface TaskTabProps {
  readonly orgId: string;
  readonly taskId: string;
}

export interface TaskTab extends Ordered {
  readonly labelKey: string;
  readonly Component: ComponentType<TaskTabProps>;
}

const tabModules: ModuleMap = import.meta.glob('../task-tabs/*/index.tsx', { eager: true });

const isTab = (value: unknown): value is TaskTab =>
  hasShape(value, { id: 'string', order: 'number', labelKey: 'string', Component: 'component' });

export function taskTabs(modules: ModuleMap = tabModules): TaskTab[] {
  return ordered(collect(modules, 'tab', isTab).map((entry) => entry.value));
}
