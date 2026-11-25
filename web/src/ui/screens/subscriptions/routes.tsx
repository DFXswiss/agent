// The page "My subscriptions" (KOR-68, rule E-30): `/subscriptions`, the user's own, loaded lazily like the other
// screens.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const SubscriptionsScreen = lazyScreen(() => import('./SubscriptionsScreen'), 'SubscriptionsScreen');

export const routes: RouteObject[] = [{ path: '/subscriptions', element: <SubscriptionsScreen /> }];
