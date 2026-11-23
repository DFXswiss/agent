// Task tab "Chat" (06-web.md section 5.2), the default tab of `/o/:org/t/:task`.
import type { TaskTab } from '../../registries/task-tabs';
import { ChatTab } from './ChatTab';

export const tab: TaskTab = { id: 'chat', order: 10, labelKey: 'task.tab.chat', Component: ChatTab };
