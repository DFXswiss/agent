// The window of the organization lists (members, workgroups, devices and the invitation lists, ADR-086 section 1):
// rows shown at first are two pages, a move adds `PAGE`, at most `MAX_ROWS` are rendered. `sameValue` is the content
// equality of the rows' memo: a rebuilt row (a new log head, the presence poll) renders again only if it changed.

/** Rows added by a move. */
export const PAGE = 50;
/** The most rows ever rendered. */
export const MAX_ROWS = 200;

/**
 * Structural equality of row data: the same primitives, functions and maps by identity, plain objects and arrays
 * field by field.
 */
export function sameValue(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  if (a instanceof Map || b instanceof Map) return false;
  const keys = Object.keys(a);
  const record = (value: object, key: string) => (value as Record<string, unknown>)[key];
  return (
    keys.length === Object.keys(b).length && keys.every((key) => key in b && sameValue(record(a, key), record(b, key)))
  );
}
