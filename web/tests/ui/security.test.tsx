// U-WEB-ui-security: the build helpers of the security page (hash of the manifest bytes, grouped hex, root key
// fingerprint, manifest shape) and the page itself (06-web.md section 10.5) against a manifest that matches the
// compiled root key, one that names another key, and a missing one.
import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { b64uDecode } from '../../src/core/encoding/b64u';
import {
  groupHex,
  loadBuild,
  MANIFEST_PATH,
  readManifest,
  rootKeyFingerprint,
  sha256Hex,
} from '../../src/ui/screens/security/build';
import { CONFIG, SimHub } from '../core/account/hub';
import { renderApp } from './harness';

const encode = (value: unknown) => new TextEncoder().encode(typeof value === 'string' ? value : JSON.stringify(value));
const COMMIT = '0123456789abcdef0123456789abcdef01234567';

async function manifest(fingerprint?: string) {
  return {
    version: '0.1.0',
    commit: COMMIT,
    files: [],
    root_key_fingerprint: fingerprint ?? (await rootKeyFingerprint(CONFIG.platform_root_key)),
  };
}

/** A hub that also delivers `bytes` (or `status`) as the build manifest. */
function hubWithManifest(answer: { bytes?: Uint8Array<ArrayBuffer>; status?: number }): SimHub {
  const hub = new SimHub();
  const base = hub.fetch;
  Object.defineProperty(hub, 'fetch', {
    value: async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input) === MANIFEST_PATH
        ? new Response(answer.bytes ?? null, { status: answer.status ?? 200 })
        : base(input, init),
  });
  return hub;
}

describe('build helpers', () => {
  it('hashes bytes and groups hex for reading', async () => {
    expect(await sha256Hex(encode('abc'))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(groupHex('0123456789')).toBe('0123 4567 89');
    expect(groupHex('abcdef', 2)).toBe('ab cd ef');
    expect(groupHex('')).toBe('');
  });

  it('fingerprints the 32-byte root key like the build manifest', async () => {
    const digest = await sha256Hex(b64uDecode(CONFIG.platform_root_key, 32));
    expect(await rootKeyFingerprint(CONFIG.platform_root_key)).toBe(digest);
  });

  it('reads a manifest and refuses anything else', async () => {
    const good = await manifest();
    const bytes = encode(good);
    expect(await readManifest(bytes)).toEqual({
      hash: await sha256Hex(bytes),
      version: '0.1.0',
      commit: COMMIT,
      rootKeyFingerprint: good.root_key_fingerprint,
    });
    expect(await readManifest(encode('not json'))).toBeNull();
    expect(await readManifest(new Uint8Array([0x7b, 0xff, 0x7d]))).toBeNull();
    expect(await readManifest(encode('null'))).toBeNull();
    expect(await readManifest(encode('"text"'))).toBeNull();
    expect(await readManifest(encode({ ...good, version: '' }))).toBeNull();
    expect(await readManifest(encode({ ...good, commit: 7 }))).toBeNull();
    expect(await readManifest(encode({ ...good, root_key_fingerprint: 'ABC' }))).toBeNull();
  });

  it('loads the delivered manifest without a cache; failures are null', async () => {
    const bytes = encode(await manifest());
    const fetch = vi.fn(async () => new Response(bytes));
    expect((await loadBuild(fetch))?.hash).toBe(await sha256Hex(bytes));
    expect(fetch).toHaveBeenCalledWith(MANIFEST_PATH, { cache: 'no-store', credentials: 'omit' });
    expect(await loadBuild(async () => new Response('', { status: 404 }))).toBeNull();
    expect(
      await loadBuild(async () => {
        throw new TypeError('offline');
      }),
    ).toBeNull();
  });
});

describe('security page', () => {
  it('shows the build hash, commit, root key fingerprint and hub key ID', async () => {
    const bytes = encode(await manifest());
    await renderApp('/security', { hub: hubWithManifest({ bytes }) });
    const hash = await sha256Hex(bytes);
    await waitFor(() => expect(screen.getByTestId('security.build-hash').textContent).toBe(groupHex(hash)));
    expect(screen.getByTestId('security.version').textContent).toBe('0.1.0');
    expect(screen.getByTestId('security.commit').textContent).toBe(COMMIT);
    expect(screen.getByTestId('security.root-key').textContent).toBe(
      groupHex(await rootKeyFingerprint(CONFIG.platform_root_key)),
    );
    expect(screen.getByTestId('security.hub-key').textContent).toBe(CONFIG.hub_key.id);
    expect(screen.queryByTestId('security.root-key.mismatch')).toBeNull();
    for (const part of ['sees', 'hidden', 'javascript', 'devices', 'community']) {
      expect(screen.getByTestId(`security.${part}`)).toBeTruthy();
    }
    // community messages are named as not confidential (12-community.md section 6)
    expect(screen.getByTestId('security.community')).toHaveTextContent('not confidential');
  });

  it('flags a manifest that names another root key', async () => {
    const bytes = encode(await manifest('f'.repeat(64)));
    await renderApp('/security', { hub: hubWithManifest({ bytes }) });
    expect((await screen.findByTestId('security.root-key.mismatch')).getAttribute('role')).toBe('alert');
  });

  it('says so when the manifest is missing', async () => {
    const view = await renderApp('/security', { hub: hubWithManifest({ status: 404 }), languages: ['de'] });
    await waitFor(() => expect(screen.getByTestId('security.build-hash').textContent).toMatch(/^Nicht verfügbar/));
    expect(screen.getByTestId('security.commit').textContent).toMatch(/^Nicht verfügbar/);
    view.view.unmount();
  });

  it('ignores answers that arrive after the page was left', async () => {
    const bytes = encode(await manifest());
    const view = await renderApp('/security', { hub: hubWithManifest({ bytes }) });
    view.view.unmount();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId('security.screen')).toBeNull();
  });
});
