// The signed-in browser for React: the `AppSession` (core/account/session.ts), the WebAuthn container
// (`navigator.credentials` or a test double) and the page origin for the session statement. Provided once by `App`.
import { createContext, useContext } from 'react';
import type { Credentials } from '../../core/account/ceremony';
import type { AppSession } from '../../core/account/session';

export interface AppEnv {
  readonly app: AppSession;
  readonly credentials: Credentials;
  /** `location.origin` (the statement's origin). */
  readonly origin: string;
}

const AppEnvContext = createContext<AppEnv | null>(null);

export const AppEnvProvider = AppEnvContext.Provider;

/** The app environment; screens are only rendered inside `App`. */
export function useAppEnv(): AppEnv {
  const env = useContext(AppEnvContext);
  if (env === null) throw new Error('useAppEnv outside <App>');
  return env;
}
