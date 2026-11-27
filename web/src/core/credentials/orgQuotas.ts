// Model access and quotas of an organization (ADR-081 section 6, 03-api.md `GET /v1/orgs/{org}/quotas`): the caller's
// own subscription (its windows as `quotas.ts` reads them, since when it is not readable, the latest observed limit),
// the operator's subscriptions when the organization may use them (per subscription its label, state, whether its
// quota is readable, the used share of its binding window, the reset, the latest observed limit and its end), whether
// DFX access over OpenRouter is available, and the summary (`usable`, `exhausted`, `next_release`). "Not readable" is
// shown as such with the last observed limit time; nothing is estimated. Malformed rows are left out; an answer
// without the own part, the two flags or the summary is `unavailable` (a hub of an older release has no such route and
// answers `not_found`). The settings section "Model access and quotas" and the counter of the situation view read it.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { RequestsContext } from '../requests/client';
import type { QuotaBar } from '../store/slices/quotas';
import { freePercent, quotaBars } from './quotas';

/** The states of an operator subscription (`operator_quota` of ADR-081 section 6). */
export const SUBSCRIPTION_STATES = ['usable', 'exhausted', 'blocked', 'deferred', 'ignored'] as const;

/** The latest observed limit of a subscription (Unix ms). */
export interface Limited {
  readonly at: number;
  /** The release, null when the provider names none. */
  readonly until: number | null;
  readonly readable: boolean;
}

export interface OperatorSubscription {
  /** The file name the operator chose. */
  readonly label: string;
  /** One of `SUBSCRIPTION_STATES`; a newer hub may send another one (shown generically). */
  readonly state: string;
  readonly readable: boolean;
  readonly usedPercent: number | null;
  readonly resetsAt: number | null;
  readonly limitedAt: number | null;
  readonly until: number | null;
  readonly fetchedAt: number | null;
}

export interface OrgQuotas {
  readonly own: {
    readonly bars: readonly QuotaBar[];
    readonly unreadableSince: number | null;
    readonly limited: Limited | null;
  };
  readonly operator: { readonly granted: boolean; readonly subscriptions: readonly OperatorSubscription[] };
  readonly platformAvailable: boolean;
  readonly summary: { readonly usable: number; readonly exhausted: number; readonly nextRelease: number | null };
}

/** `[well-formed, Unix ms]` of an optional time: null is well-formed, a malformed time is not. */
function when(value: unknown): readonly [boolean, number | null] {
  if (value === null || value === undefined) return [true, null];
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? [true, parsed] : [false, null];
}

const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

function limited(value: unknown): Limited | null {
  if (!isRecord(value) || typeof value.readable !== 'boolean') return null;
  const [atOk, at] = when(value.at);
  const [untilOk, until] = when(value.until);
  return atOk && at !== null && untilOk ? { at, until, readable: value.readable } : null;
}

/** One operator subscription, or null when it is malformed. */
export function operatorSubscription(value: unknown): OperatorSubscription | null {
  if (!isRecord(value) || typeof value.label !== 'string' || value.label === '') return null;
  if (typeof value.state !== 'string' || typeof value.readable !== 'boolean') return null;
  const used = value.used_percent ?? null;
  if (used !== null && (typeof used !== 'number' || !Number.isFinite(used) || used < 0)) return null;
  const times = [value.resets_at, value.limited_at, value.until, value.fetched_at].map(when);
  if (!times.every(([ok]) => ok)) return null;
  const [resetsAt, limitedAt, until, fetchedAt] = times.map(([, at]) => at);
  return {
    label: value.label,
    state: value.state,
    readable: value.readable,
    usedPercent: used,
    resetsAt: resetsAt!,
    limitedAt: limitedAt!,
    until: until!,
    fetchedAt: fetchedAt!,
  };
}

/** The answer of `GET /v1/orgs/{org}/quotas` (module comment). */
export function parseOrgQuotas(body: unknown): OrgQuotas {
  if (!isRecord(body)) throw new HubError('unavailable', 200);
  const { own, operator, platform, summary } = body;
  if (!isRecord(own) || !isRecord(operator) || !isRecord(platform) || !isRecord(summary)) {
    throw new HubError('unavailable', 200);
  }
  const [releaseOk, nextRelease] = when(summary.next_release);
  if (
    typeof operator.granted !== 'boolean' ||
    typeof platform.available !== 'boolean' ||
    !isCount(summary.usable) ||
    !isCount(summary.exhausted) ||
    !releaseOk
  ) {
    throw new HubError('unavailable', 200);
  }
  const subscriptions = Array.isArray(operator.subscriptions) ? operator.subscriptions : [];
  const since = isRecord(own.unreadable) ? when(own.unreadable.since)[1] : null;
  return {
    own: {
      bars: quotaBars(Array.isArray(own.windows) ? own.windows : []),
      unreadableSince: since,
      limited: limited(own.limited),
    },
    operator: {
      granted: operator.granted,
      subscriptions: subscriptions.map(operatorSubscription).filter((row) => row !== null),
    },
    platformAvailable: platform.available,
    summary: { usable: summary.usable, exhausted: summary.exhausted, nextRelease },
  };
}

/** `GET /v1/orgs/{org}/quotas`. */
export async function loadOrgQuotas(context: RequestsContext, org: string): Promise<OrgQuotas> {
  return parseOrgQuotas(await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/quotas`));
}

/** What is left of a subscription's binding window for a meter: 0…1 (`freePercent`), null when unknown. */
export const subscriptionShare = (row: OperatorSubscription, now: number): number | null =>
  row.usedPercent === null ? null : freePercent(row.usedPercent, row.resetsAt, now) / 100;
