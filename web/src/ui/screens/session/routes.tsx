// Session detail (06-web.md sections 2.4 and 5.5), route `/o/:org/s/:session` (right `task:read`).
import type { RouteObject } from 'react-router-dom';
import { SessionScreen } from './SessionScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/s/:session', element: <SessionScreen /> }];
