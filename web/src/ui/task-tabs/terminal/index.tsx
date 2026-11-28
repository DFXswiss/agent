// Task tab "Claude Code" (06-web.md section 5.2a, KONZEPT E-43), the default tab of `/o/:org/t/:task`: the native
// terminal of the task's main session with the prompt input below it. The chat stays the second tab.
import type { TaskTab } from '../../registries/task-tabs';
import { TerminalTab } from './TerminalTab';

export const tab: TaskTab = { id: 'terminal', order: 5, labelKey: 'task.tab.terminal', Component: TerminalTab };
