// Notification center (06-web.md sections 2.4 and 6.2), route `/notifications` (right `user`).
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const NotificationsScreen = lazyScreen(() => import('./NotificationsScreen'), 'NotificationsScreen');

export const routes: RouteObject[] = [{ path: '/notifications', element: <NotificationsScreen /> }];
