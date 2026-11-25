// The bodies of the device request `github.pull_status` (KOR-48, ADR-084 section 2,
// `contracts/requests/github.pull_status.*.json`): the request names the organization, the workgroup, optionally the
// task and 1-50 distinct pull requests; the one final answer lists a `pull_state` per pull request (found with GitHub's
// fields, or not found with a code) or fails with a code of `requests/bodies.ts` (`github_not_connected`, `forbidden`
// among them). Anything else is refused like a forged response. The checks register themselves with
// `requests/bodies.ts` when this module loads (with the pull-request code, after the first load, 06-web.md section
// 12.2); `status.ts` imports it before it sends a request.
import { isUuid7 } from '../encoding/ids';
import { isRecord } from '../registry';
import { isInt, keysAre, matches, optionalUuid, registerKind } from '../requests/bodies';

/** Pull requests of one `github.pull_status` request at most. */
export const PULLS_MAX = 50;
/** `owner/name` as GitHub spells it (03-api.md section 9). */
export const PULL_REPO_RE = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/;
/** Codes of a pull request the device could not read (`pull_state` with `found: false`). */
export const PULL_CODES = ['not_found', 'repo_not_accessible', 'repo_not_allowed', 'upstream_error'] as const;
export type PullCode = (typeof PULL_CODES)[number];
const SHA_RE = /^[0-9a-f]{40}$/;
/** Characters a branch name never holds besides control characters (`contracts/ops/objects.json` `branch`). */
const BRANCH_BAD_RE = /[ ~^:?*[\\]/;
/** The address of a pull request (`contracts/ops/objects.json` `url`): https only. */
const PULL_URL_RE = /^https:\/\/[!-~]{1,2048}$/;
/** `updated_at` as the contract writes it (UTC, optional fraction). */
const UPDATED_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$/;
/** The largest number of a pull request the contracts allow. */
const NUMBER_MAX = 2 ** 31 - 1;
/** GitHub's `mergeable_state` (display only): a lower-case word. */
const MERGEABLE_STATE_RE = /^[a-z_]{1,32}$/;

/** A pull request a `github.pull_status` request names. */
export interface PullTarget {
  readonly repo: string;
  readonly number: number;
}

/** The state of one pull request in the answer of `github.pull_status`. */
export type PullState =
  | {
      readonly repo: string;
      readonly number: number;
      readonly found: true;
      readonly title: string;
      readonly state: 'open' | 'closed';
      readonly draft: boolean;
      readonly merged: boolean;
      /** GitHub computes it asynchronously: null while unknown. */
      readonly mergeable: boolean | null;
      readonly mergeable_state: string;
      readonly head_ref: string;
      readonly head_sha: string;
      /** The repository of the head branch (another one for a fork), null when GitHub names none (deleted fork). */
      readonly head_repo: string | null;
      readonly base_ref: string;
      readonly url: string;
      readonly updated_at: string;
    }
  | { readonly repo: string; readonly number: number; readonly found: false; readonly code: PullCode };

const FOUND_KEYS = [
  'repo',
  'number',
  'found',
  'title',
  'state',
  'draft',
  'merged',
  'mergeable',
  'mergeable_state',
  'head_ref',
  'head_sha',
  'head_repo',
  'base_ref',
  'url',
  'updated_at',
] as const;

/** A branch name as `contracts/ops/objects.json` `branch` allows it: 1-200 characters, no control character. */
const isBranch = (value: unknown): value is string =>
  typeof value === 'string' &&
  value.length >= 1 &&
  value.length <= 200 &&
  !/^[/.-]/.test(value) &&
  !BRANCH_BAD_RE.test(value) &&
  [...value].every((char) => char.charCodeAt(0) > 0x1f && char.charCodeAt(0) !== 0x7f);

const isPullNumber = (value: unknown): value is number => isInt(value, 1, NUMBER_MAX);

const isPullTarget = (value: unknown): value is PullTarget =>
  isRecord(value) &&
  keysAre(value, ['repo', 'number']) &&
  matches(value.repo, PULL_REPO_RE) &&
  isPullNumber(value.number);

/** 1..`PULLS_MAX` pull requests, each named once (the repository compared in lower case). */
function isPullList(value: unknown): value is PullTarget[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > PULLS_MAX || !value.every(isPullTarget)) return false;
  return new Set(value.map((item) => `${item.repo.toLowerCase()}#${item.number}`)).size === value.length;
}

/** One `pull_state` exactly as the contract allows it. */
export function isPullState(value: unknown): value is PullState {
  if (!isRecord(value) || !matches(value.repo, PULL_REPO_RE) || !isPullNumber(value.number)) return false;
  if (value.found === false)
    return (
      keysAre(value, ['repo', 'number', 'found', 'code']) && (PULL_CODES as readonly unknown[]).includes(value.code)
    );
  return (
    value.found === true &&
    keysAre(value, FOUND_KEYS) &&
    typeof value.title === 'string' &&
    value.title.length <= 1024 &&
    (value.state === 'open' || value.state === 'closed') &&
    typeof value.draft === 'boolean' &&
    typeof value.merged === 'boolean' &&
    (value.mergeable === null || typeof value.mergeable === 'boolean') &&
    matches(value.mergeable_state, MERGEABLE_STATE_RE) &&
    isBranch(value.head_ref) &&
    matches(value.head_sha, SHA_RE) &&
    (value.head_repo === null || matches(value.head_repo, PULL_REPO_RE)) &&
    isBranch(value.base_ref) &&
    matches(value.url, PULL_URL_RE) &&
    matches(value.updated_at, UPDATED_RE)
  );
}

/** A request body of the contract (never a follow-up). */
export const validPullRequest = (body: Record<string, unknown>, followUp: boolean): boolean =>
  !followUp &&
  keysAre(body, ['org_id', 'wg_id', 'pulls'], ['task_id']) &&
  isUuid7(body.org_id) &&
  isUuid7(body.wg_id) &&
  optionalUuid(body.task_id) &&
  isPullList(body.pulls);

/** The `done` answer of the contract (a `failed` one is checked by `requests/bodies.ts`). */
export const validPullResponse = (body: Record<string, unknown>): boolean =>
  body.step === 'done' &&
  keysAre(body, ['step', 'pulls']) &&
  Array.isArray(body.pulls) &&
  body.pulls.length >= 1 &&
  body.pulls.length <= PULLS_MAX &&
  body.pulls.every(isPullState);

registerKind('github.pull_status', { request: validPullRequest, response: validPullResponse });
