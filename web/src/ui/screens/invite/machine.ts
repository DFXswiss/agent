// States of the invitation screen (06-web.md section 3.1) for platform invitations; org invitations branch into
// `org`, which WEB-08 fills in (`screens/invite/org/`). Pure: the screen runs the effects and moves between steps.
import type { PreparedOrg } from '../../../core/account/genesis';
import type { Lookup, Registration } from '../../../core/account/register';
import { HubError } from '../../../core/net/signedFetch';
import { errorCode } from '../../shell/errors';

export type Step =
  | { readonly step: 'parsing' }
  | { readonly step: 'lookup' }
  /** Unknown, used, revoked or expired invitation; `invite_expired` separately. */
  | { readonly step: 'invalid'; readonly code: string }
  /** A code that is not a code (`invite_code_length`, `invite_code_invalid`): "Check the code". */
  | { readonly step: 'malformed'; readonly code: string }
  /** Anything else (hub unavailable …): retry the lookup. */
  | { readonly step: 'error'; readonly code: string }
  | { readonly step: 'org'; readonly lookup: Lookup }
  | { readonly step: 'intro'; readonly lookup: Lookup; readonly notice: string | null }
  | { readonly step: 'create_passkey'; readonly lookup: Lookup }
  | {
      readonly step: 'unlock_passkey';
      readonly lookup: Lookup;
      readonly registration: Registration;
      readonly busy: boolean;
      readonly notice: string | null;
    }
  /** The new passkey gave no PRF output: the registration was withdrawn (`released`: the invitation is free now). */
  | { readonly step: 'prf_required'; readonly lookup: Lookup; readonly released: boolean }
  | {
      readonly step: 'accept';
      readonly lookup: Lookup;
      readonly registration: Registration;
      readonly prepared: PreparedOrg | null;
      readonly failed: string | null;
    }
  | { readonly step: 'done'; readonly org: string };

const INVALID = new Set(['invite_unknown', 'invite_expired', 'invite_registered']);
const MALFORMED = new Set(['invite_code_length', 'invite_code_invalid']);

/** The step for a failed parse or lookup. */
export function failedLookup(error: unknown): Step {
  const code = errorCode(error);
  if (MALFORMED.has(code)) return { step: 'malformed', code };
  if (INVALID.has(code) || (error instanceof HubError && error.status === 404)) {
    return { step: 'invalid', code: INVALID.has(code) ? code : 'invite_unknown' };
  }
  return { step: 'error', code };
}

/**
 * The step after a successful lookup: org invitations go to the org path; a platform invitation must be a user
 * invitation that has not registered anybody yet (a registration that was not finished on this page cannot be resumed
 * here: `invite_registered`; the hub releases it after 24 h).
 */
export function afterLookup(lookup: Lookup): Step {
  if (lookup.scope === 'org') return { step: 'org', lookup };
  if (lookup.kind !== 'user') return { step: 'invalid', code: 'invite_unknown' };
  if (lookup.registered) return { step: 'invalid', code: 'invite_registered' };
  return { step: 'intro', lookup, notice: null };
}
