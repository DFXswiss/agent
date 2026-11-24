// What the operation card and the Operations tab show (06-web.md sections 5.2 and 5.4, 03-api.md sections 9 and 10,
// ADR-014): the hub's row (type, live state, error code, `duplicate_of`) joined with the decrypted `operation.params`
// and `operation.result` events of the operation – events of the workgroup scope whose `ref` is the operation.
//
// Events count only when the reader verified them and their body names the same operation (`operation_id`); an event
// that fails is reported with its code, never as content. Without a row (not loaded yet) the state comes from the
// verified result (`status`), else `received`. The subject of `human.question` / `human.approval` is the question
// (KOR-39). Result facts are the links and values of 03-api.md section 9: the PR
// (number, URL), the commit SHA, the pushed branch (SHA, URL), the comment URL, the created branch, the checkout
// commit, the subagent session, the new task title (`task.set_title`: "renamed by session"), the number of listed
// repositories, issues and pull requests, the issue (number, URL) of `github.get_issue`, `github.create_issue` and
// `github.update_issue` with the new state, and the connected login of `github.request_connection`. The lists of
// `github.list_issues` / `github.list_pull_requests` (`listing`) and the issue of `github.get_issue` with one page of
// comments (`issue`) are read entry by entry: an entry without a number or title is left out. Text from GitHub is
// untrusted and only ever rendered as text; URLs pass `httpsUrl` (`safeUrl` and https only, as the contracts allow).
// A `test.run` (ADR-079) has its kind and the start of its command as subject, run ID and exit code as facts, and
// its checked result (`testRun`: state, summary, log) for the card's "Tests: 3 failed – open log".
import { safeUrl } from '../chat/links';
import { parseTestRunResult, TEST_RUN_TYPE, type TestRunResult } from '../chat/testrun';
import { isRecord } from '../registry';
import type { EventEntry } from '../store/slices/events';
import type { OperationRow } from '../store/slices/operations';
import type { StoreState } from '../store/store';
import { OPERATION_KINDS } from './load';

export type Part<T> =
  | { readonly status: 'verified'; readonly value: T; readonly at: string }
  | { readonly status: 'unverifiable'; readonly code: string }
  | { readonly status: 'missing' };

export interface ParamsBody {
  readonly type: string;
  readonly params: Readonly<Record<string, unknown>>;
}

export interface ResultBody {
  readonly status: string;
  readonly result: Readonly<Record<string, unknown>> | null;
  readonly error: { readonly code: string; readonly message: string | null; readonly retryable: boolean | null } | null;
}

export interface Fact {
  /** Message key suffix (`operations.fact.<label>`). */
  readonly label: string;
  readonly text: string;
  /** External link (https). */
  readonly href?: string;
  /** Internal link to a session (`/o/:org/s/:session`). */
  readonly session?: string;
}

/** One entry of an issue or pull request list. */
export interface ListEntry {
  readonly number: number;
  readonly title: string;
  /** `open` or `closed`; null when the result carries another value. */
  readonly state: string | null;
  readonly draft: boolean;
  readonly author: string | null;
  readonly labels: readonly string[];
  readonly href?: string;
}

/** The list of `github.list_issues` (`issues`) or `github.list_pull_requests` (`pull_requests`). */
export interface Listing {
  readonly kind: 'issues' | 'pull_requests';
  readonly entries: readonly ListEntry[];
  readonly nextPage: number | null;
  readonly truncated: boolean;
}

export interface IssueComment {
  readonly id: string;
  readonly author: string | null;
  readonly body: string;
  readonly createdAt: string | null;
}

/** The issue of `github.get_issue` with one page of its comments. */
export interface IssueContent extends ListEntry {
  readonly stateReason: string | null;
  readonly body: string | null;
  readonly pullRequest: boolean;
  readonly comments: readonly IssueComment[];
  readonly commentsTotal: number | null;
  readonly commentsNextPage: number | null;
  readonly truncated: boolean;
}

export interface OperationView {
  readonly id: string;
  readonly type: string;
  readonly state: string;
  readonly errorCode: string | null;
  readonly duplicateOf: string | null;
  readonly sessionId: string | null;
  readonly createdAt: string | null;
  readonly row: OperationRow | null;
  readonly params: Part<ParamsBody>;
  readonly result: Part<ResultBody>;
  /** A short description from the parameters (repository, PR number, branch, purpose, title). */
  readonly subject: string | null;
  readonly facts: readonly Fact[];
  /** The listed issues or pull requests of a succeeded list operation. */
  readonly listing: Listing | null;
  /** The issue of a succeeded `github.get_issue`. */
  readonly issue: IssueContent | null;
  /** The result of a succeeded `test.run` that follows the contract (state, summary, log). */
  readonly testRun: TestRunResult | null;
}

const raw = (entry: EventEntry): Readonly<Record<string, unknown>> =>
  isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};

const text = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];

/** Issue and pull request states and state reasons of the contracts (`operations.value.<value>`). */
export const ISSUE_VALUES: ReadonlySet<string> = new Set([
  'open',
  'closed',
  'completed',
  'not_planned',
  'reopened',
  'duplicate',
]);
const known = (value: unknown): string | null => (typeof value === 'string' && ISSUE_VALUES.has(value) ? value : null);

/** The URL if it is an absolute `https:` URL that `safeUrl` keeps, else null. */
export function httpsUrl(value: unknown): string | null {
  const url = typeof value === 'string' ? safeUrl(value) : null;
  return url !== null && new URL(url).protocol === 'https:' ? url : null;
}

/**
 * The newest verified event of `kind` that refers to `operation`, else the newest unverifiable one (by its unchecked
 * header): an injected unverifiable event never hides a verified one.
 */
function find(entries: Iterable<EventEntry>, operation: string, kind: string): EventEntry | null {
  let verified: EventEntry | null = null;
  let other: EventEntry | null = null;
  for (const entry of entries) {
    if (entry.status === 'pending' || entry.status === 'locked') continue;
    const header = entry.status === 'verified' ? entry.header! : raw(entry);
    if (header.ref !== operation || header.kind !== kind) continue;
    if (entry.status === 'verified')
      verified = verified === null || entry.scopeSeq > verified.scopeSeq ? entry : verified;
    else other = other === null || entry.scopeSeq > other.scopeSeq ? entry : other;
  }
  return verified ?? other;
}

function part<T>(
  entry: EventEntry | null,
  operation: string,
  device: string | null,
  read: (body: Record<string, unknown>) => T | null,
): Part<T> {
  if (entry === null) return { status: 'missing' };
  if (entry.status !== 'verified') return { status: 'unverifiable', code: entry.code ?? 'header_invalid' };
  const header = entry.header!;
  const body = entry.body;
  // the body must name the operation of its header, and the device of the row must be its author
  if (!isRecord(body) || body.operation_id !== operation || (device !== null && header.author !== device))
    return { status: 'unverifiable', code: 'header_invalid' };
  const value = read(body);
  return value === null
    ? { status: 'unverifiable', code: 'params_invalid' }
    : { status: 'verified', value, at: header.ts as string };
}

const readParams = (body: Record<string, unknown>): ParamsBody | null =>
  typeof body.type === 'string' && isRecord(body.params) ? { type: body.type, params: body.params } : null;

function readResult(body: Record<string, unknown>): ResultBody | null {
  if (body.status === 'succeeded')
    return isRecord(body.result) ? { status: 'succeeded', result: body.result, error: null } : null;
  if (typeof body.status !== 'string' || !isRecord(body.error) || typeof body.error.code !== 'string') return null;
  return {
    status: body.status,
    result: null,
    error: {
      code: body.error.code,
      message: text(body.error.message),
      retryable: typeof body.error.retryable === 'boolean' ? body.error.retryable : null,
    },
  };
}

/** At most this many characters of a test command are the subject. */
export const SUBJECT_CHARS = 200;

/** `unit: npm test` of a `test.run`: kind and the start of the command. */
function testSubject(params: Readonly<Record<string, unknown>>): string | null {
  const command = text(params.command);
  if (command === null) return null;
  const short = [...command].length > SUBJECT_CHARS ? `${[...command].slice(0, SUBJECT_CHARS).join('')}…` : command;
  return `${text(params.kind) ?? 'custom'}: ${short}`;
}

/** A short description of the parameters (module comment). */
export function subjectOf(type: string, params: Readonly<Record<string, unknown>>): string | null {
  if (type === 'subagent.request') return text(params.purpose);
  if (type === 'task.set_title') return text(params.title);
  if (type === 'workvm.request') return text(params.size) ?? 'standard';
  if (type === 'workvm.release') return text(params.scope) ?? 'task';
  if (type === TEST_RUN_TYPE) return testSubject(params);
  if (type === 'human.question' || type === 'human.approval') return text(params.question);
  const repo = text(params.repo);
  if (repo === null) return null;
  if (typeof params.number === 'number') return `${repo}#${params.number}`;
  if (type === 'github.create_issue' && typeof params.title === 'string') return `${repo}: ${params.title}`;
  const branch = text(params.branch) ?? text(params.head);
  return branch === null ? repo : `${repo} ${branch}`;
}

function link(label: string, value: unknown, href: unknown): Fact[] {
  const url = httpsUrl(href);
  if (typeof value !== 'string' && typeof value !== 'number') return [];
  return [{ label, text: String(value), ...(url !== null && { href: url }) }];
}

const issueLink = (result: Readonly<Record<string, unknown>>): Fact[] =>
  link('issue', typeof result.number === 'number' ? `#${result.number}` : null, result.url);

/** An entry of a list (or the head of an issue); null without a positive integer number or a title. */
function entryOf(value: unknown): ListEntry | null {
  if (!isRecord(value)) return null;
  const { number, title } = value;
  if (typeof number !== 'number' || !Number.isSafeInteger(number) || number < 1 || typeof title !== 'string')
    return null;
  const href = httpsUrl(value.url);
  return {
    number,
    title,
    state: value.state === 'open' || value.state === 'closed' ? value.state : null,
    draft: value.draft === true,
    author: text(value.author),
    labels: strings(value.labels),
    ...(href !== null && { href }),
  };
}

const LIST_KEYS: Readonly<Record<string, Listing['kind']>> = {
  'github.list_issues': 'issues',
  'github.list_pull_requests': 'pull_requests',
};

/** The listed issues or pull requests of a succeeded result (module comment); null for other types. */
export function listingOf(type: string, result: Readonly<Record<string, unknown>>): Listing | null {
  const kind = Object.hasOwn(LIST_KEYS, type) ? LIST_KEYS[type]! : null;
  if (kind === null) return null;
  const items: unknown = result[kind];
  return {
    kind,
    entries: Array.isArray(items) ? items.map(entryOf).filter((entry) => entry !== null) : [],
    nextPage: count(result.next_page),
    truncated: result.truncated === true,
  };
}

function commentOf(value: unknown): IssueComment | null {
  if (!isRecord(value) || typeof value.body !== 'string') return null;
  const id = typeof value.id === 'number' || typeof value.id === 'string' ? String(value.id) : null;
  if (id === null) return null;
  return { id, author: text(value.author), body: value.body, createdAt: text(value.created_at) };
}

/** The issue of a succeeded `github.get_issue` (module comment); null for other types or without number and title. */
export function issueOf(type: string, result: Readonly<Record<string, unknown>>): IssueContent | null {
  if (type !== 'github.get_issue') return null;
  const head = entryOf(result);
  if (head === null) return null;
  const comments = Array.isArray(result.comments) ? result.comments.map(commentOf).filter((c) => c !== null) : [];
  return {
    ...head,
    stateReason: known(result.state_reason),
    body: text(result.body),
    pullRequest: result.pull_request === true,
    comments,
    commentsTotal: count(result.comments_total),
    commentsNextPage: count(result.comments_next_page),
    truncated: result.truncated === true,
  };
}

/** The facts of a succeeded result (module comment). */
export function factsOf(type: string, result: Readonly<Record<string, unknown>>, params: Part<ParamsBody>): Fact[] {
  switch (type) {
    case 'github.create_pull_request':
    case 'github.get_pull_request':
      return link('pull_request', typeof result.number === 'number' ? `#${result.number}` : null, result.url);
    case 'github.commit':
      return link('commit', result.commit_sha, null);
    case 'github.push':
      return [...link('branch', result.branch, result.url), ...link('commit', result.sha, null)];
    case 'github.comment':
      return link('comment', result.url, result.url);
    case 'github.create_branch':
      return [...link('branch', result.branch, null), ...link('commit', result.sha, null)];
    case 'github.checkout':
      return link('commit', result.commit_sha, null);
    case 'github.list_repositories':
      return Array.isArray(result.repositories) ? link('repositories', result.repositories.length, null) : [];
    case 'github.list_issues':
      return Array.isArray(result.issues) ? link('issues', result.issues.length, null) : [];
    case 'github.list_pull_requests':
      return Array.isArray(result.pull_requests) ? link('pull_requests', result.pull_requests.length, null) : [];
    case 'github.get_issue':
    case 'github.create_issue':
      return issueLink(result);
    case 'github.update_issue':
      return [
        ...issueLink(result),
        ...link('state', known(result.state), null),
        ...link('state_reason', known(result.state_reason), null),
      ];
    case 'github.request_connection':
      return link('login', result.login, null);
    case 'subagent.request':
      return typeof result.session_id === 'string'
        ? [{ label: 'session', text: result.session_id, session: result.session_id }]
        : [];
    case 'task.set_title':
      return params.status === 'verified' ? link('title', params.value.params.title, null) : [];
    case 'workvm.request':
    case 'workvm.release':
      return link('workvm', result.workvm_id, null);
    case TEST_RUN_TYPE:
      return [...link('run', result.run_id, null), ...link('exit_code', result.exit_code, null)];
    default:
      return [];
  }
}

/** The view of `operation` (module comment); `row` from the slice when known. */
export function operationView(
  entries: Iterable<EventEntry>,
  operation: string,
  row: OperationRow | null,
): OperationView {
  const list = [...entries];
  const device = row?.device_id ?? null;
  const params = part(find(list, operation, OPERATION_KINDS[0]), operation, device, readParams);
  const result = part(find(list, operation, OPERATION_KINDS[1]), operation, device, readResult);
  const type = row?.type ?? (params.status === 'verified' ? params.value.type : 'unknown');
  const state = row?.state ?? (result.status === 'verified' ? result.value.status : 'received');
  const succeeded = result.status === 'verified' && result.value.result !== null ? result.value.result : null;
  return {
    id: operation,
    type,
    state,
    errorCode: row?.error_code ?? (result.status === 'verified' ? (result.value.error?.code ?? null) : null),
    duplicateOf: row?.duplicate_of ?? null,
    sessionId: row?.session_id ?? null,
    createdAt: row?.created_at ?? null,
    row,
    params,
    result,
    subject: params.status === 'verified' ? subjectOf(type, params.value.params) : null,
    facts: succeeded === null ? [] : factsOf(type, succeeded, params),
    listing: succeeded === null ? null : listingOf(type, succeeded),
    issue: succeeded === null ? null : issueOf(type, succeeded),
    testRun: succeeded === null || type !== TEST_RUN_TYPE ? null : parseTestRunResult(succeeded),
  };
}

/** A memoized selector of the view of `operation` in workgroup `wg`. */
export function selectOperation(wg: string, operation: string): (state: StoreState) => OperationView {
  let last: { scope: unknown; row: unknown; view: OperationView } | null = null;
  return (state) => {
    const scope = state.events[wg];
    const row = state.operations.rows[operation] ?? null;
    if (last !== null && last.scope === scope && last.row === row) return last.view;
    const view = operationView(scope?.entries.values() ?? [], operation, row);
    last = { scope, row, view };
    return view;
  };
}

/** Where an operation's chat item lives: the org, workgroup and task of its verified event at `seq`, or null. */
export function locateOperation(
  state: StoreState,
  seq: number,
  operation: string,
): { readonly org: string; readonly wg: string; readonly task: string } | null {
  for (const [scopeId, scope] of Object.entries(state.events)) {
    const entry = scope.entries.get(seq);
    if (entry?.status !== 'verified' || entry.header!.ref !== operation) continue;
    return { org: entry.org, wg: scopeId, task: entry.header!.task as string };
  }
  return null;
}

/**
 * The purpose of a subagent child from its origin operation (`subagent.request` parameters): the hub names the
 * operation (`origin_operation_id`), so its verified result must name this child (a hub could swap purposes); null
 * while the parameters are not read or carry no purpose.
 */
export function purposeOf(
  view: OperationView,
  child: string,
):
  | { readonly status: 'verified'; readonly text: string }
  | { readonly status: 'unverifiable'; readonly code: string }
  | null {
  const result = view.result.status === 'verified' ? view.result.value.result : null;
  if (result !== null && result.session_id !== child) return { status: 'unverifiable', code: 'header_invalid' };
  if (view.params.status === 'unverifiable') return { status: 'unverifiable', code: view.params.code };
  const purpose = view.params.status === 'verified' ? view.params.value.params.purpose : undefined;
  return typeof purpose === 'string' ? { status: 'verified', text: purpose } : null;
}
