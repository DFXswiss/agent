// Platform invitations of inviters (06-web.md section 7.4), route `/platform/invites`.
import type { RouteObject } from 'react-router-dom';
import { PlatformInvitesScreen } from './PlatformInvitesScreen';

export const routes: RouteObject[] = [{ path: '/platform/invites', element: <PlatformInvitesScreen /> }];
