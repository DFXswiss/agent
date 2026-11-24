// Browser support (06-web.md section 12.6): passkeys (WebAuthn), X25519 and Ed25519 in WebCrypto, and streaming
// `fetch` (the live channel reads SSE from a `ReadableStream`); IndexedDB is checked when it is opened. Without them
// nothing of the platform works, so the client shows the unsupported-browser page (with what is missing) instead of the
// router.
export type Feature = 'webauthn' | 'streams' | 'x25519' | 'ed25519' | 'indexeddb';

export interface SupportScope {
  readonly PublicKeyCredential?: unknown;
  readonly ReadableStream?: unknown;
  readonly crypto?: { readonly subtle?: Pick<SubtleCrypto, 'generateKey'> };
}

async function generates(subtle: Pick<SubtleCrypto, 'generateKey'> | undefined, name: string, usages: KeyUsage[]) {
  if (subtle === undefined) return false;
  try {
    await subtle.generateKey({ name }, false, usages);
    return true;
  } catch {
    return false;
  }
}

/** What `scope` (default: the page) lacks of what the client needs; empty if it is supported. */
export async function missingFeatures(scope: SupportScope = globalThis as SupportScope): Promise<Feature[]> {
  const subtle = scope.crypto?.subtle;
  const checks: [Feature, boolean][] = [
    ['webauthn', typeof scope.PublicKeyCredential === 'function'],
    ['streams', typeof scope.ReadableStream === 'function'],
    ['x25519', await generates(subtle, 'X25519', ['deriveBits'])],
    ['ed25519', await generates(subtle, 'Ed25519', ['sign'])],
  ];
  return checks.filter(([, present]) => !present).map(([feature]) => feature);
}

/** True if `scope` (default: the page) offers everything the client needs. */
export async function browserSupported(scope?: SupportScope): Promise<boolean> {
  return (await missingFeatures(scope)).length === 0;
}
