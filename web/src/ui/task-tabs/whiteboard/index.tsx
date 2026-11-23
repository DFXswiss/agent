// Task tab "Whiteboard" (06-web.md sections 2.4 and 6.1): the task-level board.
import type { TaskTab } from '../../registries/task-tabs';
import { WhiteboardTab } from './WhiteboardTab';

export const tab: TaskTab = { id: 'whiteboard', order: 40, labelKey: 'whiteboard.tab', Component: WhiteboardTab };
