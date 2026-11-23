// Virtual WebAuthn authenticators over CDP (07-testing.md section 1, Spike D): the platform authenticator with UV,
// resident keys and PRF; a USB security key; a PRF-less variant; user-verification refusal (run that case last in a
// spec – Spike D observed that Chromium keeps the refusal for later ceremonies of the same page).
import type { CDPSession, Page } from '@playwright/test';

/** PRF input of every ceremony: `prf.eval.first = SHA-256("agent.dfx.swiss/prf/v1")` (04-crypto.md section 3). */
export const PRF_INPUT_TEXT = 'agent.dfx.swiss/prf/v1';

export type Variant = 'platform' | 'usb' | 'no-prf' | 'uv-refused';

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

export const VARIANTS: Readonly<Record<Variant, AuthenticatorOptions>> = {
  platform: PLATFORM,
  usb: { ...PLATFORM, transport: 'usb' },
  'no-prf': { ...PLATFORM, hasPrf: false },
  'uv-refused': { ...PLATFORM, isUserVerified: false },
};

export interface VirtualAuthenticator {
  readonly id: string;
  readonly cdp: CDPSession;
  readonly options: AuthenticatorOptions;
  credentials(): Promise<unknown[]>;
  setUserVerified(verified: boolean): Promise<void>;
  remove(): Promise<void>;
}

/** Adds a virtual authenticator of `variant` to the page's browser context (WebAuthn UI off). */
export async function addAuthenticator(
  page: Page,
  variant: Variant = 'platform',
  overrides: Partial<AuthenticatorOptions> = {},
): Promise<VirtualAuthenticator> {
  const options = { ...VARIANTS[variant], ...overrides };
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('WebAuthn.enable', { enableUI: false });
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options });
  return {
    id: authenticatorId,
    cdp,
    options,
    credentials: async () => (await cdp.send('WebAuthn.getCredentials', { authenticatorId })).credentials,
    setUserVerified: async (isUserVerified) => {
      await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified });
    },
    remove: async () => {
      await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
    },
  };
}
