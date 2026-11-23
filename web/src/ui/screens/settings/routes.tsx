// Account settings (06-web.md section 2.4, route `/settings`; right `user`): the frame of `ui/settings/`.
import type { RouteObject } from 'react-router-dom';
import { SettingsScreen } from '../../settings/SettingsScreen';

export const routes: RouteObject[] = [{ path: '/settings', element: <SettingsScreen /> }];
