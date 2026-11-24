// Delegated agent access (11-agent-access.md section 3): the edges of `crypto/agent.ts` next to the shared vectors
// (tests/core/crypto/vector-suite.ts): lengths, a fresh seal round trip, the label rules and a non-canonical label body.
import { describe, expect, it } from 'vitest';
import * as agent from '../../../src/core/crypto/agent';
import * as hpke from '../../../src/core/crypto/hpke';
import { Identity } from '../../../src/core/crypto/keys';
import { Secret } from '../../../src/core/crypto/secret';
import { utf8 } from '../../../src/core/encoding/bytes';
import { codeOf } from './helpers';

const AGENT = '01a1ee56-5e00-7242-9e24-f87629ad2977';
const USER = '01a1ee56-5e00-77be-8cf3-1f27bb0e92a2';

describe('agent crypto', () => {
  it('refuses keys and secrets of the wrong length', async () => {
    const key = new Uint8Array(32);
    expect(await codeOf(() => agent.fingerprint(new Uint8Array(31), key))).toBe('header_invalid');
    expect(await codeOf(() => agent.fingerprint(key, new Uint8Array(33)))).toBe('header_invalid');
    expect(await codeOf(() => agent.claim(Secret.random(16), AGENT, USER))).toBe('header_invalid');
    expect(await codeOf(() => agent.bundleKey(new Uint8Array(31), AGENT, USER))).toBe('header_invalid');
    expect(await codeOf(() => agent.claimHash(new Uint8Array(16)))).toBe('header_invalid');
    expect(() => agent.popText(AGENT, key, new Uint8Array(1))).toThrow('header_invalid');
  });

  it('seals and opens a fresh identity with a random nonce and ephemeral key', async () => {
    const holder = await Identity.generate();
    const user = await Identity.generate();
    const secret = Secret.random(32);
    const sealed = await agent.sealIdentity(holder.kemPublic(), secret, AGENT, USER, user);
    const opened = await agent.openIdentity(holder.kem, secret, AGENT, USER, sealed);
    expect(opened.public()).toEqual(user.public());
    expect(await codeOf(() => agent.openIdentity(holder.kem, Secret.random(32), AGENT, USER, sealed))).toBe(
      'decrypt_failed',
    );
    const label = await agent.sealLabel(user.kemPublic(), AGENT, USER, { name: 'Claude Code', host: 'Büro-Mac' });
    expect(await agent.openLabel(user.kem, AGENT, USER, label)).toEqual({ host: 'Büro-Mac', name: 'Claude Code' });
  });

  it('checks label texts by code point and category', () => {
    expect(agent.isLabelText('🙂'.repeat(64))).toBe(true);
    expect(agent.isLabelText('🙂'.repeat(65))).toBe(false);
    expect(agent.isLabelText('a b')).toBe(true);
    expect(agent.isLabelText('a ')).toBe(false);
    expect(agent.isLabelText(7)).toBe(false);
    expect(agent.isLabelText('')).toBe(false);
    expect(agent.isLabelText('\uD800')).toBe(false);
    expect(agent.isLabelText('a b')).toBe(false);
    expect(() => agent.checkLabel({ host: 'h', name: 3 })).toThrow('header_invalid');
    expect(() => agent.checkLabel(['h', 'n'])).toThrow('header_invalid');
  });

  it('refuses a label body that is not the exact body JSON', async () => {
    const user = await Identity.generate();
    const sealed = await hpke.seal(user.kemPublic(), agent.labelInfo(AGENT, USER), utf8('{"name":"n","host":"h"}'));
    const text = Buffer.from(sealed).toString('base64url');
    expect(await codeOf(() => agent.openLabel(user.kem, AGENT, USER, text))).toBe('header_invalid');
  });
});
