// The Claude subscription login and the credential checks as device requests (06-web.md section 8.2, 05-runner.md
// sections 11.2 and 11.3, ADR-011, 04-crypto.md section 4.5), built on the shared client `core/requests`.
//
// - `startLogin`: `claude.login` `{mode: "manual" | "automatic"}` to the chosen device. In the relay (manual) the
//   device answers `authorize` with the link; the user authorizes in a new tab and pastes the code, which goes out as
//   a follow-up request (`sendCode`: same kind, header `follows` = the original). The device stores the
//   `claude_subscription` record itself (CK-encrypted) and answers `done` (with the subscription e-mail when the login
//   program reports it) – the token never reaches the browser. In the automatic mode (mailbox automation) the device
//   reports `progress` stages; when the automation fails it answers `authorize` with `automation: "failed"`, which the
//   UI shows as `login_automation_failed` and continues as the relay.
// - `loginProgress`: the flow's state from the original request and its follow-up: the newest verified response of
//   either (follow-up last) decides; a hub state that ended the active request without an answer is `timeout`
//   (`expired`) or `request_failed` (`failed`).
// - `startCheck`: `credential.check {kind}` (the API key or the subscription) or `mailbox.check {}`; `checkProgress`
//   reads `done {valid}` or a failure.
import { REQUEST_FAILED, type ResponseBody } from '../requests/bodies';
import {
  type OpenedResponse,
  type PreparedRequest,
  prepareRequest,
  type RequestsContext,
  type RequestView,
  submitRequest,
} from '../requests/client';

export type LoginMode = 'manual' | 'automatic';

/** Client code of a failed mailbox automation (03-api.md section 11). */
export const AUTOMATION_FAILED = 'login_automation_failed';

/** Seals and posts `claude.login` to `device`. */
export async function startLogin(context: RequestsContext, device: string, mode: LoginMode): Promise<PreparedRequest> {
  const prepared = await prepareRequest(context, { device, kind: 'claude.login', body: { mode } });
  await submitRequest(context, prepared);
  return prepared;
}

/** Seals and posts the authorization code as the follow-up of `original`. */
export async function sendCode(
  context: RequestsContext,
  original: PreparedRequest,
  code: string,
): Promise<PreparedRequest> {
  const prepared = await prepareRequest(context, {
    device: original.device.device,
    kind: 'claude.login',
    body: { code: code.trim() },
    follows: original,
  });
  await submitRequest(context, prepared);
  return prepared;
}

export type LoginProgress =
  | { readonly step: 'waiting' }
  | { readonly step: 'progress'; readonly stage: string }
  | { readonly step: 'authorize'; readonly url: string; readonly automationFailed: boolean }
  | { readonly step: 'done'; readonly email: string | null }
  | { readonly step: 'failed'; readonly code: string };

type Verified = Extract<OpenedResponse, { status: 'verified' }>;

const verified = (view: RequestView | null): Verified[] =>
  view === null ? [] : view.responses.filter((item): item is Verified => item.status === 'verified');

/** The failure of a hub state that ended a request without a final answer, else null. */
function ended(view: RequestView): LoginProgress | null {
  if (view.state === 'expired') return { step: 'failed', code: 'timeout' };
  if (view.state === 'failed') return { step: 'failed', code: REQUEST_FAILED };
  return null;
}

function fromBody(body: ResponseBody): LoginProgress | null {
  switch (body.step) {
    case 'done':
      return { step: 'done', email: body.email ?? null };
    case 'failed':
      return { step: 'failed', code: body.code };
    case 'authorize':
      return { step: 'authorize', url: body.url, automationFailed: body.automation === 'failed' };
    case 'progress':
      return { step: 'progress', stage: body.stage };
    default:
      return null;
  }
}

/** The state of a login flow (module comment). */
export function loginProgress(original: RequestView, followUp: RequestView | null): LoginProgress {
  const last = [...verified(original), ...verified(followUp)].at(-1);
  const fromLast = last === undefined ? null : fromBody(last.body);
  if (fromLast !== null && (fromLast.step === 'done' || fromLast.step === 'failed')) return fromLast;
  const stopped = ended(followUp ?? original);
  if (stopped !== null) return stopped;
  return fromLast ?? { step: 'waiting' };
}

// --- checks ---

export type CheckKind = 'anthropic_api_key' | 'claude_subscription' | 'mailbox';

/** Seals and posts the check of the stored `kind` credential to `device`. */
export async function startCheck(context: RequestsContext, device: string, kind: CheckKind): Promise<PreparedRequest> {
  const prepared =
    kind === 'mailbox'
      ? await prepareRequest(context, { device, kind: 'mailbox.check', body: {} })
      : await prepareRequest(context, { device, kind: 'credential.check', body: { kind } });
  await submitRequest(context, prepared);
  return prepared;
}

export type CheckProgress =
  | { readonly step: 'waiting' }
  | { readonly step: 'done'; readonly valid: boolean }
  | { readonly step: 'failed'; readonly code: string };

/** The state of a check from its request view. */
export function checkProgress(view: RequestView): CheckProgress {
  const body = verified(view).at(-1)?.body;
  if (body?.step === 'done') return { step: 'done', valid: body.valid === true };
  if (body?.step === 'failed') return { step: 'failed', code: body.code };
  const stopped = ended(view);
  return stopped === null ? { step: 'waiting' } : (stopped as CheckProgress);
}
