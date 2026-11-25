// Task tab "Pull requests" (KOR-48, ADR-084, 06-web.md section 5.6).
import type { TaskTab } from '../../registries/task-tabs';
import { PullsTab } from './PullsTab';

export const tab: TaskTab = { id: 'pulls', order: 25, labelKey: 'pulls.tab', Component: PullsTab };
