// The plaintext bodies of device requests and their responses (04-crypto.md section 4.5, `contracts/requests/`): the
// four request kinds, what a user may send for each, and the closed set of response steps a device may answer. A body
// that does not match is refused like a forged response (`params_invalid`): the browser renders only checked bodies.
//
// `github.connect` may carry an existing token (KOR-25, ADR-065); its `done` then names the scopes and the source.
// GitHub identities (KOR-47, ADR-078): the request may name the record to reconnect (`identity_id`), a company account
// of an organization (`org_id`, its `done` then carries the credential `body` for the admin's client to seal) and ask
// for an own signing key (`signing_key`, outcome `signing`); `source` adds `operator`.
//
// `github.pull_status` (KOR-48, ADR-084 section 2): its bodies are checked by `core/pulls/checks.ts`, which registers
// itself here (`registerKind`) when the pull-request code loads – that code is not part of the first load (06-web.md
// section 12.2), and no such request is sent before it registered. Without a registration every body of the kind is
// refused.
//
// Used by WEB-09 (`github.connect`), WEB-10 (`claude.login`, `credential.check`, `mailbox.check`) and KOR-48
// (`github.pull_status`, `core/pulls/status.ts`).
import { isUuid7 } from '../encoding/ids';
import type { PullState } from '../pulls/checks';
import { isRecord } from '../registry';

export const REQUEST_KINDS = [
  'github.connect',
  'claude.login',
  'credential.check',
  'mailbox.check',
  'github.pull_status',
] as const;
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
  'flow_unavailable',
  'github_not_connected',
  'forbidden',
] as const;
export type FailureCode = (typeof FAILURE_CODES)[number];
/** Client code of a request the hub failed (device no longer eligible, or replaced by a newer one of its kind). */
export const REQUEST_FAILED = 'request_failed';

export const PROGRESS_STAGES = [
  'browser_started',
  'email_entered',
  'magic_link_received',
  'authorized',
  'mail_code_received',
  'registered',
  'verified',
] as const;
/** Why the mailbox automation gave up (`reason` of `authorize`, KOR-73, ADR-099 section 4). */
export const AUTOMATION_REASONS = [
  'disabled',
  'no_sandbox',
  'human_check',
  'consent_missing',
  'account_missing',
  'unknown_page',
  'mail_timeout',
  'browser_failed',
] as const;
export type AutomationReason = (typeof AUTOMATION_REASONS)[number];
/** The consents an owner can give for a new account (ADR-099 section 5); only these boxes are ever ticked. */
export const CONSENTS = ['terms', 'age', 'marketing'] as const;
export type Consent = (typeof CONSENTS)[number];
/** A date of birth `YYYY-MM-DD`. */
export const BIRTH_DATE_RE = /^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$/;
/** A picture of a check for humans: JPEG in standard base64, at most 100 KiB. */
const JPEG_B64_RE = /^[A-Za-z0-9+/]+={0,2}$/;
const MAX_JPEG_B64 = 136536;

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
export const SOURCES = ['device_flow', 'token', 'operator'] as const;
/** The device flows `github.connect` may name (`app`, KOR-65). */
export const APPS = ['oauth_app', 'github_app'] as const;
export type App = (typeof APPS)[number];
export type Source = (typeof SOURCES)[number];
/** Outcomes of `signing_key` in the final answer of `github.connect` (KOR-47). */
export const SIGNING_OUTCOMES = ['registered', 'scope_missing', 'refused'] as const;
export type SigningOutcome = (typeof SIGNING_OUTCOMES)[number];
/** The display name of a GitHub account (`contracts/credentials/github.json`): printable ASCII without `<` and `>`. */
export const NAME_RE = /^[ -;=?-~]{1,255}$/;
/** b64u of exactly 32 bytes. */
const B64U_32_RE = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;
/** The OpenSSH form of an Ed25519 public key with an optional comment. */
const SSH_PUBLIC_RE = /^ssh-ed25519 [A-Za-z0-9+/]{68}( [!-~]{1,64})?$/;

/** The identity's own signing key (`signing_key` of `contracts/credentials/github.json`). */
export interface SigningKey {
  readonly alg: 'ssh-ed25519';
  readonly seed: string;
  readonly public: string;
  readonly github_key_id?: number;
}

/** The plaintext body of a `github` credential record (`contracts/credentials/github.json`). */
export interface GithubBody {
  readonly token: string;
  readonly login: string;
  readonly user_id: number;
  readonly scopes: string[];
  readonly source?: Source;
  readonly created_at: string;
  readonly name?: string;
  readonly signing_key?: SigningKey;
}

/** The sandbox's picture of a check for humans (`verify`, KOR-73). */
export interface VerifyImage {
  readonly width: number;
  readonly height: number;
  /** JPEG, standard base64. */
  readonly data: string;
}

export type ResponseBody =
  | {
      readonly step: 'device_code';
      readonly user_code: string;
      readonly verification_uri: string;
      readonly expires_in: number;
    }
  | {
      readonly step: 'authorize';
      readonly url: string;
      readonly automation?: 'failed';
      readonly reason?: AutomationReason;
    }
  | { readonly step: 'progress'; readonly stage: (typeof PROGRESS_STAGES)[number] }
  | { readonly step: 'verify'; readonly frame: number; readonly image: VerifyImage }
  | {
      readonly step: 'done';
      readonly login?: string;
      readonly email?: string;
      readonly valid?: boolean;
      readonly scopes?: readonly string[];
      readonly source?: Source;
      readonly identity_id?: string;
      readonly signing?: SigningOutcome;
      readonly body?: GithubBody;
      /** `github.pull_status` (`core/pulls/checks.ts`). */
      readonly pulls?: readonly PullState[];
    }
  | { readonly step: 'failed'; readonly code: FailureCode };

export const isRequestKind = (value: unknown): value is RequestKind =>
  (REQUEST_KINDS as readonly unknown[]).includes(value);

/** Exactly the keys `required` plus any of `optional`. */
export function keysAre(value: Record<string, unknown>, required: readonly string[], optional: readonly string[] = []) {
  const keys = Object.keys(value);
  return (
    required.every((key) => keys.includes(key)) && keys.every((key) => required.includes(key) || optional.includes(key))
  );
}

export const matches = (value: unknown, re: RegExp, max = Infinity): value is string =>
  typeof value === 'string' && value.length <= max && re.test(value);

export const isInt = (value: unknown, min: number, max: number): value is number =>
  Number.isSafeInteger(value) && (value as number) >= min && (value as number) <= max;

/** At most 64 scopes of the contract's form. */
export const isScopes = (value: unknown): value is string[] =>
  Array.isArray(value) && value.length <= 64 && value.every((scope) => matches(scope, SCOPE_RE));

const isPositive = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 1;

const isSigningKey = (value: unknown): value is SigningKey =>
  isRecord(value) &&
  keysAre(value, ['alg', 'seed', 'public'], ['github_key_id']) &&
  value.alg === 'ssh-ed25519' &&
  matches(value.seed, B64U_32_RE) &&
  matches(value.public, SSH_PUBLIC_RE) &&
  (value.github_key_id === undefined || isPositive(value.github_key_id));

/** The body exactly as `contracts/credentials/github.json` allows it (`source`, `name`, `signing_key` optional). */
export const isGithubBody = (value: unknown): value is GithubBody =>
  isRecord(value) &&
  keysAre(value, ['token', 'login', 'user_id', 'scopes', 'created_at'], ['source', 'name', 'signing_key']) &&
  matches(value.token, TOKEN_RE) &&
  matches(value.login, LOGIN_RE) &&
  isPositive(value.user_id) &&
  isScopes(value.scopes) &&
  (value.source === undefined || (SOURCES as readonly unknown[]).includes(value.source)) &&
  typeof value.created_at === 'string' &&
  (value.name === undefined || matches(value.name, NAME_RE)) &&
  (value.signing_key === undefined || isSigningKey(value.signing_key));

export const optionalUuid = (value: unknown): boolean => value === undefined || isUuid7(value);

/** The checks of a kind whose code registers itself (module comment). */
export interface KindChecks {
  readonly request: (body: Record<string, unknown>, followUp: boolean) => boolean;
  readonly response: (body: Record<string, unknown>) => boolean;
}

const registered = new Map<RequestKind, KindChecks>();

/** Registers the checks of `kind` (module comment). */
export function registerKind(kind: RequestKind, checks: KindChecks): void {
  registered.set(kind, checks);
}

/** A request body a user may send for `kind` (first request or, with `follows`, a follow-up). */
export function validRequestBody(kind: RequestKind, body: unknown, followUp: boolean): boolean {
  if (!isRecord(body)) return false;
  switch (kind) {
    case 'github.connect':
      // without `token` the device flow runs (of the app `app` names, KOR-65); with it the device checks and stores an
      // existing token (KOR-25), never both; KOR-47 adds the identity, the organization and the signing key
      return (
        !followUp &&
        keysAre(body, [], ['token', 'app', 'org_id', 'signing_key', 'identity_id']) &&
        (body.token === undefined || matches(body.token, TOKEN_RE)) &&
        (body.app === undefined || (body.token === undefined && (APPS as readonly unknown[]).includes(body.app))) &&
        optionalUuid(body.org_id) &&
        optionalUuid(body.identity_id) &&
        (body.signing_key === undefined || typeof body.signing_key === 'boolean')
      );
    case 'mailbox.check':
      // KOR-73: the own mailbox of a further subscription
      return !followUp && keysAre(body, [], ['subscription_id']) && optionalUuid(body.subscription_id);
    case 'github.pull_status':
      return registered.get(kind)?.request(body, followUp) ?? false;
    case 'credential.check':
      return (
        !followUp &&
        keysAre(body, ['kind']) &&
        (body.kind === 'anthropic_api_key' || body.kind === 'claude_subscription')
      );
    case 'claude.login':
      if (followUp) return isLoginFollowUp(body);
      // KOR-69 (ADR-090 section 2): a further subscription, its label and note and, automatic only, the address;
      // KOR-73 (ADR-099 section 5): automatic only, the data of a new account
      return (
        keysAre(body, ['mode'], ['subscription_id', 'name', 'note', 'email', 'account', 'extended']) &&
        (body.extended === undefined || body.extended === true) &&
        (body.mode === 'manual' || body.mode === 'automatic') &&
        optionalUuid(body.subscription_id) &&
        (body.name === undefined || isLabel(body.name, 64)) &&
        (body.note === undefined || isLabel(body.note, 500)) &&
        (body.email === undefined || (body.mode === 'automatic' && matches(body.email, EMAIL_RE, 254))) &&
        (body.account === undefined || (body.mode === 'automatic' && isAccount(body.account)))
      );
  }
}

/** A fraction of a picture's width or height. */
const isFraction = (value: unknown): boolean => typeof value === 'number' && value >= 0 && value <= 1;

/** A follow-up of `claude.login`: the code, a click into the picture of a check for humans, or giving up (KOR-73). */
function isLoginFollowUp(body: Record<string, unknown>): boolean {
  if ('click' in body)
    return (
      keysAre(body, ['click']) &&
      isRecord(body.click) &&
      keysAre(body.click, ['x', 'y']) &&
      isFraction(body.click.x) &&
      isFraction(body.click.y)
    );
  if ('relay' in body) return keysAre(body, ['relay']) && body.relay === true;
  return keysAre(body, ['code']) && matches(body.code, CODE_RE, 512);
}

/** The data of a new account (`account` of `contracts/requests/claude.login.request.json`). */
export function isAccount(value: unknown): boolean {
  if (!isRecord(value) || !keysAre(value, ['name'], ['birth_date', 'consents'])) return false;
  const consents = value.consents;
  return (
    isLabel(value.name, 100) &&
    (value.birth_date === undefined || matches(value.birth_date, BIRTH_DATE_RE)) &&
    (consents === undefined ||
      (Array.isArray(consents) &&
        consents.every((item) => (CONSENTS as readonly unknown[]).includes(item)) &&
        new Set(consents).size === consents.length))
  );
}

/** A subscription's name or note: 1 to `most` characters without a control character (ADR-090 section 1). */
export function isLabel(value: unknown, most: number): value is string {
  // eslint-disable-next-line no-control-regex
  return typeof value === 'string' && value.length > 0 && value.length <= most && !/[\u0000-\u001f]/.test(value);
}

function failed(body: Record<string, unknown>): boolean {
  return keysAre(body, ['step', 'code']) && (FAILURE_CODES as readonly unknown[]).includes(body.code);
}

/** `verify {frame, image}` of `claude.login` (KOR-73). */
function isVerify(body: Record<string, unknown>): boolean {
  const image = body.image;
  return (
    keysAre(body, ['step', 'frame', 'image']) &&
    isInt(body.frame, 1, 100) &&
    isRecord(image) &&
    keysAre(image, ['width', 'height', 'data']) &&
    isInt(image.width, 1, 4096) &&
    isInt(image.height, 1, 4096) &&
    matches(image.data, JPEG_B64_RE, MAX_JPEG_B64) &&
    (image.data as string).length >= 4
  );
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
          keysAre(body, ['step', 'login'], ['scopes', 'source', 'identity_id', 'signing', 'body']) &&
          matches(body.login, LOGIN_RE) &&
          (body.scopes === undefined || isScopes(body.scopes)) &&
          (body.source === undefined || (SOURCES as readonly unknown[]).includes(body.source)) &&
          optionalUuid(body.identity_id) &&
          (body.signing === undefined || (SIGNING_OUTCOMES as readonly unknown[]).includes(body.signing)) &&
          (body.body === undefined || isGithubBody(body.body))
        );
      case 'claude.login':
        if (body.step === 'authorize')
          return (
            keysAre(body, ['step', 'url'], ['automation', 'reason']) &&
            matches(body.url, URL_RE, 2048) &&
            (body.automation === undefined || body.automation === 'failed') &&
            (body.reason === undefined || (AUTOMATION_REASONS as readonly unknown[]).includes(body.reason))
          );
        if (body.step === 'verify') return isVerify(body);
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
      case 'github.pull_status':
        return registered.get(kind)?.response(body) ?? false;
    }
  })();
  return ok ? (body as ResponseBody) : null;
}
