// Subscription quotas (06-web.md section 8.2, 03-api.md section 4.1 `GET /v1/me/quotas`, ADR-010): the windows the
// polling execution device last read for the user's subscription – `five_hour`, `seven_day` and `weekly:<model>` –
// as bars with the used share and the reset time. Several devices may have reported a window over time; the newest
// `fetched_at` wins. Bars are ordered 5 h, week, then per model by name. Malformed rows are left out; an answer
// without rows means the quota is "not available" (no subscription, or the usage endpoint did not answer: quota
// polling is feature-detected).
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { RequestsContext } from '../requests/client';
import { setQuotas, type QuotaBar } from '../store/slices/quotas';

const WINDOW_RE = /^(five_hour|seven_day|weekly:[A-Za-z0-9._-]{1,64})$/;

const time = (value: unknown): number | null => {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
};

/** One row of the answer as a bar, or null when it is malformed. */
export function quotaBar(value: unknown): QuotaBar | null {
  if (!isRecord(value) || typeof value.window !== 'string' || !WINDOW_RE.test(value.window)) return null;
  const used = value.used_percent;
  if (typeof used !== 'number' || !Number.isFinite(used) || used < 0) return null;
  const fetchedAt = time(value.fetched_at);
  const resetsAt = value.resets_at === null ? null : time(value.resets_at);
  if (fetchedAt === null || (value.resets_at !== null && resetsAt === null)) return null;
  const severity = typeof value.severity === 'string' ? value.severity : null;
  const model = value.window.startsWith('weekly:') ? value.window.slice('weekly:'.length) : null;
  return { window: value.window, model, usedPercent: used, resetsAt, severity, fetchedAt };
}

const rank = (bar: QuotaBar): number => (bar.window === 'five_hour' ? 0 : bar.window === 'seven_day' ? 1 : 2);

/** The newest bar per window, ordered (module comment). */
export function quotaBars(rows: readonly unknown[]): QuotaBar[] {
  const newest = new Map<string, QuotaBar>();
  for (const bar of rows.map(quotaBar)) {
    if (bar === null) continue;
    const known = newest.get(bar.window);
    if (known === undefined || known.fetchedAt < bar.fetchedAt) newest.set(bar.window, bar);
  }
  return [...newest.values()].sort(
    (a, b) => rank(a) - rank(b) || Number(a.window > b.window) - Number(a.window < b.window),
  );
}

/** `GET /v1/me/quotas` into the `quotas` slice; a malformed answer is `unavailable`. */
export async function loadQuotas(context: RequestsContext): Promise<readonly QuotaBar[]> {
  const generation = context.store.generation;
  const body = await context.client.request('GET', '/v1/me/quotas');
  if (!isRecord(body) || !Array.isArray(body.quotas)) throw new HubError('unavailable', 200);
  const bars = quotaBars(body.quotas);
  if (context.store.generation === generation) setQuotas(context.store, bars);
  return bars;
}

/** The share of a bar for the meter: 0…1 (a used share above 100 % fills the bar). */
export const barShare = (bar: QuotaBar): number => Math.min(bar.usedPercent, 100) / 100;
