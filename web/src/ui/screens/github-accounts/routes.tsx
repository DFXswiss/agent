// Company GitHub accounts of an organization (ADR-078, 06-web.md section 8.1), route `/o/:org/github`.
import type { RouteObject } from 'react-router-dom';
import { CompanyAccountsScreen } from './CompanyAccountsScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/github', element: <CompanyAccountsScreen /> }];
