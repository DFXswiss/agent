// Invitation screen (06-web.md sections 2.4 and 3.1), route `/invite` with the code in the fragment.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const InviteScreen = lazyScreen(() => import('./InviteScreen'), 'InviteScreen');

export const routes: RouteObject[] = [{ path: '/invite', element: <InviteScreen /> }];
