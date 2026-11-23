// U-WEB-credentials (WEB-10): model access preference and model choices only from `GET /v1/config` (`models`),
// saved with `PUT /v1/me/settings` without touching the locale.
import { describe, expect, it } from 'vitest';
import type { PlatformConfig } from '../../../src/core/config/check';
import {
  isAccess,
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
