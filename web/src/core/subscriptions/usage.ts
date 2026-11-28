// Who used how much of which subscription (ADR-103; twin of `agent_platform/subscriptions.py` `subscriptions usage`):
// `GET /v1/me/subscriptions/usage?period=five_hour|week|range[&since&until]`.
//
// - `five_hour` / `week`: the running window of each subscription as its newest reading names it; `measured` carries
//   the window's used share, its reset and the time of the reading, null when there is no such reading (then the last
//   5 hours or 7 days count and no share of the quota can be estimated).
// - `range`: a span the user chooses (at most 92 days); the provider measures no quota for it, so `measured` is null.
//
// Per person the hub counts requests, input, output, cache read and cache write tokens, their sum and the sessions,
// the person's share of the tokens on that subscription and `estimatedPercent` = that share times the measured use.
// The provider measures the quota per subscription only, so this is an estimate and always shown as such.
// A subscription of the caller lists every person and the total; another owner's subscription the caller used lists
// the caller's own row only (`total` null). Numbers only, no content. A malformed answer is `unavailable`; a malformed
// subscription or person is left out (nothing is shown twice).
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { RequestsContext } from '../requests/client';

export const PERIODS = ['five_hour', 'week', 'range'] as const;
export type UsagePeriod = (typeof PERIODS)[number];
/** The longest span of `range` the hub answers. */
export const MAX_DAYS = 92;
const DAY_MS = 86_400_000;

export interface UsageFigures {
  readonly requests: number;
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly cacheReadTokens: number;
  readonly cacheWriteTokens: number;
  readonly tokens: number;
  readonly sessions: number;
}

export interface PersonUsage extends UsageFigures {
  readonly userId: string;
  /** 0..1: the person's share of all tokens on the subscription in the span. */
  readonly tokenShare: number;
  /** The estimated share of the quota in percent, null without a measured window. */
  readonly estimatedPercent: number | null;
}

export interface Measured {
  readonly usedPercent: number;
  /** Unix ms. */
  readonly resetsAt: number;
  /** Unix ms of the reading. */
  readonly fetchedAt: number;
}

export interface SubscriptionUsage {
  readonly subscriptionId: string;
  readonly ownerId: string;
  readonly primary: boolean;
  /** The caller owns it (every person and the total are listed). */
  readonly own: boolean;
  /** Unix ms. */
  readonly since: number;
  /** Unix ms. */
  readonly until: number;
  readonly measured: Measured | null;
  readonly total: UsageFigures | null;
  readonly people: readonly PersonUsage[];
}

export interface UsageAnswer {
  readonly period: UsagePeriod;
  readonly subscriptions: readonly SubscriptionUsage[];
}

const time = (value: unknown): number | null => {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
};

const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;

const share = (value: unknown, most: number): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= most ? value : null;

function figures(value: Record<string, unknown>): UsageFigures | null {
  const found = {
    requests: count(value.requests),
    inputTokens: count(value.input_tokens),
    outputTokens: count(value.output_tokens),
    cacheReadTokens: count(value.cache_read_tokens),
    cacheWriteTokens: count(value.cache_write_tokens),
    tokens: count(value.tokens),
    sessions: count(value.sessions),
  };
  return Object.values(found).every((item) => item !== null) ? (found as UsageFigures) : null;
}

function person(value: unknown): PersonUsage | null {
  if (!isRecord(value) || typeof value.user_id !== 'string') return null;
  const found = figures(value);
  const tokenShare = share(value.token_share, 1);
  const estimated = value.estimated_percent === null ? null : share(value.estimated_percent, 1000);
  if (found === null || tokenShare === null || (value.estimated_percent !== null && estimated === null)) return null;
  return { ...found, userId: value.user_id, tokenShare, estimatedPercent: estimated };
}

function measured(value: unknown): Measured | null {
  if (!isRecord(value)) return null;
  const usedPercent = share(value.used_percent, 1000);
  const resetsAt = time(value.resets_at);
  const fetchedAt = time(value.fetched_at);
  return usedPercent === null || resetsAt === null || fetchedAt === null ? null : { usedPercent, resetsAt, fetchedAt };
}

function subscription(value: unknown): SubscriptionUsage | null {
  if (!isRecord(value) || typeof value.subscription_id !== 'string' || typeof value.owner_id !== 'string') return null;
  const since = time(value.since);
  const until = time(value.until);
  if (since === null || until === null || typeof value.own !== 'boolean' || !Array.isArray(value.people)) return null;
  const total = isRecord(value.total) ? figures(value.total) : null;
  const seen = new Set<string>();
  const people = value.people.flatMap((item) => {
    const found = person(item);
    if (found === null || seen.has(found.userId)) return [];
    seen.add(found.userId);
    return [found];
  });
  return {
    subscriptionId: value.subscription_id,
    ownerId: value.owner_id,
    primary: value.primary === true,
    own: value.own,
    since,
    until,
    measured: measured(value.measured),
    total: value.own ? total : null,
    people,
  };
}

/** The checked answer (module comment). */
export function parseUsage(body: unknown): UsageAnswer {
  if (!isRecord(body) || !Array.isArray(body.subscriptions) || !PERIODS.includes(body.period as UsagePeriod)) {
    throw new HubError('unavailable', 200);
  }
  const seen = new Set<string>();
  const subscriptions = body.subscriptions.flatMap((item) => {
    const found = subscription(item);
    if (found === null || seen.has(found.subscriptionId)) return [];
    seen.add(found.subscriptionId);
    return [found];
  });
  return { period: body.period as UsagePeriod, subscriptions };
}

/** A local calendar day `YYYY-MM-DD` as Unix ms of its local midnight, null when it is no day. */
export function dayStart(day: string): number | null {
  const match = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(day);
  if (match === null) return null;
  const at = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])).getTime();
  return Number.isFinite(at) && new Date(at).getDate() === Number(match[3]) ? at : null;
}

/** The local calendar day of `at` as `YYYY-MM-DD`. */
export function dayOf(at: number): string {
  const date = new Date(at);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The span of the days `from` to `to` (both included, local time), or null when it is not a valid span of at most
 *  `MAX_DAYS`. */
export function rangeOf(from: string, to: string): { readonly since: number; readonly until: number } | null {
  const since = dayStart(from);
  const last = dayStart(to);
  if (since === null || last === null) return null;
  const next = new Date(last);
  next.setDate(next.getDate() + 1); // the next local midnight, also across a change of the clock
  const until = next.getTime();
  return until > since && until - since <= MAX_DAYS * DAY_MS ? { since, until } : null;
}

/** The request target of a period (`range` needs the span). */
export function usageTarget(period: UsagePeriod, span?: { readonly since: number; readonly until: number }): string {
  const params = new URLSearchParams({ period });
  if (period === 'range' && span !== undefined) {
    params.set('since', new Date(span.since).toISOString());
    params.set('until', new Date(span.until).toISOString());
  }
  return `/v1/me/subscriptions/usage?${params.toString()}`;
}

export async function loadUsage(
  context: RequestsContext,
  period: UsagePeriod,
  span?: { readonly since: number; readonly until: number },
): Promise<UsageAnswer> {
  return parseUsage(await context.client.request('GET', usageTarget(period, span)));
}

/** A token count as a short number of the locale (`12.3k`, `4.1M`). */
export function formatTokens(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}
