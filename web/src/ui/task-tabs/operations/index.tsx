// Task tab "Operations" (06-web.md section 5.4).
import type { TaskTab } from '../../registries/task-tabs';
import { OperationsTab } from './OperationsTab';

export const tab: TaskTab = { id: 'operations', order: 30, labelKey: 'operations.tab', Component: OperationsTab };
