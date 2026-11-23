// Task page (06-web.md section 2.4), route `/o/:org/t/:task`; the frame of WEB-04, extended by WEB-05.
import type { RouteObject } from 'react-router-dom';
import { TaskScreen } from './TaskScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/t/:task', element: <TaskScreen /> }];
