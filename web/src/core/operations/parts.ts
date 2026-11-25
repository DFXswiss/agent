// The parameters and the result of an operation (03-api.md sections 9 and 10, ADR-014): the hub's row joined with the
// decrypted `operation.params` and `operation.result` events of the workgroup scope whose `ref` is the operation.
// Events count only when the reader verified them, their body names the same operation (`operation_id`) and the
// device of the row is their author; an event that fails is reported with its code, never as content. The full card
// view (`view.ts`: subject, facts, lists) builds on these; the start of the app (the attention records) needs only
// these parts.
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

export interface OperationParts {
  readonly row: OperationRow | null;
  readonly params: Part<ParamsBody>;
  readonly result: Part<ResultBody>;
}

/** The checked parameters and result of `operation` among `entries`, with the hub's `row`. */
export function operationParts(
  entries: Iterable<EventEntry>,
  operation: string,
  row: OperationRow | null,
): OperationParts {
  const list = [...entries];
  const device = row?.device_id ?? null;
  return {
    row,
    params: part(find(list, operation, OPERATION_KINDS[0]), operation, device, readParams),
    result: part(find(list, operation, OPERATION_KINDS[1]), operation, device, readResult),
  };
}

/** A memoized selector of the parts of `operation` in workgroup `wg`. */
export function selectOperationParts(wg: string, operation: string): (state: StoreState) => OperationParts {
  let last: { scope: unknown; row: unknown; parts: OperationParts } | null = null;
  return (state) => {
    const scope = state.events[wg];
    const row = state.operations.rows[operation] ?? null;
    if (last !== null && last.scope === scope && last.row === row) return last.parts;
    const parts = operationParts(scope?.entries.values() ?? [], operation, row);
    last = { scope, row, parts };
    return parts;
  };
}
