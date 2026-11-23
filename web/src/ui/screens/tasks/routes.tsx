// Task list of a workgroup (06-web.md sections 2.4 and 5.1), route `/o/:org/w/:wg`, and the task overview of the
// organization across its readable workgroups (KOR-32), route `/o/:org/tasks`.
import type { RouteObject } from 'react-router-dom';
import { OverviewScreen } from './OverviewScreen';
import { TaskListScreen } from './TaskListScreen';

export const routes: RouteObject[] = [
  { path: '/o/:org/w/:wg', element: <TaskListScreen /> },
  { path: '/o/:org/tasks', element: <OverviewScreen /> },
];
