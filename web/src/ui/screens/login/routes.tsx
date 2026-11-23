// Sign-in and the start route (06-web.md section 2.4): `/` sends an unlocked user to the last org, everyone else to
// `/login`; `/login` signs in with one passkey gesture and unlocks the identity (section 3.4).
import type { RouteObject } from 'react-router-dom';
import { LoginScreen } from './LoginScreen';
import { StartRedirect } from './StartRedirect';

export const routes: RouteObject[] = [
  { path: '/', element: <StartRedirect /> },
  { path: '/login', element: <LoginScreen /> },
];
