// Workgroups of an organization (06-web.md section 7.2), route `/o/:org/workgroups`.
import type { RouteObject } from 'react-router-dom';
import { WorkgroupsScreen } from './WorkgroupsScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/workgroups', element: <WorkgroupsScreen /> }];
