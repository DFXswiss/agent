// Members and rights of an organization (06-web.md section 7.1), route `/o/:org/members`.
import type { RouteObject } from 'react-router-dom';
import { MembersScreen } from './MembersScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/members', element: <MembersScreen /> }];
