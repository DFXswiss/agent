// The strict pairing link of `/connect#…` (11-agent-access.md section 5.1) and the invitation of an agent (section 5.2).
import { describe, expect, it } from 'vitest';
import { pairingFragment, parseInviteFragment, parsePairingLink, parsePort } from '../../../src/core/agents/link';

const ID = '01a1ee56-5e00-7242-9e24-f87629ad2977';
const FP = 'CzOH3lkVGYJwp1q24gW2DQ';
const valid = `p=${ID}&f=${FP}&c=40123&a=Claude+Code&h=B%C3%BCro-Mac`;
const remote = `p=${ID}&f=${FP}&m=remote&a=Claude+Code&h=build-server`;

describe('parsePairingLink', () => {
  it('reads the five parameters (form encoding)', () => {
    expect(parsePairingLink(valid)).toEqual({
      mode: 'loopback',
      pairingId: ID,
      fingerprint: FP,
      port: 40123,
      name: 'Claude Code',
      host: 'Büro-Mac',
    });
  });

  it('refuses anything else', () => {
    for (const text of [
      '',
      `${valid}&x=1`,
      `${valid}&a=again`,
      `p=${ID}&f=${FP}&c=40123&a=x&x=h`,
      valid.replace(ID, 'not-a-uuid'),
      valid.replace(FP, 'short'),
      valid.replace(FP, 'CzOH3lkVGYJwp1q24gW2DR'),
      valid.replace(FP, 'CzOH3lkVGYJwp1q24gW2D*'),
      valid.replace('40123', '80'),
      valid.replace('Claude+Code', '+Claude'),
      valid.replace('B%C3%BCro-Mac', 'a%E2%80%AEb'),
    ]) {
      expect(parsePairingLink(text)).toBeNull();
    }
  });

  it('reads a link from another computer: m=remote instead of the port (ADR-062)', () => {
    const link = parsePairingLink(remote);
    expect(link).toEqual({
      mode: 'remote',
      pairingId: ID,
      fingerprint: FP,
      port: null,
      name: 'Claude Code',
      host: 'build-server',
    });
    expect(parsePairingLink(pairingFragment(link!))).toEqual(link);
    expect(parsePairingLink(pairingFragment(parsePairingLink(valid)!))).toEqual(parsePairingLink(valid));
    for (const text of [
      `${remote}&c=40123`,
      remote.replace('m=remote', 'm=loopback'),
      remote.replace('m=remote', 'm=REMOTE'),
      remote.replace('m=remote', 'm='),
      `${remote}&m=remote`,
      remote.replace(ID, 'x'),
      remote.replace('build-server', '+x'),
    ]) {
      expect(parsePairingLink(text), text).toBeNull();
    }
  });
});

describe('parsePort', () => {
  it('takes 1024–65535 in canonical decimal form', () => {
    expect(parsePort('1024')).toBe(1024);
    expect(parsePort('65535')).toBe(65535);
    expect(parsePort('1023')).toBeNull();
    expect(parsePort('65536')).toBeNull();
    expect(parsePort('01024')).toBeNull();
    expect(parsePort('99999')).toBeNull();
    expect(parsePort('4e3')).toBeNull();
  });
});

describe('parseInviteFragment', () => {
  const pairing = parsePairingLink(valid);
  const CODE = 'ABCDE-FGHIJ';

  it('leaves a plain invitation unchanged', () => {
    expect(parseInviteFragment(CODE)).toEqual({ code: CODE, pairing: null, displayName: null, valid: true });
    expect(parseInviteFragment('')).toEqual({ code: '', pairing: null, displayName: null, valid: true });
  });

  it('reads the pairing after the code, with and without a display name', () => {
    expect(parseInviteFragment(`${CODE}&${valid}`)).toEqual({ code: CODE, pairing, displayName: null, valid: true });
    expect(parseInviteFragment(`${CODE}&u=+Ada+Lovelace+&${valid}`)).toEqual({
      code: CODE,
      pairing,
      displayName: 'Ada Lovelace',
      valid: true,
    });
  });

  it('reads a remote pairing after the code', () => {
    expect(parseInviteFragment(`${CODE}&${remote}&u=Ada`)).toEqual({
      code: CODE,
      pairing: parsePairingLink(remote),
      displayName: 'Ada',
      valid: true,
    });
  });

  it('refuses parameters that are not exactly a pairing and one name of 1–80 characters', () => {
    const invalid = { code: CODE, pairing: null, displayName: null, valid: false };
    for (const rest of [
      `${valid}&x=1`,
      `u=Ada&u=Bob&${valid}`,
      `u=+&${valid}`,
      `u=${'a'.repeat(81)}&${valid}`,
      'u=Ada',
      '',
    ]) {
      expect(parseInviteFragment(`${CODE}&${rest}`), rest).toEqual(invalid);
    }
    expect(parseInviteFragment(`${CODE}&u=${'a'.repeat(80)}&${valid}`).displayName).toHaveLength(80);
  });
});
