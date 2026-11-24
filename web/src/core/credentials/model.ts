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
//
// The two fallback settings of ADR-080 section 1 are stored with them (`fallback`): `own_to_dfx` (default off: an own
// subscription that is used up never moves to paid DFX access silently) and `operator_to_platform` (default on: from
// used-up operator subscriptions to DFX access over OpenRouter). A hub whose settings carry no `fallback` is of an older
// release: the settings then have none and the body sent to it has none either.
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

export interface FallbackSettings {
  readonly ownToDfx: boolean;
  readonly operatorToPlatform: boolean;
}

/** The fallback settings when none are stored (ADR-080 section 1). */
export const FALLBACK_DEFAULTS: FallbackSettings = { ownToDfx: false, operatorToPlatform: true };

export interface ModelSettings {
  readonly access: AccessPreference;
  readonly modelId: string | null;
  /** Absent for a hub of an older release (module comment). */
  readonly fallback?: FallbackSettings;
}

const flag = (value: unknown, fallback: boolean): boolean => (typeof value === 'boolean' ? value : fallback);

/** The stored fallback settings, null for a hub of an older release (module comment). */
export function fallbackOf(settings: Readonly<Record<string, unknown>>): FallbackSettings | null {
  const stored = settings.fallback;
  if (!isRecord(stored)) return null;
  return {
    ownToDfx: flag(stored.own_to_dfx, FALLBACK_DEFAULTS.ownToDfx),
    operatorToPlatform: flag(stored.operator_to_platform, FALLBACK_DEFAULTS.operatorToPlatform),
  };
}

export interface FallbackBody {
  readonly own_to_dfx: boolean;
  readonly operator_to_platform: boolean;
}

interface SettingsBody {
  readonly model_access: AccessPreference;
  readonly model_id: string | null;
  readonly locale: Locale;
  readonly fallback?: FallbackBody;
}

/** The `fallback` field of a settings body. */
export const fallbackBody = (fallback: FallbackSettings): FallbackBody => ({
  own_to_dfx: fallback.ownToDfx,
  operator_to_platform: fallback.operatorToPlatform,
});

/** The stored `fallback` of `settings` for a body that changes something else (nothing for an older hub). */
export function keptFallback(settings: Readonly<Record<string, unknown>>): { fallback?: FallbackBody } {
  const found = fallbackOf(settings);
  return found === null ? {} : { fallback: fallbackBody(found) };
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
  const fallback = fallbackOf(settings);
  return { access, modelId: allowed ? stored : null, ...(fallback !== null && { fallback }) };
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
  const body: SettingsBody = {
    model_access: settings.access,
    model_id: settings.modelId,
    locale,
    ...(settings.fallback !== undefined && { fallback: fallbackBody(settings.fallback) }),
  };
  await putSettings(context, body);
  return settings;
}

/**
 * `PUT /v1/me/settings`; the answer must echo what was sent (preference, model and the fallback settings when they were
 * sent), and the profile takes it unless the user signed out meanwhile.
 */
async function putSettings(context: RequestsContext, body: SettingsBody): Promise<void> {
  const generation = context.store.generation;
  const saved = await context.client.request('PUT', '/v1/me/settings', { body });
  if (!isRecord(saved) || saved.model_access !== body.model_access || saved.model_id !== body.model_id) {
    throw new HubError('unavailable', 200);
  }
  const echoed = fallbackOf(saved);
  const sent = body.fallback;
  const differs = (found: FallbackSettings, wanted: FallbackBody) =>
    found.ownToDfx !== wanted.own_to_dfx || found.operatorToPlatform !== wanted.operator_to_platform;
  if (sent !== undefined && (echoed === null || differs(echoed, sent))) {
    throw new HubError('unavailable', 200);
  }
  if (context.store.generation === generation) {
    const profile = context.store.getState().me.profile;
    setProfile(context.store, { ...profile, settings: saved });
  }
}

/**
 * "Always" on the card `quota_choice` (ADR-080 section 5): `fallback.own_to_dfx` on, everything else of the stored
 * settings unchanged (the raw stored preference and model, so a configuration not loaded yet changes nothing).
 */
export async function allowDfxAlways(context: RequestsContext, locale: Locale): Promise<void> {
  const profile = context.store.getState().me.profile;
  const settings = isRecord(profile?.settings) ? profile.settings : {};
  const fallback = { ...(fallbackOf(settings) ?? FALLBACK_DEFAULTS), ownToDfx: true };
  await putSettings(context, {
    model_access: isAccess(settings.model_access) ? settings.model_access : 'auto',
    model_id: typeof settings.model_id === 'string' ? settings.model_id : null,
    locale,
    fallback: fallbackBody(fallback),
  });
}

/**
 * "Continue with DFX access" on the card `quota_choice` (ADR-080 section 5): `POST /v1/tasks/{task}/model-consent`
 * (writers of the task's workgroup); the task may use DFX access while its owner's own subscription is used up.
 */
export async function allowDfxForTask(context: RequestsContext, task: string): Promise<void> {
  const answer = await context.client.request('POST', `/v1/tasks/${encodeURIComponent(task)}/model-consent`, {
    body: {},
  });
  if (!isRecord(answer) || answer.model_consent !== true) throw new HubError('unavailable', 200);
}
