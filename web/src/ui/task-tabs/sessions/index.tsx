// Task tab "Sessions" (06-web.md section 5.3).
import type { TaskTab } from '../../registries/task-tabs';
import { SessionsTab } from './SessionsTab';

export const tab: TaskTab = { id: 'sessions', order: 20, labelKey: 'task.tab.sessions', Component: SessionsTab };
