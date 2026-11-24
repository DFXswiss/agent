// Model access preference and model choice (06-web.md section 8.2, 03-api.md section 4.1, ADR-010): the preference
// `auto | subscription | api_key | platform` and the model ID are stored in the user's settings
// (`PUT /v1/me/settings`, the locale is sent unchanged). Allowed model IDs come only from `GET /v1/config` (`models`,
// per access kind; there is no build-time list):
// - an explicit access kind offers exactly its list;
// - `auto` (subscription → API key → platform, decided per session on the device) offers the IDs every access kind
//   allows, since the device may pick any of them;
// - "default" (`model_id` null) is always offered: the device uses the catalog default of the access kind it picks
//   (`defaults.platform` for the platform).
// `platform` is available only on DFX execution devices (platform devices); the UI says so.
import type { PlatformConfig } from '../config/check';
import type { Locale } from '../i18n/locale';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { setProfile } from '../store/slices/me';
import type { Store } from '../store/store';
import type { RequestsContext } from '../requests/client';
import { RequestError } from '../requests/client';

export const ACCESS_KINDS = ['auto', 'subscription', 'api_key', 'platform'] as const;
export type AccessPreference = (typeof ACCESS_KINDS)[number];
const CONCRETE = ['subscription', 'api_key', 'platform'] as const;

export const isAccess = (value: unknown): value is AccessPreference =>
  (ACCESS_KINDS as readonly unknown[]).includes(value);

export interface ModelSettings {
  readonly access: AccessPreference;
  readonly modelId: string | null;
}

/**
 * The stored settings of `GET /v1/me` (`auto` and the default model when absent or malformed; a stored model the
 * configuration no longer allows for the preference reads as the default).
 */
export function modelSettings(store: Store): ModelSettings {
  const profile = store.getState().me.profile;
  const settings = isRecord(profile?.settings) ? profile.settings : {};
  const access = isAccess(settings.model_access) ? settings.model_access : 'auto';
  const stored = typeof settings.model_id === 'string' ? settings.model_id : null;
  const allowed = stored !== null && modelChoices(store.getState().config.config, access).includes(stored);
  return { access, modelId: allowed ? stored : null };
}

const listOf = (config: PlatformConfig | null, kind: string): readonly string[] => {
  const list = config?.models[kind];
  return Array.isArray(list) ? list.filter((item) => typeof item === 'string') : [];
};

/** The model IDs allowed for `access` (module comment), in catalog order. */
export function modelChoices(config: PlatformConfig | null, access: AccessPreference): readonly string[] {
  if (access !== 'auto') return listOf(config, access);
  const [first, ...rest] = CONCRETE.map((kind) => listOf(config, kind));
  return first!.filter((id) => rest.every((list) => list.includes(id)));
}

/** The default model of the platform (`defaults.platform`), or null. */
export function platformDefault(config: PlatformConfig | null): string | null {
  const found = config?.defaults.platform;
  return typeof found === 'string' ? found : null;
}

/** `PUT /v1/me/settings` with the new preference and model; the locale stays. `params_invalid` for a model the
 * configuration does not allow for the preference. */
export async function saveModelSettings(
  context: RequestsContext,
  settings: ModelSettings,
  locale: Locale,
): Promise<ModelSettings> {
  const { config } = context.store.getState().config;
  if (settings.modelId !== null && !modelChoices(config, settings.access).includes(settings.modelId)) {
    throw new RequestError('params_invalid');
  }
  const generation = context.store.generation;
  const body = { model_access: settings.access, model_id: settings.modelId, locale };
  const saved = await context.client.request('PUT', '/v1/me/settings', { body });
  if (!isRecord(saved) || saved.model_access !== body.model_access || saved.model_id !== body.model_id) {
    throw new HubError('unavailable', 200);
  }
  if (context.store.generation === generation) {
    const profile = context.store.getState().me.profile;
    setProfile(context.store, { ...profile, settings: saved });
  }
  return { access: settings.access, modelId: settings.modelId };
}
