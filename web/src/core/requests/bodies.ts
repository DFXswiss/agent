// The plaintext bodies of device requests and their responses (04-crypto.md section 4.5, `contracts/requests/`): the
// four request kinds, what a user may send for each, and the closed set of response steps a device may answer. A body
// that does not match is refused like a forged response (`params_invalid`): the browser renders only checked bodies.
//
// `github.connect` may carry an existing token (KOR-25, ADR-059); its `done` then names the scopes and the source.
//
// Used by WEB-09 (`github.connect`) and WEB-10 (`claude.login`, `credential.check`, `mailbox.check`).
import { isRecord } from '../registry';

export const REQUEST_KINDS = ['github.connect', 'claude.login', 'credential.check', 'mailbox.check'] as const;
export type RequestKind = (typeof REQUEST_KINDS)[number];

/** Failure codes of device responses (`contracts/requests/objects.json` `failure_code`). */
export const FAILURE_CODES = [
  'timeout',
  'busy',
  'credential_missing',
  'code_invalid',
  'login_cli_failed',
  'token_invalid',
  'upstream_error',
  'upstream_unreachable',
  'device_shutdown',
  'token_refused',
  'scope_missing',
  'sso_required',
] as const;
export type FailureCode = (typeof FAILURE_CODES)[number];
/** Client code of a request the hub failed (device no longer eligible, or replaced by a newer one of its kind). */
export const REQUEST_FAILED = 'request_failed';

export const PROGRESS_STAGES = ['browser_started', 'email_entered', 'magic_link_received', 'authorized'] as const;

/** https URLs, or http on the loopback host (hermetic doubles); at most 2048 characters. */
export const URL_RE = /^(https:\/\/[!-~]{1,2040}|http:\/\/(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(\/[!-~]{0,2000})?)$/;
export const USER_CODE_RE = /^[A-Z0-9-]{4,16}$/;
export const LOGIN_RE = /^[A-Za-z0-9-]{1,39}$/;
export const EMAIL_RE = /^[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}$/;
export const CODE_RE = /^[!-~]+$/;
/** An existing GitHub token (`contracts/requests/github.connect.request.json`): printable ASCII without spaces. */
export const TOKEN_RE = /^[!-~]{1,4096}$/;
/** A scope of a classic token (`X-OAuth-Scopes`). */
export const SCOPE_RE = /^[a-z0-9:_-]{1,64}$/;
export const SOURCES = ['device_flow', 'token'] as const;
export type Source = (typeof SOURCES)[number];

export type ResponseBody =
  | {
      readonly step: 'device_code';
      readonly user_code: string;
      readonly verification_uri: string;
      readonly expires_in: number;
    }
  | { readonly step: 'authorize'; readonly url: string; readonly automation?: 'failed' }
  | { readonly step: 'progress'; readonly stage: (typeof PROGRESS_STAGES)[number] }
  | {
      readonly step: 'done';
      readonly login?: string;
      readonly email?: string;
      readonly valid?: boolean;
      readonly scopes?: readonly string[];
      readonly source?: Source;
    }
  | { readonly step: 'failed'; readonly code: FailureCode };

export const isRequestKind = (value: unknown): value is RequestKind =>
  (REQUEST_KINDS as readonly unknown[]).includes(value);

/** Exactly the keys `required` plus any of `optional`. */
function keysAre(value: Record<string, unknown>, required: readonly string[], optional: readonly string[] = []) {
  const keys = Object.keys(value);
  return (
    required.every((key) => keys.includes(key)) && keys.every((key) => required.includes(key) || optional.includes(key))
  );
}

const matches = (value: unknown, re: RegExp, max = Infinity): value is string =>
  typeof value === 'string' && value.length <= max && re.test(value);

const isInt = (value: unknown, min: number, max: number): value is number =>
  Number.isSafeInteger(value) && (value as number) >= min && (value as number) <= max;

/** At most 64 scopes of the contract's form. */
export const isScopes = (value: unknown): value is string[] =>
  Array.isArray(value) && value.length <= 64 && value.every((scope) => matches(scope, SCOPE_RE));

/** A request body a user may send for `kind` (first request or, with `follows`, a follow-up). */
export function validRequestBody(kind: RequestKind, body: unknown, followUp: boolean): boolean {
  if (!isRecord(body)) return false;
  switch (kind) {
    case 'github.connect':
      // without `token` the device flow runs; with it the device checks and stores an existing token (KOR-25)
      return !followUp && keysAre(body, [], ['token']) && (body.token === undefined || matches(body.token, TOKEN_RE));
    case 'mailbox.check':
      return !followUp && keysAre(body, []);
    case 'credential.check':
      return (
        !followUp &&
        keysAre(body, ['kind']) &&
        (body.kind === 'anthropic_api_key' || body.kind === 'claude_subscription')
      );
    case 'claude.login':
      if (followUp) return keysAre(body, ['code']) && matches(body.code, CODE_RE, 512);
      return keysAre(body, ['mode']) && (body.mode === 'manual' || body.mode === 'automatic');
  }
}

function failed(body: Record<string, unknown>): boolean {
  return keysAre(body, ['step', 'code']) && (FAILURE_CODES as readonly unknown[]).includes(body.code);
}

/** The response body of a device for `kind`, or null when it is not one the contracts allow. */
export function checkResponseBody(kind: RequestKind, body: unknown): ResponseBody | null {
  if (!isRecord(body)) return null;
  const ok = ((): boolean => {
    if (body.step === 'failed') return failed(body);
    switch (kind) {
      case 'github.connect':
        if (body.step === 'device_code')
          return (
            keysAre(body, ['step', 'user_code', 'verification_uri', 'expires_in']) &&
            matches(body.user_code, USER_CODE_RE) &&
            matches(body.verification_uri, URL_RE, 2048) &&
            isInt(body.expires_in, 1, 3600)
          );
        return (
          body.step === 'done' &&
          keysAre(body, ['step', 'login'], ['scopes', 'source']) &&
          matches(body.login, LOGIN_RE) &&
          (body.scopes === undefined || isScopes(body.scopes)) &&
          (body.source === undefined || (SOURCES as readonly unknown[]).includes(body.source))
        );
      case 'claude.login':
        if (body.step === 'authorize')
          return (
            keysAre(body, ['step', 'url'], ['automation']) &&
            matches(body.url, URL_RE, 2048) &&
            (body.automation === undefined || body.automation === 'failed')
          );
        if (body.step === 'progress')
          return keysAre(body, ['step', 'stage']) && (PROGRESS_STAGES as readonly unknown[]).includes(body.stage);
        return (
          body.step === 'done' &&
          keysAre(body, ['step'], ['email']) &&
          (body.email === undefined || matches(body.email, EMAIL_RE, 254))
        );
      case 'credential.check':
      case 'mailbox.check':
        return body.step === 'done' && keysAre(body, ['step', 'valid']) && typeof body.valid === 'boolean';
    }
  })();
  return ok ? (body as ResponseBody) : null;
}
