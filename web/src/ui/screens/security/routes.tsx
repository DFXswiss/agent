// Security page (06-web.md sections 2.4 and 10.5), route `/security` (anonymous). Loaded lazily (06-web.md section
// 12.2).
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const SecurityScreen = lazyScreen(() => import('./SecurityScreen'), 'SecurityScreen');

export const routes: RouteObject[] = [{ path: '/security', element: <SecurityScreen /> }];
