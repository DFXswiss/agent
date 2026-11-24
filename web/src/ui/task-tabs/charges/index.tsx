// Task tab "Costs" (12-billing.md section 9): the charges of the task per session (`ChargesTab`).
import type { TaskTab } from '../../registries/task-tabs';
import { ChargesTab } from './ChargesTab';

export const tab: TaskTab = { id: 'charges', order: 35, labelKey: 'billing.tab', Component: ChargesTab };
