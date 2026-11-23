// Entry formats (04-crypto.md sections 4.7 and 4.8, contracts/common.json log_body_* / platform_body_*): every
// field check refuses a wrong value with `header_invalid`, the key sets are exact, and the check order of
// contracts/vectors/README.md holds (object and type first, `log_type` before the key set).
import { describe, expect, it } from 'vitest';
import { CryptoError } from '../../../src/core/crypto/errors';
import { checkLogEntry, checkPlatformEntry, type Body } from '../../../src/core/log/entries';
import { appendEntries } from '../../../src/core/log/verify';
import { VECTOR_FILES } from '../crypto/vector-suite';

const MAIN = VECTOR_FILES.log.logs.main as { body: Body; sig: string; inv_sig?: string }[];
const PLATFORM = VECTOR_FILES['platform-log'].logs.platform as { body: Body; sig: string }[];

const of = <E extends { body: Body }>(entries: E[], type: string): E => entries.find((e) => e.body.type === type)!;
const refusal = (run: () => unknown): string | null => {
  try {
    run();
  } catch (error) {
    return (error as CryptoError).code;
  }
  return null;
};
const withBody = (entry: { body: Body }, changes: Body) => ({ ...entry, body: { ...entry.body, ...changes } });

describe('membership-log entry format', () => {
  it('accepts every entry of the main vector log', () => {
    for (const entry of MAIN) expect(checkLogEntry(entry)).toBe(entry.body);
  });

  it.each([
    ['genesis', { private: 'yes' }],
    ['role', { role: 'owner' }],
    ['invite', { wgs: [] }],
    ['invite', { wgs: { 'not-a-uuid': 'read' } }],
    ['invite', { wgs: { [MAIN[0]!.body.default_wg as string]: 'device' } }],
    ['rotate', { epoch: 0 }],
    ['rotate', { epoch: 2 ** 31 }],
    ['rotate', { scope: 'cred' }],
    ['wg_create', { prev: 'short' }],
    ['grant', { v: 2 }],
    ['accept', { pub: { kem: 'x', sig: 'y' } }],
    ['device_assign', { platform_seq: -1 }],
    ['invite_revoke', { invite_id: 'AAAA' }],
  ])('refuses a wrong %s field %j', (type, changes) => {
    expect(refusal(() => checkLogEntry(withBody(of(MAIN, type), changes)))).toBe('header_invalid');
  });

  it('checks object, type, key sets and ASCII in this order', () => {
    expect(refusal(() => checkLogEntry(null))).toBe('header_invalid');
    expect(refusal(() => checkLogEntry({ body: [] }))).toBe('header_invalid');
    expect(refusal(() => checkLogEntry({ body: { type: 1 } }))).toBe('header_invalid');
    expect(refusal(() => checkLogEntry({ body: { type: 'merge' }, extra: 1 }))).toBe('log_type');
    const accept = of(MAIN, 'accept');
    expect(refusal(() => checkLogEntry({ body: accept.body, sig: accept.sig }))).toBe('header_invalid');
    expect(refusal(() => checkLogEntry({ ...of(MAIN, 'role'), inv_sig: 'x' }))).toBe('header_invalid');
    expect(refusal(() => checkLogEntry(withBody(of(MAIN, 'role'), { extra: 1 })))).toBe('header_invalid');
    expect(refusal(() => checkLogEntry(withBody(of(MAIN, 'role'), { role: 'Ädmin' })))).toBe('non_ascii');
  });
});

describe('platform-log entry format', () => {
  it('accepts every entry of the platform vector log', () => {
    for (const entry of PLATFORM) expect(checkPlatformEntry(entry)).toBe(entry.body);
  });

  it.each([
    ['org_platform_access', { granted: 1 }],
    ['device_certify', { agent_version: '-1' }],
    ['device_certify', { os: 'Linux' }],
    ['device_certify', { arch: 7 }],
    ['device_certify', { enroll_sig: 'AAAA' }],
    ['device_certify', { capacity: { pool_ready: 0 } }],
    ['root', { by: 'someone' }],
    ['role_grant', { role: 'admin' }],
  ])('refuses a wrong %s field %j', (type, changes) => {
    expect(refusal(() => checkPlatformEntry(withBody(of(PLATFORM, type), changes)))).toBe('header_invalid');
  });

  it('refuses an unknown type and an accept-style signature', () => {
    expect(refusal(() => checkPlatformEntry({ body: { type: 'accept' }, sig: '' }))).toBe('log_type');
    expect(refusal(() => checkPlatformEntry({ ...of(PLATFORM, 'root'), inv_sig: 'x' }))).toBe('header_invalid');
  });
});

describe('verified log accessors', () => {
  it('refuses an empty append to an empty log and has no author for an unknown epoch', async () => {
    await expect(appendEntries(null, [], '')).rejects.toThrow('log_empty');
    const log = await appendEntries(null, MAIN.slice(0, 2), VECTOR_FILES.log.founder);
    const wg = MAIN[1]!.body.wg as string;
    expect([...log.epochHolders('wg', wg, 1)]).toEqual([MAIN[0]!.body.by]);
    expect([...log.epochHolders('wg', wg, 7)]).toEqual([]);
  });
});
