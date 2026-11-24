// States of the org path of the invitation screen (06-web.md section 3.1): `org_verify` → `intro` (nobody signed in:
// "I already have an account – sign in and join" first, "New here – create an account with a passkey" second) or
// `existing_account` (a stored session: one button joins, with one passkey gesture when the keys are locked) → `accept`
// → `done` (KOR-36). The step waits until the stored session was restored: a session still `starting` must not land
// in the new-user path, which would create a second identity. Pure apart from `settledPhase`: the component runs the
// effects and moves between steps.
import type { OrgInvitation } from '../../../../core/account/join';
import type { SignIn } from '../../../../core/account/login';
import type { Registration } from '../../../../core/account/register';
import type { SessionPhase } from '../../../../core/store/slices/session';
import { type Store, subscribe } from '../../../../core/store/store';
import { HubError } from '../../../../core/net/signedFetch';
import { errorCode } from '../../../shell/errors';

export type OrgStep =
  | { readonly step: 'org_verify' }
  /** The package or the log does not hold up (`invite_code_invalid`, `invite_not_in_log`, `log_*`): nothing is sent. */
  | { readonly step: 'blocked'; readonly code: string }
  /** Unknown, used, revoked or expired (`invite_unknown`, `invite_expired`), or bound to another user. */
  | { readonly step: 'invalid'; readonly code: string }
  | { readonly step: 'intro'; readonly invitation: OrgInvitation; readonly notice: string | null }
  | { readonly step: 'create_passkey'; readonly invitation: OrgInvitation }
  | {
      readonly step: 'unlock_passkey';
      readonly invitation: OrgInvitation;
      readonly registration: Registration;
      readonly busy: boolean;
      readonly notice: string | null;
    }
  /** The new passkey gave no PRF output: the registration was withdrawn (`released`: the invitation is free now). */
  | { readonly step: 'prf_required'; readonly invitation: OrgInvitation; readonly released: boolean }
  | {
      readonly step: 'existing_account';
      readonly invitation: OrgInvitation;
      /**
       * `sign_in`: no unlocked identity yet (the join button signs in and joins in one go); `recovery`: the passkey
       * could not unlock (the join follows the unlock); `ready`: unlocked, the join button joins.
       */
      readonly mode: 'sign_in' | 'recovery' | 'ready';
      readonly signIn: SignIn | null;
      readonly busy: boolean;
      readonly notice: string | null;
      /** No display name was found in the account's orgs and none typed: the field is asked for (rare). */
      readonly needName: boolean;
      /** `recovery`: the sign-in was of another account than the one before (its typed or read name is not used). */
      readonly changed: boolean;
    }
  | {
      readonly step: 'accept';
      readonly invitation: OrgInvitation;
      readonly failed: string | null;
    }
  | { readonly step: 'done'; readonly org: string; readonly orgName: string };

const INVALID = new Set(['invite_unknown', 'invite_expired', 'invite_registered']);
/** Revoked and consumed invitations get the one message of unknown ones (06-web.md section 3.1 `invalid`). */
const UNKNOWN = new Set(['invite_revoked', 'invite_consumed']);
const BLOCKING = new Set([
  'invite_code_invalid',
  'invite_code_length',
  'invite_not_in_log',
  'header_invalid',
  'non_ascii',
]);

/** Whether `code` is a refusal of the package or the log (blocking, 06-web.md section 3.1 `org_verify`). */
export function blocking(code: string): boolean {
  return BLOCKING.has(code) || (code.startsWith('log_') && code !== 'log_accept');
}

/** The step for a failure while verifying or joining; null for failures the current step shows with a retry. */
export function failedStep(error: unknown): OrgStep | null {
  const code = errorCode(error);
  const unknown = UNKNOWN.has(code) || (error instanceof HubError && error.status === 404);
  const invalid = INVALID.has(code) ? code : unknown ? 'invite_unknown' : null;
  if (invalid !== null) return { step: 'invalid', code: invalid };
  return !(error instanceof HubError) && blocking(code) ? { step: 'blocked', code } : null;
}

/** The step for a failed verification: anything that is not an invalid invitation blocks (nothing is sent). */
export function verifyFailed(error: unknown): OrgStep {
  return failedStep(error) ?? { step: 'blocked', code: errorCode(error) };
}

const MODES: Partial<Record<SessionPhase, 'ready' | 'sign_in'>> = { unlocked: 'ready', locked: 'sign_in' };

/** The step after the invitation verified: a signed-in user joins with the existing account directly. */
export function afterVerify(invitation: OrgInvitation, phase: SessionPhase): OrgStep {
  const mode = MODES[phase];
  return mode === undefined ? { step: 'intro', invitation, notice: null } : existingStep(invitation, mode);
}

/** The `existing_account` step in `mode`, idle. */
export function existingStep(invitation: OrgInvitation, mode: 'sign_in' | 'ready'): OrgStep {
  return {
    step: 'existing_account',
    invitation,
    mode,
    signIn: null,
    busy: false,
    notice: null,
    needName: false,
    changed: false,
  };
}

/** The session phase once the stored session was restored (`starting` is over). */
export function settledPhase(store: Store): Promise<SessionPhase> {
  const phase = store.getState().session.phase;
  if (phase !== 'starting') return Promise.resolve(phase);
  return new Promise((resolve) => {
    const stop = subscribe(
      store,
      (state) => state.session.phase,
      (next) => {
        stop();
        resolve(next);
      },
    );
  });
}
