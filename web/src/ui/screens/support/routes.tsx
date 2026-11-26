// The support area of DFX support persons (ADR-096 section 4; KOR-56): `/support` (the releases), a released workgroup
// and a task of it; loaded lazily like the other screens.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const SupportScreen = lazyScreen(() => import('./SupportScreen'), 'SupportScreen');
const SupportWorkgroupScreen = lazyScreen(() => import('./SupportScreen'), 'SupportWorkgroupScreen');
const SupportTaskScreen = lazyScreen(() => import('./SupportScreen'), 'SupportTaskScreen');

export const routes: RouteObject[] = [
  { path: '/support', element: <SupportScreen /> },
  { path: '/support/:org/:wg', element: <SupportWorkgroupScreen /> },
  { path: '/support/:org/:wg/:task', element: <SupportTaskScreen /> },
];
