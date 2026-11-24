// Task tab "Audit" (06-web.md sections 2.4 and 9.2): the audit view filtered by the task.
import type { TaskTab } from '../../registries/task-tabs';
import { AuditTab } from './AuditTab';

export const tab: TaskTab = { id: 'audit', order: 50, labelKey: 'audit.tab', Component: AuditTab };
