// Strict checks of decrypted event bodies against `contracts/events/objects.json` (`body_workvm_*`, `body_test_run`):
// every required field present and valid, an optional field valid when present, no other field. Shared by the work
// VM (`workvm.ts`) and the test run (`testrun.ts`) parsers, with the ordering helpers of their selectors.
import { parseTime } from '../encoding/time';
import { isUuid7 } from '../encoding/ids';
import { isRecord } from '../registry';
import type { ScopeEvents } from '../store/slices/events';

export type Check = (value: unknown) => boolean;

export const pattern =
  (re: RegExp): Check =>
  (value) =>
    typeof value === 'string' && re.test(value);
export const oneOf =
  (...values: readonly unknown[]): Check =>
  (value) =>
    values.includes(value);
export const integer =
  (min: number, max: number): Check =>
  (value) =>
    Number.isSafeInteger(value) && (value as number) >= min && (value as number) <= max;
/** JSON Schema `maxLength` counts code points. */
export const text =
  (max: number): Check =>
  (value) =>
    typeof value === 'string' && [...value].length <= max;
export const nullable =
  (check: Check): Check =>
  (value) =>
    value === null || check(value);
export const time: Check = (value) => {
  try {
    parseTime(value);
    return true;
  } catch {
    return false;
  }
};
export const count = integer(0, Number.MAX_SAFE_INTEGER);
export const uuid: Check = isUuid7;
export const flag = oneOf(true, false);

/** Every field of `shape` present and valid, every present field of `optional` valid, no other field. */
export function matches(
  body: unknown,
  shape: Readonly<Record<string, Check>>,
  optional: Readonly<Record<string, Check>> = {},
): body is Record<string, unknown> {
  if (!isRecord(body)) return false;
  const required = Object.keys(shape);
  if (!required.every((key) => Object.hasOwn(body, key))) return false;
  return Object.keys(body).every((key) => {
    const check = Object.hasOwn(shape, key) ? shape[key] : Object.hasOwn(optional, key) ? optional[key] : undefined;
    return check !== undefined && check(body[key]);
  });
}

/** `log_ref` of `contracts/ops/objects.json`: the complete output as an encrypted log object (ADR-080 section 3). */
export interface LogRef {
  readonly object_id: string;
  /** The full output length. */
  readonly bytes: number;
  /** The bytes kept (the last part when `truncated`). */
  readonly stored_bytes: number;
  readonly truncated: boolean;
}

const LOG_REF_SHAPE: Readonly<Record<keyof LogRef, Check>> = {
  object_id: uuid,
  bytes: count,
  stored_bytes: count,
  truncated: flag,
};

/** A `log_ref` value: null or the object reference. */
export const logRef: Check = (value) => value === null || matches(value, LOG_REF_SHAPE);

/** The scope's entries in scope order. */
export const sortedEntries = (scope: ScopeEvents | undefined) =>
  [...(scope?.entries.values() ?? [])].sort((a, b) => a.scopeSeq - b.scopeSeq);

/** Newest start first; equal starts keep the timeline order. */
export function newestFirst<T extends { readonly started_at: string }>(runs: readonly T[]): T[] {
  return runs
    .map((run, index) => ({ run, index, at: parseTime(run.started_at) }))
    .sort((a, b) => b.at - a.at || a.index - b.index)
    .map((entry) => entry.run);
}
