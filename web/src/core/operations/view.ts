// What the operation card and the Operations tab show (06-web.md sections 5.2 and 5.4, 03-api.md sections 9 and 10,
// ADR-014): the hub's row (type, live state, error code, `duplicate_of`) joined with the decrypted `operation.params`
// and `operation.result` events of the operation – events of the workgroup scope whose `ref` is the operation.
//
// Events count only when the reader verified them and their body names the same operation (`operation_id`); an event
// that fails is reported with its code, never as content. Without a row (not loaded yet) the state comes from the
// verified result (`status`), else `received`. Result facts are the links and values of 03-api.md section 9: the PR
// (number, URL), the commit SHA, the pushed branch (SHA, URL), the comment URL, the created branch, the checkout
// commit, the subagent session, the new task title (`task.set_title`: "renamed by session") and the number of listed
// repositories. URLs pass `safeUrl` (http(s) only; the contracts allow https only).
import { safeUrl } from '../chat/links';
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
}

const raw = (entry: EventEntry): Readonly<Record<string, unknown>> =>
  isRecord(entry.event) && isRecord(entry.event.header) ? entry.event.header : {};

const text = (value: unknown): string | null => (typeof value === 'string' ? value : null);

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

/** A short description of the parameters (module comment). */
export function subjectOf(type: string, params: Readonly<Record<string, unknown>>): string | null {
  if (type === 'subagent.request') return text(params.purpose);
  if (type === 'task.set_title') return text(params.title);
  if (type === 'workvm.request') return text(params.size) ?? 'standard';
  if (type === 'workvm.release') return text(params.scope) ?? 'task';
  const repo = text(params.repo);
  if (repo === null) return null;
  if (typeof params.number === 'number') return `${repo}#${params.number}`;
  const branch = text(params.branch) ?? text(params.head);
  return branch === null ? repo : `${repo} ${branch}`;
}

function link(label: string, value: unknown, href: unknown): Fact[] {
  const url = typeof href === 'string' ? safeUrl(href) : null;
  if (typeof value !== 'string' && typeof value !== 'number') return [];
  return [{ label, text: String(value), ...(url !== null && { href: url }) }];
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
    case 'subagent.request':
      return typeof result.session_id === 'string'
        ? [{ label: 'session', text: result.session_id, session: result.session_id }]
        : [];
    case 'task.set_title':
      return params.status === 'verified' ? link('title', params.value.params.title, null) : [];
    case 'workvm.request':
    case 'workvm.release':
      return link('workvm', result.workvm_id, null);
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
