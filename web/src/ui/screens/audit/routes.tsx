// Audit of an organization (06-web.md section 9.2), route `/o/:org/audit`.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const AuditScreen = lazyScreen(() => import('./AuditScreen'), 'AuditScreen');

export const routes: RouteObject[] = [{ path: '/o/:org/audit', element: <AuditScreen /> }];
