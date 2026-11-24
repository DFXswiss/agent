// Devices of an organization (06-web.md section 7.3), route `/o/:org/devices`.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const DevicesScreen = lazyScreen(() => import('./DevicesScreen'), 'DevicesScreen');

export const routes: RouteObject[] = [{ path: '/o/:org/devices', element: <DevicesScreen /> }];
