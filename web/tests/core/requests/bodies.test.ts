// Request and response bodies of the four device-request kinds against contracts/requests/ (closed key sets, formats).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../../helpers';
import {
  checkResponseBody,
  FAILURE_CODES,
  isRequestKind,
  PROGRESS_STAGES,
  REQUEST_KINDS,
  validRequestBody,
} from '../../../src/core/requests/bodies';

const contract = (name: string) =>
  JSON.parse(readFileSync(join(WEB_DIR, '..', 'contracts', 'requests', name), 'utf8')) as Record<string, unknown>;

describe('closed lists', () => {
  it('match the contracts', () => {
    const objects = contract('objects.json') as { $defs: { failure_code: { enum: string[] } } };
    expect([...FAILURE_CODES]).toEqual(objects.$defs.failure_code.enum);
    const login = contract('claude.login.response.json') as { oneOf: { properties: { stage?: { enum: string[] } } }[] };
    expect([...PROGRESS_STAGES]).toEqual(login.oneOf[1]!.properties.stage!.enum);
    const common = JSON.parse(readFileSync(join(WEB_DIR, '..', 'contracts', 'common.json'), 'utf8'));
    expect([...REQUEST_KINDS]).toEqual(common.$defs.request_kind.enum);
    expect(isRequestKind('github.connect')).toBe(true);
    expect(isRequestKind('github.disconnect')).toBe(false);
  });
});

describe('validRequestBody', () => {
  it('accepts exactly the bodies of each kind', () => {
    expect(validRequestBody('github.connect', {}, false)).toBe(true);
    expect(validRequestBody('github.connect', {}, true)).toBe(false);
    expect(validRequestBody('github.connect', { a: 1 }, false)).toBe(false);
    expect(validRequestBody('mailbox.check', {}, false)).toBe(true);
    expect(validRequestBody('credential.check', { kind: 'anthropic_api_key' }, false)).toBe(true);
    expect(validRequestBody('credential.check', { kind: 'claude_subscription' }, false)).toBe(true);
    expect(validRequestBody('credential.check', { kind: 'github' }, false)).toBe(false);
    expect(validRequestBody('credential.check', { kind: 'anthropic_api_key' }, true)).toBe(false);
    expect(validRequestBody('claude.login', { mode: 'manual' }, false)).toBe(true);
    expect(validRequestBody('claude.login', { mode: 'automatic' }, false)).toBe(true);
    expect(validRequestBody('claude.login', { mode: 'other' }, false)).toBe(false);
    expect(validRequestBody('claude.login', { code: 'abc-123' }, true)).toBe(true);
    expect(validRequestBody('claude.login', { code: 'a b' }, true)).toBe(false);
    expect(validRequestBody('claude.login', { code: 'x'.repeat(513) }, true)).toBe(false);
    expect(validRequestBody('claude.login', { code: 'x', extra: 1 }, true)).toBe(false);
    expect(validRequestBody('claude.login', null, false)).toBe(false);
  });
});

describe('checkResponseBody', () => {
  const code = {
    step: 'device_code',
    user_code: 'ABCD-1234',
    verification_uri: 'https://github.com/login/device',
    expires_in: 900,
  };

  it('accepts the steps of github.connect', () => {
    expect(checkResponseBody('github.connect', code)).toEqual(code);
    expect(
      checkResponseBody('github.connect', { ...code, verification_uri: 'http://127.0.0.1:8080/device' }),
    ).not.toBeNull();
    expect(checkResponseBody('github.connect', { step: 'done', login: 'octo-cat' })).not.toBeNull();
    expect(checkResponseBody('github.connect', { step: 'failed', code: 'timeout' })).not.toBeNull();
  });

  it('refuses malformed github.connect steps', () => {
    for (const bad of [
      { ...code, user_code: 'abcd' },
      { ...code, verification_uri: 'http://github.com/login/device' },
      { ...code, verification_uri: `https://${'a'.repeat(2050)}` },
      { ...code, expires_in: 0 },
      { ...code, expires_in: 3601 },
      { ...code, expires_in: 1.5 },
      { ...code, extra: true },
      { step: 'done', login: 'bad login' },
      { step: 'done' },
      { step: 'failed', code: 'other' },
      { step: 'failed', code: 'timeout', more: 1 },
      { step: 'authorize', url: 'https://example.com' },
      'done',
    ]) {
      expect(checkResponseBody('github.connect', bad)).toBeNull();
    }
  });

  it('accepts and refuses the steps of claude.login', () => {
    expect(checkResponseBody('claude.login', { step: 'authorize', url: 'https://claude.ai/x' })).not.toBeNull();
    expect(
      checkResponseBody('claude.login', { step: 'authorize', url: 'https://claude.ai/x', automation: 'failed' }),
    ).not.toBeNull();
    expect(
      checkResponseBody('claude.login', { step: 'authorize', url: 'https://claude.ai/x', automation: 'ok' }),
    ).toBeNull();
    expect(checkResponseBody('claude.login', { step: 'authorize', url: 'ftp://x' })).toBeNull();
    expect(checkResponseBody('claude.login', { step: 'progress', stage: 'email_entered' })).not.toBeNull();
    expect(checkResponseBody('claude.login', { step: 'progress', stage: 'other' })).toBeNull();
    expect(checkResponseBody('claude.login', { step: 'done' })).not.toBeNull();
    expect(checkResponseBody('claude.login', { step: 'done', email: 'ada@example.com' })).not.toBeNull();
    expect(checkResponseBody('claude.login', { step: 'done', email: 'no-at' })).toBeNull();
    expect(checkResponseBody('claude.login', { step: 'device_code' })).toBeNull();
  });

  it('accepts and refuses the steps of the checks', () => {
    for (const kind of ['credential.check', 'mailbox.check'] as const) {
      expect(checkResponseBody(kind, { step: 'done', valid: false })).not.toBeNull();
      expect(checkResponseBody(kind, { step: 'done', valid: 'yes' })).toBeNull();
      expect(checkResponseBody(kind, { step: 'progress', stage: 'authorized' })).toBeNull();
      expect(checkResponseBody(kind, { step: 'failed', code: 'credential_missing' })).not.toBeNull();
    }
  });
});
