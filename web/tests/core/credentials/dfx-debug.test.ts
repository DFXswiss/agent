// U-WEB-credentials-dfx-debug (DBG-01, ADR-102 section 2): the DFX debug sign-in is a credential kind of its own;
// the web checks its body exactly as contracts/credentials/dfx_debug.json allows and shows only the API URL and the
// released workgroups, never the wallet address or the signature.
import { describe, expect, it } from 'vitest';
import { isCredentialKind, summarize, validBody } from '../../../src/core/credentials/records';
import { KINDS } from '../../../src/core/crypto/credential';

const WG = '01930001-0000-7000-8000-0000000000aa';
const BODY = { address: 'wallet-address', signature: 'signature-secret', api_url: 'https://api.example.com/v1' };
const many = Array.from(
  { length: 51 },
  (_, i) => `01930001-0000-7000-8000-0000000001${i.toString(16).padStart(2, '0')}`,
);

describe('the DFX debug sign-in', () => {
  it('is a known kind', () => {
    expect(KINDS).toContain('dfx_debug');
    expect(isCredentialKind('dfx_debug')).toBe(true);
  });

  it('accepts exactly the body of the contract', () => {
    expect(validBody('dfx_debug', BODY)).toBe(true);
    expect(validBody('dfx_debug', { ...BODY, use: { wgs: [WG] } })).toBe(true);
    expect(validBody('dfx_debug', { ...BODY, use: { wgs: [] } })).toBe(true);
    expect(validBody('dfx_debug', { ...BODY, api_url: 'https://api.example.com:8443/v1/x' })).toBe(true);
    for (const broken of [
      { api_url: 'http://api.example.com/v1' },
      { api_url: 'https://api.example.com/v1?x=1' },
      { api_url: 7 },
      { address: '' },
      { address: 'with space' },
      { address: 7 },
      { signature: '' },
      { signature: 7 },
      { use: { wgs: [WG, WG] } },
      { use: { wgs: ['not-a-uuid'] } },
      { use: { wgs: [7] } },
      { use: { wgs: 'x' } },
      { use: { wgs: many } },
      { use: { wgs: [], extra: 1 } },
      { use: 'x' },
      { extra: 1 },
    ]) {
      expect(validBody('dfx_debug', { ...BODY, ...broken })).toBe(false);
    }
    expect(validBody('dfx_debug', 'x')).toBe(false);
  });

  it('is summarized without address or signature', () => {
    expect(summarize('dfx_debug', BODY)).toEqual({ kind: 'dfx_debug', apiUrl: BODY.api_url, wgs: [] });
    const released = summarize('dfx_debug', { ...BODY, use: { wgs: [WG] } });
    expect(released).toEqual({ kind: 'dfx_debug', apiUrl: BODY.api_url, wgs: [WG] });
    expect(JSON.stringify(released)).not.toContain('signature-secret');
    expect(JSON.stringify(released)).not.toContain('wallet-address');
  });
});
