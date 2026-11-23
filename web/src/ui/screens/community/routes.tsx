// The community (12-community.md section 6), route `/community` (no org in the path, right `user`). Loaded lazily
// (06-web.md section 12.2).
import type { RouteObject } from 'react-router-dom';

export const routes: RouteObject[] = [
  {
    path: '/community',
    lazy: () => import('./CommunityScreen').then((module) => ({ Component: module.CommunityScreen })),
  },
];
