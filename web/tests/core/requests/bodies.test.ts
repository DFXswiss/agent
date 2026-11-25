// Request and response bodies of the four device-request kinds against contracts/requests/ (closed key sets, formats).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../../helpers';
import {
  checkResponseBody,
  FAILURE_CODES,
  isGithubBody,
  isRequestKind,
  PROGRESS_STAGES,
  REQUEST_KINDS,
  SIGNING_OUTCOMES,
  SOURCES,
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
    expect(validRequestBody('github.connect', { token: 'ghp_x' }, false)).toBe(true);
    expect(validRequestBody('github.connect', { token: 'ghp_x' }, true)).toBe(false);
    expect(validRequestBody('github.connect', { token: '' }, false)).toBe(false);
    expect(validRequestBody('github.connect', { token: 'a b' }, false)).toBe(false);
    expect(validRequestBody('github.connect', { token: 5 }, false)).toBe(false);
    expect(validRequestBody('github.connect', { token: 'x'.repeat(4097) }, false)).toBe(false);
    // the device flow of a named app (KOR-65), never together with a token
    expect(validRequestBody('github.connect', { app: 'oauth_app' }, false)).toBe(true);
    expect(validRequestBody('github.connect', { app: 'github_app' }, false)).toBe(true);
    expect(validRequestBody('github.connect', { app: 'gitlab' }, false)).toBe(false);
    expect(validRequestBody('github.connect', { app: 'oauth_app', token: 'ghp_x' }, false)).toBe(false);
    expect(validRequestBody('github.connect', { token: 'x', extra: 1 }, false)).toBe(false);
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
    expect(
      checkResponseBody('github.connect', { step: 'done', login: 'octo-cat', scopes: ['repo'], source: 'token' }),
    ).not.toBeNull();
    expect(checkResponseBody('github.connect', { step: 'done', login: 'o', scopes: [] })).not.toBeNull();
    expect(checkResponseBody('github.connect', { step: 'failed', code: 'sso_required' })).not.toBeNull();
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
      { step: 'done', login: 'o', scopes: 'repo' },
      { step: 'done', login: 'o', scopes: ['Repo Write'] },
      { step: 'done', login: 'o', scopes: Array.from({ length: 65 }, () => 'repo') },
      { step: 'done', login: 'o', source: 'paste' },
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

describe('GitHub identities (KOR-47)', () => {
  const ID = '0190f000-0000-7000-8000-0000000000e1';
  const BODY = {
    token: 'ghp_x',
    login: 'octo-cat',
    user_id: 7,
    scopes: ['repo'],
    created_at: '2026-11-24T12:00:00.000Z',
  };
  const KEY = { alg: 'ssh-ed25519', seed: `${'A'.repeat(42)}w`, public: `ssh-ed25519 ${'A'.repeat(68)} key` };

  it('takes the source operator and the signing outcomes from the contracts', () => {
    const response = contract('github.connect.response.json') as {
      oneOf: { properties: { source?: { enum: string[] }; signing?: { enum: string[] } } }[];
    };
    expect([...SOURCES]).toEqual(response.oneOf[1]!.properties.source!.enum);
    expect([...SIGNING_OUTCOMES]).toEqual(response.oneOf[1]!.properties.signing!.enum);
  });

  it('accepts the request fields org_id, identity_id and signing_key', () => {
    expect(validRequestBody('github.connect', { org_id: ID, identity_id: ID, signing_key: true }, false)).toBe(true);
    expect(validRequestBody('github.connect', { token: 'x', signing_key: false }, false)).toBe(true);
    for (const bad of [{ org_id: 'org' }, { identity_id: ID.toUpperCase() }, { signing_key: 'yes' }]) {
      expect(validRequestBody('github.connect', bad, false)).toBe(false);
    }
  });

  it('accepts the identity, the signing outcome and a credential body in done', () => {
    const done = { step: 'done', login: 'octo-cat', source: 'operator', identity_id: ID, signing: 'scope_missing' };
    expect(checkResponseBody('github.connect', done)).toEqual(done);
    expect(checkResponseBody('github.connect', { ...done, body: BODY })).not.toBeNull();
    for (const bad of [
      { ...done, identity_id: 'x' },
      { ...done, signing: 'maybe' },
      { ...done, body: { ...BODY, token: undefined } },
    ]) {
      expect(checkResponseBody('github.connect', bad)).toBeNull();
    }
  });

  it('checks the credential body exactly', () => {
    expect(isGithubBody(BODY)).toBe(true);
    expect(isGithubBody({ ...BODY, source: 'operator', name: 'Octo Cat', signing_key: KEY })).toBe(true);
    expect(isGithubBody({ ...BODY, signing_key: { ...KEY, github_key_id: 9 } })).toBe(true);
    for (const bad of [
      null,
      { ...BODY, extra: 1 },
      { ...BODY, token: 'a b' },
      { ...BODY, login: 'bad login' },
      { ...BODY, user_id: 0 },
      { ...BODY, scopes: 'repo' },
      { ...BODY, source: 'paste' },
      { ...BODY, created_at: 5 },
      { ...BODY, name: 'a <b>' },
      { ...BODY, signing_key: 'key' },
      { ...BODY, signing_key: { ...KEY, alg: 'ed25519' } },
      { ...BODY, signing_key: { ...KEY, seed: 'short' } },
      { ...BODY, signing_key: { ...KEY, public: 'ssh-rsa AAAA' } },
      { ...BODY, signing_key: { ...KEY, github_key_id: 0 } },
      { ...BODY, signing_key: { ...KEY, extra: 1 } },
    ]) {
      expect(isGithubBody(bad)).toBe(false);
    }
  });
});

describe('github.pull_status without its checks (KOR-48)', () => {
  it('refuses every body until core/pulls/checks.ts registered the kind, except a failure', () => {
    const request = { org_id: '0190f000-0000-7000-8000-0000000000e1', wg_id: '0190f000-0000-7000-8000-0000000000e2' };
    expect(validRequestBody('github.pull_status', { ...request, pulls: [{ repo: 'a/b', number: 1 }] }, false)).toBe(
      false,
    );
    expect(checkResponseBody('github.pull_status', { step: 'done', pulls: [] })).toBeNull();
    expect(checkResponseBody('github.pull_status', { step: 'failed', code: 'github_not_connected' })).not.toBeNull();
  });
});
