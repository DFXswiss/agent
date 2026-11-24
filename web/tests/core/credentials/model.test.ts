// U-WEB-credentials (WEB-10): model access preference and model choices only from `GET /v1/config` (`models`),
// saved with `PUT /v1/me/settings` without touching the locale.
import { describe, expect, it } from 'vitest';
import type { PlatformConfig } from '../../../src/core/config/check';
import {
  allowDfxAlways,
  allowDfxForTask,
  FALLBACK_DEFAULTS,
  fallbackOf,
  isAccess,
  keptFallback,
  modelChoices,
  modelSettings,
  platformDefault,
  saveModelSettings,
} from '../../../src/core/credentials/model';
import { HubError } from '../../../src/core/net/signedFetch';
import { setConfig } from '../../../src/core/store/slices/config';
import { setProfile } from '../../../src/core/store/slices/me';
import { credWorld } from './world';

const CONFIG = {
  models: {
    platform: ['deepseek/deepseek-v4.1-flash-1017', 'claude-sonnet-4-5'],
    api_key: ['claude-sonnet-4-5', 'claude-opus-4-1'],
    subscription: ['claude-opus-4-1', 'claude-sonnet-4-5'],
  },
  defaults: { platform: 'deepseek/deepseek-v4.1-flash-1017' },
} as unknown as PlatformConfig;

const codeOf = async (run: () => Promise<unknown>) => {
  try {
    await run();
  } catch (error) {
    return (error as { code: string }).code;
  }
  return 'none';
};

describe('choices', () => {
  it('offers exactly the configured IDs per access kind, and their intersection for auto', () => {
    expect(modelChoices(CONFIG, 'platform')).toEqual(['deepseek/deepseek-v4.1-flash-1017', 'claude-sonnet-4-5']);
    expect(modelChoices(CONFIG, 'api_key')).toEqual(['claude-sonnet-4-5', 'claude-opus-4-1']);
    expect(modelChoices(CONFIG, 'auto')).toEqual(['claude-sonnet-4-5']);
    expect(modelChoices(null, 'auto')).toEqual([]);
    expect(modelChoices({ ...CONFIG, models: { api_key: 'x' } } as never, 'api_key')).toEqual([]);
    expect(platformDefault(CONFIG)).toBe('deepseek/deepseek-v4.1-flash-1017');
    expect(platformDefault(null)).toBeNull();
    expect(isAccess('platform')).toBe(true);
    expect(isAccess('anything')).toBe(false);
  });

  it('reads the stored settings with safe defaults', async () => {
    const t = await credWorld();
    setProfile(t.store, { settings: { model_access: 'api_key', model_id: 'claude-opus-4-1', locale: 'de' } });
    expect(modelSettings(t.store)).toEqual({ access: 'api_key', modelId: null });
    setConfig(t.store, CONFIG);
    expect(modelSettings(t.store)).toEqual({ access: 'api_key', modelId: 'claude-opus-4-1' });
    setProfile(t.store, { settings: { model_access: 'platform', model_id: 'claude-opus-4-1', locale: 'de' } });
    expect(modelSettings(t.store)).toEqual({ access: 'platform', modelId: null });
    setProfile(t.store, { settings: { model_access: 'nope', model_id: 3 } });
    expect(modelSettings(t.store)).toEqual({ access: 'auto', modelId: null });
    setProfile(t.store, {});
    expect(modelSettings(t.store)).toEqual({ access: 'auto', modelId: null });
    t.store.update('me', (me) => ({ ...me, profile: null }));
    expect(modelSettings(t.store)).toEqual({ access: 'auto', modelId: null });
  });
});

describe('saveModelSettings', () => {
  it('stores the preference and the model with the locale unchanged', async () => {
    const t = await credWorld();
    setConfig(t.store, CONFIG);
    t.client.on('PUT /v1/me/settings', (body) => body);
    const saved = await saveModelSettings(t.context, { access: 'api_key', modelId: 'claude-opus-4-1' }, 'de');
    expect(saved).toEqual({ access: 'api_key', modelId: 'claude-opus-4-1' });
    expect(t.client.of('PUT /v1/me/settings')[0]!.body).toEqual({
      model_access: 'api_key',
      model_id: 'claude-opus-4-1',
      locale: 'de',
    });
    expect(modelSettings(t.store)).toEqual({ access: 'api_key', modelId: 'claude-opus-4-1' });
    await saveModelSettings(t.context, { access: 'platform', modelId: null }, 'en');
    expect(modelSettings(t.store)).toEqual({ access: 'platform', modelId: null });
  });

  it('refuses a model the configuration does not allow and unexpected answers; writes nothing after a sign-out', async () => {
    const t = await credWorld();
    setConfig(t.store, CONFIG);
    t.client.on('PUT /v1/me/settings', () => ({ model_access: 'auto', model_id: null, locale: 'en' }));
    expect(await codeOf(() => saveModelSettings(t.context, { access: 'auto', modelId: 'claude-opus-4-1' }, 'en'))).toBe(
      'params_invalid',
    );
    expect(t.client.of('PUT /v1/me/settings')).toHaveLength(0);
    expect(await codeOf(() => saveModelSettings(t.context, { access: 'platform', modelId: null }, 'en'))).toBe(
      'unavailable',
    );
    t.client.on('PUT /v1/me/settings', () => ({ model_access: 'platform', model_id: 'x', locale: 'en' }));
    expect(await codeOf(() => saveModelSettings(t.context, { access: 'platform', modelId: null }, 'en'))).toBe(
      'unavailable',
    );
    t.client.on('PUT /v1/me/settings', () => null);
    expect(await codeOf(() => saveModelSettings(t.context, { access: 'platform', modelId: null }, 'en'))).toBe(
      'unavailable',
    );
    t.client.on('PUT /v1/me/settings', () => {
      throw new HubError('request_invalid', 400);
    });
    expect(await codeOf(() => saveModelSettings(t.context, { access: 'platform', modelId: null }, 'en'))).toBe(
      'request_invalid',
    );
    t.client.on('PUT /v1/me/settings', (body) => {
      t.store.reset();
      return body;
    });
    await saveModelSettings(t.context, { access: 'platform', modelId: null }, 'en');
    expect(t.store.getState().me.profile).toBeNull();
  });
});

describe('fallback settings (ADR-080 section 1)', () => {
  const FALLBACK = { own_to_dfx: true, operator_to_platform: false };

  it('reads the stored fallback, the defaults for a missing flag, nothing from an older hub', async () => {
    expect(fallbackOf({})).toBeNull();
    expect(fallbackOf({ fallback: 'x' })).toBeNull();
    expect(fallbackOf({ fallback: FALLBACK })).toEqual({ ownToDfx: true, operatorToPlatform: false });
    expect(fallbackOf({ fallback: { own_to_dfx: 1 } })).toEqual(FALLBACK_DEFAULTS);
    expect(keptFallback({})).toEqual({});
    expect(keptFallback({ fallback: FALLBACK })).toEqual({ fallback: FALLBACK });
    const t = await credWorld();
    setProfile(t.store, { settings: { model_access: 'auto', model_id: null, locale: 'de', fallback: FALLBACK } });
    expect(modelSettings(t.store)).toEqual({
      access: 'auto',
      modelId: null,
      fallback: { ownToDfx: true, operatorToPlatform: false },
    });
  });

  it('saves the fallback with the preference and refuses an answer that does not echo it', async () => {
    const t = await credWorld();
    setConfig(t.store, CONFIG);
    t.client.on('PUT /v1/me/settings', (body) => body);
    const settings = {
      access: 'auto' as const,
      modelId: null,
      fallback: { ownToDfx: false, operatorToPlatform: false },
    };
    expect(await saveModelSettings(t.context, settings, 'de')).toEqual(settings);
    expect(t.client.of('PUT /v1/me/settings')[0]!.body).toEqual({
      model_access: 'auto',
      model_id: null,
      locale: 'de',
      fallback: { own_to_dfx: false, operator_to_platform: false },
    });
    expect(modelSettings(t.store).fallback).toEqual({ ownToDfx: false, operatorToPlatform: false });
    for (const echoed of [
      undefined,
      { own_to_dfx: true, operator_to_platform: false },
      { own_to_dfx: false, operator_to_platform: true },
    ]) {
      t.client.on('PUT /v1/me/settings', (body) => ({ ...(body as object), fallback: echoed }));
      expect(await codeOf(() => saveModelSettings(t.context, settings, 'de'))).toBe('unavailable');
    }
  });

  it('"Always" switches own_to_dfx on and keeps the stored preference and model as they are', async () => {
    const t = await credWorld();
    t.client.on('PUT /v1/me/settings', (body) => body);
    setProfile(t.store, {
      settings: { model_access: 'subscription', model_id: 'claude-opus-4-1', locale: 'de', fallback: FALLBACK },
    });
    await allowDfxAlways(t.context, 'de');
    expect(t.client.of('PUT /v1/me/settings')[0]!.body).toEqual({
      model_access: 'subscription',
      model_id: 'claude-opus-4-1',
      locale: 'de',
      fallback: FALLBACK,
    });
    // nothing stored yet (or an older hub): the defaults with own_to_dfx on
    setProfile(t.store, { settings: { model_access: 'nope' } });
    await allowDfxAlways(t.context, 'en');
    expect(t.client.of('PUT /v1/me/settings')[1]!.body).toEqual({
      model_access: 'auto',
      model_id: null,
      locale: 'en',
      fallback: { own_to_dfx: true, operator_to_platform: true },
    });
    t.store.update('me', (me) => ({ ...me, profile: null }));
    await allowDfxAlways(t.context, 'en');
    expect(t.client.of('PUT /v1/me/settings')).toHaveLength(3);
  });

  it('gives the consent of a task and refuses an unexpected answer', async () => {
    const t = await credWorld();
    t.client.on('POST /v1/tasks/t-1/model-consent', () => ({ task_id: 't-1', model_consent: true }));
    await allowDfxForTask(t.context, 't-1');
    expect(t.client.of('POST /v1/tasks/t-1/model-consent')[0]!.body).toEqual({});
    t.client.on('POST /v1/tasks/t-1/model-consent', () => ({ task_id: 't-1' }));
    expect(await codeOf(() => allowDfxForTask(t.context, 't-1'))).toBe('unavailable');
    t.client.on('POST /v1/tasks/t-1/model-consent', () => null);
    expect(await codeOf(() => allowDfxForTask(t.context, 't-1'))).toBe('unavailable');
  });
});
