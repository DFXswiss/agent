import { describe, expect, it, vi } from 'vitest';
import { CONFIG_PATH, checkConfig, isPlatformConfig } from '../../src/core/config/check';
import valid from '../../../contracts/fixtures/api/config.response/valid/basic.json';

const ROOT = valid.platform_root_key;
const ORIGIN = valid.origin;

function respond(body: unknown, init: ResponseInit = { status: 200 }) {
  return vi.fn<typeof fetch>(async () => new Response(JSON.stringify(body), init));
}

describe('checkConfig', () => {
  it('accepts the contract fixture when key and origin match', async () => {
    const fetch = respond(valid);
    expect(await checkConfig({ fetch, rootKey: ROOT, origin: ORIGIN })).toEqual({ ok: true, config: valid });
    // the request of the preload in index.html (06-web.md section 12.2)
    expect(fetch).toHaveBeenCalledWith(CONFIG_PATH, { method: 'GET', credentials: 'same-origin' });
  });

  it('blocks a different platform root key', async () => {
    const result = await checkConfig({ fetch: respond(valid), rootKey: 'A'.repeat(43), origin: ORIGIN });
    expect(result).toEqual({ ok: false, code: 'platform_root_mismatch' });
  });

  it('blocks a build without a compiled key', async () => {
    const result = await checkConfig({ fetch: respond(valid), rootKey: '', origin: ORIGIN });
    expect(result).toEqual({ ok: false, code: 'platform_root_mismatch' });
  });

  it('blocks a foreign origin', async () => {
    const result = await checkConfig({ fetch: respond(valid), rootKey: ROOT, origin: 'https://evil.example' });
    expect(result).toEqual({ ok: false, code: 'origin_mismatch' });
  });

  it('blocks a foreign RP ID', async () => {
    const fetch = respond({ ...valid, rp_id: 'other.example' });
    expect(await checkConfig({ fetch, rootKey: ROOT, origin: ORIGIN })).toEqual({ ok: false, code: 'origin_mismatch' });
  });

  it('reports an unavailable hub', async () => {
    const failing = vi.fn<typeof fetch>(async () => {
      throw new TypeError('network');
    });
    expect(await checkConfig({ fetch: failing, rootKey: ROOT, origin: ORIGIN })).toEqual({
      ok: false,
      code: 'unavailable',
    });
    const status = respond({ error: 'unavailable' }, { status: 503 });
    expect(await checkConfig({ fetch: status, rootKey: ROOT, origin: ORIGIN })).toEqual({
      ok: false,
      code: 'unavailable',
    });
    const text = vi.fn<typeof fetch>(async () => new Response('not json'));
    expect(await checkConfig({ fetch: text, rootKey: ROOT, origin: ORIGIN })).toEqual({
      ok: false,
      code: 'unavailable',
    });
    const wrong = respond({ ...valid, version: 1 });
    expect(await checkConfig({ fetch: wrong, rootKey: ROOT, origin: ORIGIN })).toEqual({
      ok: false,
      code: 'unavailable',
    });
  });
});

describe('isPlatformConfig', () => {
  it.each(['rp_id', 'origin', 'platform_root_key', 'version'])('needs the text field %s', (field) => {
    expect(isPlatformConfig({ ...valid, [field]: 1 })).toBe(false);
  });

  it.each(['hub_key', 'limits', 'models', 'defaults'])('needs the object %s', (field) => {
    expect(isPlatformConfig({ ...valid, [field]: 'x' })).toBe(false);
  });

  it('needs the hub key fields', () => {
    expect(isPlatformConfig({ ...valid, hub_key: { id: 1, public_key: 'x' } })).toBe(false);
    expect(isPlatformConfig({ ...valid, hub_key: { id: 'x', public_key: 1 } })).toBe(false);
    expect(isPlatformConfig(null)).toBe(false);
    expect(isPlatformConfig(valid)).toBe(true);
  });
});
