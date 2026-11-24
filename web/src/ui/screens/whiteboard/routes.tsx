// Whiteboards of an organization and of a workgroup (06-web.md sections 2.4 and 6.1), routes `/o/:org/whiteboard`
// (right `org:member`) and `/o/:org/w/:wg/whiteboard` (right `wg:read`); the task level is a task tab.
import type { RouteObject } from 'react-router-dom';
import { lazyScreen } from '../../lazy/screen';

const WhiteboardScreen = lazyScreen(() => import('./WhiteboardScreen'), 'WhiteboardScreen');

export const routes: RouteObject[] = [
  { path: '/o/:org/whiteboard', element: <WhiteboardScreen /> },
  { path: '/o/:org/w/:wg/whiteboard', element: <WhiteboardScreen /> },
];
