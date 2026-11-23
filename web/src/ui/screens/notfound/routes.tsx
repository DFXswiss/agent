// Not-found screen (06-web.md section 2.4, route `*`).
import type { RouteObject } from 'react-router-dom';
import { NotFound } from './NotFound';

export const routes: RouteObject[] = [{ path: '*', element: <NotFound /> }];
