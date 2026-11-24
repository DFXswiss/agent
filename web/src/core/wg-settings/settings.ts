// Workgroup settings (06-web.md section 8.1, 03-api.md sections 9 and 10, ADR-015, ADR-054): the optional
// repository allow-list (empty = every repository of the GitHub connection; `owner/name`, the regex of section 9,
// stored lower case), the protected branch patterns (no default since ADR-078: GitHub's own branch protection
// applies) and the PR draft default (true). They are a `settings.workgroup` event of
// the workgroup scope, written by users with `wg:write`; the newest verified one wins (by `scope_seq`).
//
// - `parseSettings`: the form input (one repository and one pattern per line) → the event body, or the errors per
//   line. Repositories must match the section 9 regex (`repo_invalid`), patterns the contract's character set
//   (`pattern_invalid`); duplicates (after lower-casing repositories) are dropped; at most 500 repositories and 100
//   patterns (`too_many`).
// - `currentSettings`: the newest verified `settings.workgroup` of the workgroup in the store, its `scope_seq` and the
//   body (a body that does not fit the contract is reported, the defaults are shown instead).
// - `saveSettings`: seals the body with the workgroup's current key (`lh` = verified head) and sends it with
//   `POST /v1/events`. `base` is the `scope_seq` the form started from: when a newer event arrived meanwhile the save
//   is refused with `settings_conflict` unless `overwrite` is set (the concurrent-edit warning of section 8.1). The
//   own event is read back through the reader at the position the hub gave it.
import { bodyJson } from '../encoding/json';
import { uuid7 } from '../encoding/ids';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import { epochKey, WG } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { orgEntry } from '../store/slices/orgs';
import { memoize } from '../store/selectors';
import type { ScopeEvents } from '../store/slices/events';
import type { StoreState } from '../store/store';
import type { TasksContext } from '../tasks/pull';

export const KIND = 'settings.workgroup';
/** No default (ADR-078 section 3, A17): GitHub's own branch protection applies; a workgroup may set its own list. */
export const DEFAULT_PROTECTED: readonly string[] = [];
export const MAX_REPOSITORIES = 500;
export const MAX_PATTERNS = 100;
/** `owner/name` of 03-api.md section 9 (any case; stored lower case). */
export const REPO_RE = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/;
/** Branch names or glob patterns with `*` (`contracts/events/objects.json` `body_settings_workgroup`). */
export const PATTERN_RE = /^[A-Za-z0-9._/*+-]{1,200}$/;

export interface WorkgroupSettings {
  readonly repositories: readonly string[];
  readonly protected_branches: readonly string[];
  readonly pr_draft_default: boolean;
}

export const DEFAULTS: WorkgroupSettings = {
  repositories: [],
  protected_branches: [...DEFAULT_PROTECTED],
  pr_draft_default: true,
};

export interface SettingsInput {
  readonly repositories: string;
  readonly protectedBranches: string;
  readonly prDraftDefault: boolean;
}

export interface FieldError {
  readonly field: 'repositories' | 'protected_branches';
  /** 1-based line of the input, 0 for the whole field. */
  readonly line: number;
  readonly code: 'repo_invalid' | 'pattern_invalid' | 'too_many';
  readonly value: string;
}

export type Parsed =
  | { readonly ok: true; readonly settings: WorkgroupSettings }
  | { readonly ok: false; readonly errors: readonly FieldError[] };

/** Non-empty trimmed lines with their 1-based line numbers. */
function lines(text: string): [number, string][] {
  return text
    .split(/\r?\n/)
    .map((line, index): [number, string] => [index + 1, line.trim()])
    .filter(([, line]) => line !== '');
}

function parseList(
  text: string,
  field: FieldError['field'],
  re: RegExp,
  code: FieldError['code'],
  max: number,
  normalize: (value: string) => string,
  errors: FieldError[],
): string[] {
  const values: string[] = [];
  for (const [line, value] of lines(text)) {
    if (!re.test(value)) errors.push({ field, line, code, value });
    else if (!values.includes(normalize(value))) values.push(normalize(value));
  }
  if (values.length > max) errors.push({ field, line: 0, code: 'too_many', value: String(values.length) });
  return values;
}

/** The form input as an event body, or its errors (module comment). */
export function parseSettings(input: SettingsInput): Parsed {
  const errors: FieldError[] = [];
  const repositories = parseList(
    input.repositories,
    'repositories',
    REPO_RE,
    'repo_invalid',
    MAX_REPOSITORIES,
    (value) => value.toLowerCase(),
    errors,
  );
  const patterns = parseList(
    input.protectedBranches,
    'protected_branches',
    PATTERN_RE,
    'pattern_invalid',
    MAX_PATTERNS,
    (value) => value,
    errors,
  );
  if (errors.length > 0) return { ok: false, errors };
  return { ok: true, settings: { repositories, protected_branches: patterns, pr_draft_default: input.prDraftDefault } };
}

/** The form input for stored settings. */
export function toInput(settings: WorkgroupSettings): SettingsInput {
  return {
    repositories: settings.repositories.join('\n'),
    protectedBranches: settings.protected_branches.join('\n'),
    prDraftDefault: settings.pr_draft_default,
  };
}

const LOWER_REPO_RE = /^[a-z0-9-]{1,39}\/[a-z0-9._-]{1,100}$/;

/** A body exactly as `contracts/events/settings.workgroup.json` allows it, else null. */
export function checkBody(body: unknown): WorkgroupSettings | null {
  if (!isRecord(body)) return null;
  const keys = Object.keys(body).sort();
  if (keys.join(',') !== 'pr_draft_default,protected_branches,repositories') return null;
  const { repositories, protected_branches: patterns, pr_draft_default: draft } = body;
  const list = (value: unknown, re: RegExp, max: number): value is string[] =>
    Array.isArray(value) && value.length <= max && value.every((item) => typeof item === 'string' && re.test(item));
  if (!list(repositories, LOWER_REPO_RE, MAX_REPOSITORIES) || !list(patterns, PATTERN_RE, MAX_PATTERNS)) return null;
  if (typeof draft !== 'boolean') return null;
  return { repositories, protected_branches: patterns, pr_draft_default: draft };
}

export type Current =
  | { readonly status: 'none' }
  | { readonly status: 'set'; readonly seq: number; readonly settings: WorkgroupSettings; readonly author: string }
  | { readonly status: 'invalid'; readonly seq: number };

const NONE: Current = { status: 'none' };

const currentOf = memoize((scope: ScopeEvents): Current => {
  let newest: Current = NONE;
  for (const entry of scope.entries.values()) {
    if (entry.status !== 'verified' || entry.header!.kind !== KIND) continue;
    if (newest.status !== 'none' && newest.seq > entry.scopeSeq) continue;
    const settings = checkBody(entry.body);
    newest =
      settings === null
        ? { status: 'invalid', seq: entry.scopeSeq }
        : { status: 'set', seq: entry.scopeSeq, settings, author: entry.header!.author as string };
  }
  return newest;
});

/** The newest verified `settings.workgroup` event of `wg` in the store (module comment; stable per scope state). */
export const selectCurrent =
  (wg: string) =>
  (state: StoreState): Current => {
    const scope = state.events[wg];
    return scope === undefined ? NONE : currentOf(scope);
  };

/** Settings of a `Current` (the defaults when none or invalid). */
export const settingsOf = (current: Current): WorkgroupSettings =>
  current.status === 'set' ? current.settings : DEFAULTS;

/** The `scope_seq` of a `Current` (0 when none). */
export const seqOf = (current: Current): number => (current.status === 'none' ? 0 : current.seq);

/** A refused save with a catalog code. */
export class SettingsError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = 'SettingsError';
    this.code = code;
  }
}

function refuse(code: string): never {
  throw new SettingsError(code);
}

/** Seals and sends the settings (module comment); returns the event's `scope_seq`. */
export async function saveSettings(
  context: TasksContext,
  input: {
    readonly org: string;
    readonly wg: string;
    readonly settings: WorkgroupSettings;
    readonly base: number;
    readonly overwrite?: boolean;
  },
): Promise<number> {
  const { store } = context;
  const { userId, identity } = store.getState().me;
  const entry = orgEntry(store.getState().orgs, input.org);
  if (userId === null || identity === null) refuse('unauthorized');
  if (entry.blocked !== null) refuse(entry.blocked.code);
  const log = entry.log ?? refuse('log_empty');
  if (!log.state.canWriteWg(userId, input.wg)) refuse('forbidden');
  if (checkBody(input.settings) === null) refuse('params_invalid');
  if (!input.overwrite && seqOf(selectCurrent(input.wg)(store.getState())) > input.base) refuse('settings_conflict');
  const epoch = log.state.epoch(WG, input.wg);
  const key = entry.keys.get(epochKey(WG, input.wg, epoch)) ?? refuse('epoch_stale');
  const now = context.now();
  const header = newHeader({
    id: uuid7(now),
    org: input.org,
    scope: WG,
    scope_id: input.wg,
    epoch,
    author: userId,
    kind: KIND,
    ts: formatTime(now),
    lh: log.head.seq,
    wg: input.wg,
  });
  const event = await sealEvent(key, header, bodyJson(input.settings), identity.sig.privateKey);
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : undefined;
  if (!isRecord(result) || result.id !== header.id || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(input.org, input.wg, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
  return result.scope_seq as number;
}
