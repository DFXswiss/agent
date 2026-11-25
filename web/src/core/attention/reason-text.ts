// The plain words of an attention reason with the time it continues by itself (ADR-081 section 5): `quota_wait` and
// `quota_choice` name their `wait_until`; every other reason the words of `reasonKey`. Only the situation view (a lazily
// loaded screen) needs it, so it stays out of the first load.
import { parseTime } from '../encoding/time';
import type { Params } from '../i18n/catalog';
import { formatClock, type TimeOptions } from '../i18n/format';
import type { AttentionRow, Category } from '../store/slices/attention';
import { REASONS, reasonKey } from './view';

/** The reasons whose text names the time the task continues by itself (ADR-081 section 5). */
export const TIMED_REASONS: ReadonlySet<string> = new Set(['quota_wait', 'quota_choice']);

/**
 * The message key and parameters of a reason's plain words: a timed reason of its own category with a known
 * `wait_until` names that time (`attention.reason.<reason>.at`), otherwise the words of `reasonKey`.
 */
export function reasonText(
  category: Category,
  row: Pick<AttentionRow, 'reason' | 'wait_until'>,
  options: TimeOptions,
): { readonly key: string; readonly params: Params } {
  const key = reasonKey(category, row.reason);
  const until = row.wait_until ?? null;
  if (until === null || !TIMED_REASONS.has(row.reason) || !REASONS[category].includes(row.reason)) {
    return { key, params: {} };
  }
  return { key: `${key}.at`, params: { time: formatClock(parseTime(until), options) } };
}
