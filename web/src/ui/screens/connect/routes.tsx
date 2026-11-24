// Confirming the pairing of the user's AI agent (06-web.md section 2.4, 11-agent-access.md section 5.1), route
// `/connect` with the pairing in the fragment (anonymous until the passkey gesture).
import type { RouteObject } from 'react-router-dom';
import { ConnectScreen } from './ConnectScreen';

export const routes: RouteObject[] = [{ path: '/connect', element: <ConnectScreen /> }];
