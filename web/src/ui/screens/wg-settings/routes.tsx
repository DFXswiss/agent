// Workgroup settings (06-web.md sections 2.4 and 8.1), route `/o/:org/w/:wg/settings`.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const WgSettingsScreen = lazyScreen(() => import('./WgSettingsScreen'), 'WgSettingsScreen');

export const routes: RouteObject[] = [{ path: '/o/:org/w/:wg/settings', element: <WgSettingsScreen /> }];
