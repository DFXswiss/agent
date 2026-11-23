// Monitoring of an organization (06-web.md section 9.1), route `/o/:org/monitoring`.
import type { RouteObject } from 'react-router-dom';
import { MonitoringScreen } from './MonitoringScreen';

export const routes: RouteObject[] = [{ path: '/o/:org/monitoring', element: <MonitoringScreen /> }];
