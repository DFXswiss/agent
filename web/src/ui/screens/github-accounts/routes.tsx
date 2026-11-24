// Company GitHub accounts of an organization (ADR-078, 06-web.md section 8.1), route `/o/:org/github`, loaded lazily
// like the other screens (06-web.md section 12.2).
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const CompanyAccountsScreen = lazyScreen(() => import('./CompanyAccountsScreen'), 'CompanyAccountsScreen');

export const routes: RouteObject[] = [{ path: '/o/:org/github', element: <CompanyAccountsScreen /> }];
