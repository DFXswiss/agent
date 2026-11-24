// The runner download of an own Mac (ADR-074 sections 2 and 7).
import { describe, expect, it, vi } from 'vitest';
import { FEED_PATH, loadMacDownload, macDownload } from '../../../src/core/org/download';

const NOW = Date.parse('2026-11-24T12:00:00Z');
const FEED = {
  v: 1,
  version: '2026.11.24.0800',
  package: 'DFX-Agent-Runner-2026.11.24.0800.pkg',
  package_sha256: '0'.repeat(64),
  manifest_sha256: '1'.repeat(64),
  min_macos: '14.0',
  notarized: true,
  signed_at: '2026-11-24T08:00:00Z',
  expires: '2027-02-22T08:00:00Z',
};

describe('macDownload', () => {
  it('offers a notarised, unexpired package of the feed', () => {
    expect(macDownload(FEED, NOW)).toEqual({
      href: '/downloads/runner/DFX-Agent-Runner-2026.11.24.0800.pkg',
      version: '2026.11.24.0800',
    });
  });

  it.each([
    ['no object', null],
    ['a string', 'feed'],
    ['not notarised', { ...FEED, notarized: false }],
    ['no package name', { ...FEED, package: 7 }],
    ['no expiry', { ...FEED, expires: 7 }],
    ['another file name', { ...FEED, package: '../evil.pkg' }],
    ['a version that differs from the name', { ...FEED, version: '2026.11.24.0801' }],
    ['an expired feed', { ...FEED, expires: '2026-11-24T12:00:00Z' }],
    ['an unreadable expiry', { ...FEED, expires: 'soon' }],
  ])('offers nothing for %s', (_name, feed) => {
    expect(macDownload(feed, NOW)).toBeNull();
  });
});

describe('loadMacDownload', () => {
  it('reads the feed of this hub without credentials', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(FEED), { status: 200 }));
    expect(await loadMacDownload(fetcher as unknown as typeof fetch, NOW)).not.toBeNull();
    expect(fetcher).toHaveBeenCalledWith(FEED_PATH, { cache: 'no-cache', credentials: 'omit' });
  });

  it('offers nothing when the hub has no download area, sends no JSON or cannot be reached', async () => {
    const missing = vi.fn(async () => new Response('{"error":"not_found"}', { status: 404 }));
    expect(await loadMacDownload(missing as unknown as typeof fetch, NOW)).toBeNull();
    const garbage = vi.fn(async () => new Response('<html>', { status: 200 }));
    expect(await loadMacDownload(garbage as unknown as typeof fetch, NOW)).toBeNull();
    const offline = vi.fn(async () => {
      throw new TypeError('network');
    });
    expect(await loadMacDownload(offline as unknown as typeof fetch, NOW)).toBeNull();
  });
});
