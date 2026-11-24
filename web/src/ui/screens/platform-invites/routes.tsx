// Platform invitations of inviters (06-web.md section 7.4), route `/platform/invites`.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const PlatformInvitesScreen = lazyScreen(() => import('./PlatformInvitesScreen'), 'PlatformInvitesScreen');

export const routes: RouteObject[] = [{ path: '/platform/invites', element: <PlatformInvitesScreen /> }];
