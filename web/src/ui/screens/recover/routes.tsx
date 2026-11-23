// Recovery screen (06-web.md sections 2.4 and 3.5), route `/recover` (anonymous).
import type { RouteObject } from 'react-router-dom';
import { RecoverScreen } from './RecoverScreen';

export const routes: RouteObject[] = [{ path: '/recover', element: <RecoverScreen /> }];
