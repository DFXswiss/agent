// Home of an organization (06-web.md sections 2.4 and 4.1), route `/o/:org`: the first-task composer and the recent
// tasks of the default workgroup.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const HomeScreen = lazyScreen(() => import('./HomeScreen'), 'HomeScreen');

export const routes: RouteObject[] = [{ path: '/o/:org', element: <HomeScreen /> }];
