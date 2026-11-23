// Virtual WebAuthn authenticators over CDP (07-testing.md section 1, Spike D) for the web specs: the platform
// authenticator with UV, resident keys and PRF; variants without PRF and with refused user verification.
import type { CDPSession, Page } from '@playwright/test';

export interface AuthenticatorOptions {
  readonly protocol: 'ctap2';
  readonly ctap2Version: 'ctap2_1';
  readonly transport: 'internal' | 'usb';
  readonly hasResidentKey: boolean;
  readonly hasUserVerification: boolean;
  readonly isUserVerified: boolean;
  readonly automaticPresenceSimulation: boolean;
  readonly hasPrf: boolean;
}

export const PLATFORM: AuthenticatorOptions = {
  protocol: 'ctap2',
  ctap2Version: 'ctap2_1',
  transport: 'internal',
  hasResidentKey: true,
  hasUserVerification: true,
  isUserVerified: true,
  automaticPresenceSimulation: true,
  hasPrf: true,
};

export interface VirtualAuthenticator {
  readonly id: string;
  readonly cdp: CDPSession;
  credentials(): Promise<unknown[]>;
  /** Presence simulation off: the next ceremony waits for the user and can be cancelled from the page. */
  setPresence(enabled: boolean): Promise<void>;
  setUserVerified(verified: boolean): Promise<void>;
}

export async function addAuthenticator(
  page: Page,
  overrides: Partial<AuthenticatorOptions> = {},
): Promise<VirtualAuthenticator> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('WebAuthn.enable', { enableUI: false });
  const options = { ...PLATFORM, ...overrides };
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options });
  return {
    id: authenticatorId,
    cdp,
    credentials: async () => (await cdp.send('WebAuthn.getCredentials', { authenticatorId })).credentials,
    setPresence: async (enabled) => {
      await cdp.send('WebAuthn.setAutomaticPresenceSimulation', { authenticatorId, enabled });
    },
    setUserVerified: async (isUserVerified) => {
      await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified });
    },
  };
}

/**
 * The same passkeys in an authenticator that gives no PRF output – an existing account signing in from a browser or
 * password manager without PRF (KOR-22: a new account cannot be made that way). `from` is removed; its credentials
 * (keys, user handles, counters) are copied into a new authenticator with `hasPrf: false`.
 */
export async function withoutPrf(page: Page, from: VirtualAuthenticator): Promise<VirtualAuthenticator> {
  const { credentials } = await from.cdp.send('WebAuthn.getCredentials', { authenticatorId: from.id });
  await from.cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: from.id });
  const to = await addAuthenticator(page, { hasPrf: false });
  for (const credential of credentials) {
    await to.cdp.send('WebAuthn.addCredential', { authenticatorId: to.id, credential });
  }
  return to;
}
