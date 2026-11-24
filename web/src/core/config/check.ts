// Start-up check against the hub configuration (06-web.md section 10.4, ADR-023): the platform root key compiled
// into the build must equal `platform_root_key` of `GET /v1/config`, and the hub's origin and RP ID must be the
// origin the page was loaded from. Any mismatch is a blocking error; nothing else may run against such a hub.
import { isRecord } from '../registry';

export interface PlatformConfig {
  readonly rp_id: string;
  readonly origin: string;
  readonly platform_root_key: string;
  readonly hub_key: { readonly id: string; readonly public_key: string };
  readonly version: string;
  readonly limits: Readonly<Record<string, number>>;
  readonly models: Readonly<Record<string, readonly string[]>>;
  readonly defaults: Readonly<Record<string, string>>;
}

export type ConfigErrorCode = 'platform_root_mismatch' | 'origin_mismatch' | 'unavailable';

export type ConfigResult =
  { readonly ok: true; readonly config: PlatformConfig } | { readonly ok: false; readonly code: ConfigErrorCode };

export interface ConfigCheckInput {
  readonly fetch: typeof globalThis.fetch;
  /** b64u Ed25519 public key compiled into the build (`VITE_PLATFORM_ROOT_KEY`). */
  readonly rootKey: string;
  /** `location.origin` of the page. */
  readonly origin: string;
}

export const CONFIG_PATH = '/v1/config';

const isText = (value: unknown): value is string => typeof value === 'string';

/** Structural check of the fields the client relies on; the full schema is contracts/api/config.response.json. */
export function isPlatformConfig(value: unknown): value is PlatformConfig {
  return (
    isRecord(value) &&
    isText(value.rp_id) &&
    isText(value.origin) &&
    isText(value.platform_root_key) &&
    isRecord(value.hub_key) &&
    isText(value.hub_key.id) &&
    isText(value.hub_key.public_key) &&
    isText(value.version) &&
    isRecord(value.limits) &&
    isRecord(value.models) &&
    isRecord(value.defaults)
  );
}

async function load(fetch: typeof globalThis.fetch): Promise<unknown> {
  try {
    // the same request as the `<link rel="preload" as="fetch" crossorigin="anonymous">` of index.html, so the browser
    // answers it from the preload that started with the page (06-web.md section 12.2); the hub sends `no-store`
    const response = await fetch(CONFIG_PATH, { method: 'GET', credentials: 'same-origin' });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

export async function checkConfig({ fetch, rootKey, origin }: ConfigCheckInput): Promise<ConfigResult> {
  const config = await load(fetch);
  if (!isPlatformConfig(config)) return { ok: false, code: 'unavailable' };
  if (rootKey === '' || config.platform_root_key !== rootKey) return { ok: false, code: 'platform_root_mismatch' };
  if (config.origin !== origin || config.rp_id !== new URL(origin).hostname) {
    return { ok: false, code: 'origin_mismatch' };
  }
  return { ok: true, config };
}
