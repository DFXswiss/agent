import type { TaskTabProps } from '../../../../../src/ui/registries/task-tabs';

export const tab = {
  id: 'demo',
  order: 10,
  labelKey: 'demo.tab',
  Component: ({ taskId }: TaskTabProps) => <p>demo tab {taskId}</p>,
};
