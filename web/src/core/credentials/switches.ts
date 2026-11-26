// The newest model switch of a task (ADR-081 section 5): when the platform moves a session to another model access
// (its access was used up, could not be used or was released again) or continues a session whose access the provider
// refused once it was renewed (`renewed`, HOT-26), the runner writes the audit row
// `session.model_switched` with the access kinds and models before and after, the cause and the release time of the
// used-up access – metadata only. The task view shows the newest one as a line ("Model switched: … → …"). The rows come
// from the organization's audit filtered by the action and the task (`GET /v1/orgs/{org}/audit`, members see the rows
// of the workgroups they may read); a row whose detail does not have this form (an access or a cause outside the
// closed lists of the contract) is left out.
import { parseTime } from '../encoding/time';
import { type AuditRow, auditTarget, NO_FILTERS, parseAuditPage } from '../monitoring/audit';
import type { RequestsContext } from '../requests/client';

export const SWITCH_ACTION = 'session.model_switched';

export interface ModelSwitch {
  /** Unix ms of the switch. */
  readonly at: number;
  readonly sessionId: string | null;
  /** One of `SWITCH_ACCESSES`. */
  readonly fromAccess: string;
  readonly toAccess: string;
  readonly fromModel: string | null;
  readonly toModel: string | null;
  /** One of `SWITCH_CAUSES`. */
  readonly cause: string;
  /** Unix ms of the release of the used-up access, null without one. */
  readonly until: number | null;
}

/** The access kinds of a switch (`model_access` and `operator`, an operator subscription). */
export const SWITCH_ACCESSES: readonly string[] = ['subscription', 'operator', 'api_key', 'platform'];
/** The causes of a switch. */
export const SWITCH_CAUSES: readonly string[] = ['exhausted', 'unusable', 'released', 'renewed'];

const text = (value: unknown): value is string => typeof value === 'string' && value !== '';
const oneOf = (list: readonly string[], value: unknown): value is string => list.includes(value as string);
const optionalText = (value: unknown): value is string | null => value === null || text(value);

function time(value: unknown): number | null | undefined {
  if (value === null) return null;
  if (typeof value !== 'string') return undefined;
  try {
    return parseTime(value);
  } catch {
    return undefined;
  }
}

/** The switch of an audit row, or null when the row is not one or its detail is malformed. */
export function modelSwitch(row: AuditRow): ModelSwitch | null {
  const { detail } = row;
  const until = time(detail.until);
  if (row.action !== SWITCH_ACTION || !oneOf(SWITCH_ACCESSES, detail.from_access)) return null;
  if (!oneOf(SWITCH_ACCESSES, detail.to_access) || !oneOf(SWITCH_CAUSES, detail.cause)) return null;
  if (!optionalText(detail.from_model) || !optionalText(detail.to_model) || until === undefined) return null;
  return {
    at: Date.parse(row.at),
    sessionId: row.session_id,
    fromAccess: detail.from_access,
    toAccess: detail.to_access,
    fromModel: detail.from_model,
    toModel: detail.to_model,
    cause: detail.cause,
    until,
  };
}

/** The task's newest model switch, null when it has none. */
export async function loadModelSwitch(
  context: RequestsContext,
  org: string,
  task: string,
): Promise<ModelSwitch | null> {
  const target = auditTarget(org, { ...NO_FILTERS, action: SWITCH_ACTION, task }, null);
  const page = parseAuditPage(await context.client.request('GET', target));
  return page.rows.map(modelSwitch).find((found) => found !== null) ?? null;
}
