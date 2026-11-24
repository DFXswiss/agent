// Home of an organization (06-web.md sections 2.4, 4.0 and 4.1), route `/o/:org`: the situation view with the
// first-task composer on top, loaded lazily like the other screens (the sign-in screen does not wait for its code).
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const HomeScreen = lazyScreen(() => import('./HomeScreen'), 'HomeScreen');

export const routes: RouteObject[] = [{ path: '/o/:org', element: <HomeScreen /> }];
