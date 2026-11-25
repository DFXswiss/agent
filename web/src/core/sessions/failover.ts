// The device failure of a task's current main session (ADR-086 sections 2, 3 and 6, KOR-58): the hub reports on every
// `session_row` `interrupted_at` (the session's device failed while it was active; the session continues there or on
// another device by itself) and `failover_of` (this session continues the session lost to a device failure, from the
// last synchronised checkpoint). Older hubs send neither field, so anything missing or malformed means "nothing to
// show". An ended session (`stopped`, `failed`) is no longer interrupted.
import { isUuid7 } from '../encoding/ids';
import { parseTime } from '../encoding/time';
import { isRecord } from '../registry';
import type { SessionEntry } from '../store/slices/tasks';

export interface DeviceFailure {
  /** Since when the session's device is unreachable (hub clock, unix ms); null while the session is not interrupted. */
  readonly interruptedAt: number | null;
  /** The lost session this session continues; null for a session that continues none. */
  readonly failoverOf: string | null;
}

const ENDED: ReadonlySet<unknown> = new Set(['stopped', 'failed']);

function time(value: unknown): number | null {
  try {
    return parseTime(value);
  } catch {
    return null;
  }
}

/** The device failure a session row reports, or null when it reports none. */
export function deviceFailure(entry: SessionEntry | null | undefined): DeviceFailure | null {
  const raw: unknown = entry?.row;
  const row: Readonly<Record<string, unknown>> = isRecord(raw) ? raw : {};
  const interruptedAt = ENDED.has(row.state) ? null : time(row.interrupted_at);
  const failoverOf = isUuid7(row.failover_of) ? row.failover_of : null;
  return interruptedAt === null && failoverOf === null ? null : { interruptedAt, failoverOf };
}
