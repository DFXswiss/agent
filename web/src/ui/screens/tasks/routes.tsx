// Task list of a workgroup (06-web.md sections 2.4 and 5.1), route `/o/:org/w/:wg`.
import type { RouteObject } from 'react-router-dom';
import { TaskListScreen } from './TaskListScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }];
