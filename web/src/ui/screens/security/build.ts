// What the security page shows about the delivered build (06-web.md sections 10.3 and 10.5): the build hash is the
// SHA-256 of the exact bytes of `build-manifest.json` as the hub delivers it; version, commit and the root key
// fingerprint come from the manifest. The fingerprint of the compiled platform root key is computed here, so the page
// shows whether the manifest belongs to the running code's key.
import { b64uDecode } from '../../../core/encoding/b64u';

export const MANIFEST_PATH = '/build-manifest.json';

export interface BuildInfo {
  /** SHA-256 (hex) of the manifest bytes: the build hash. */
  readonly hash: string;
  readonly version: string;
  readonly commit: string;
  /** SHA-256 (hex) of the root key the manifest names. */
  readonly rootKeyFingerprint: string;
}

/** Lower-case hex of SHA-256 over `bytes`. */
export async function sha256Hex(bytes: BufferSource): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return [...digest].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** `hex` in groups of `size` characters separated by spaces (`1a2b 3c4d …`), easier to compare by eye. */
export function groupHex(hex: string, size = 4): string {
  return (hex.match(new RegExp(`.{1,${size}}`, 'g')) ?? []).join(' ');
}

/** SHA-256 (hex) of the 32-byte Ed25519 platform root public key (b64u), as `build-manifest.json` records it. */
export function rootKeyFingerprint(rootKey: string): Promise<string> {
  return sha256Hex(b64uDecode(rootKey, 32));
}

const isText = (value: unknown): value is string => typeof value === 'string' && value !== '';
const HEX64 = /^[0-9a-f]{64}$/;

/** The build of the manifest bytes, or null if they are no manifest. */
export async function readManifest(bytes: Uint8Array<ArrayBuffer>): Promise<BuildInfo | null> {
  let manifest: unknown;
  try {
    manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
  if (typeof manifest !== 'object' || manifest === null) return null;
  const { version, commit, root_key_fingerprint: fingerprint } = manifest as Record<string, unknown>;
  if (!isText(version) || !isText(commit) || !isText(fingerprint) || !HEX64.test(fingerprint)) return null;
  return { hash: await sha256Hex(bytes), version, commit, rootKeyFingerprint: fingerprint };
}

/** Loads the manifest the hub delivers (revalidated, never from a cache); null if it is missing or malformed. */
export async function loadBuild(fetch: typeof globalThis.fetch): Promise<BuildInfo | null> {
  try {
    const response = await fetch(MANIFEST_PATH, { cache: 'no-store', credentials: 'omit' });
    if (!response.ok) return null;
    return await readManifest(new Uint8Array(await response.arrayBuffer()));
  } catch {
    return null;
  }
}
