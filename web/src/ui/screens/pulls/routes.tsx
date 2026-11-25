// The "Pull requests" view of a workgroup (KOR-48, ADR-084): `/o/:org/pulls` (the default workgroup) and
// `/o/:org/w/:wg/pulls`, loaded lazily like the other screens.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const PullsScreen = lazyScreen(() => import('./PullsScreen'), 'PullsScreen');

export const routes: RouteObject[] = [
  { path: '/o/:org/pulls', element: <PullsScreen /> },
  { path: '/o/:org/w/:wg/pulls', element: <PullsScreen /> },
];
