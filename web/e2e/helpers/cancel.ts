// Cancelled passkey ceremonies: the next `create()`/`get()` of the page rejects like a dismissed browser dialog
// (`NotAllowedError`), later ones reach the virtual authenticator. Injected before the app's scripts run (CDP init
// script, not subject to the page's CSP), at the WebAuthn API boundary only.
import type { Page } from '@playwright/test';

export async function cancelNext(page: Page, method: 'create' | 'get', times = 1): Promise<void> {
  await page.addInitScript(
    ({ method, times }) => {
      const credentials = navigator.credentials;
      const original = credentials[method].bind(credentials);
      let left = times;
      credentials[method] = (options?: CredentialCreationOptions & CredentialRequestOptions) => {
        if (left > 0) {
          left -= 1;
          return Promise.reject(
            new DOMException('The operation either timed out or was not allowed.', 'NotAllowedError'),
          );
        }
        return original(options);
      };
    },
    { method, times },
  );
}
