// The protocol prefix and the labels of 04-crypto.md section 3 (as bytes, without and with the prefix P).
import { concat, utf8 } from '../encoding/bytes';

/** `P = "agent.dfx.swiss/v1/"`: a protocol constant, not a deployment host name. */
export const PREFIX = 'agent.dfx.swiss/v1/';
export const P = utf8(PREFIX);

/** `P || label` as bytes; `label` may contain `\0`. */
export function label(text: string): Uint8Array<ArrayBuffer> {
  return concat(P, utf8(text));
}

export const PRF_INPUT_LABEL = 'agent.dfx.swiss/prf/v1';

// Info labels of the derivations (section 3), without the prefix P.
export const KEK = 'kek\0';
export const RECOVERY_KEK = 'recovery-kek';
export const RECOVERY_ID = 'recovery-id';
export const INVITE_ID = 'invite-id';
export const INVITE_PACKAGE = 'invite-package';
export const INVITE_SIGN = 'invite-sign';
export const EVENT = 'event';
export const DEDUPE = 'dedupe';
export const CREDENTIAL = 'credential';
export const RUNNER_STORAGE = 'runner-storage';

// Signature/AAD labels (section 3), without the prefix P.
export const LABEL_BUNDLE = 'bundle\0';
export const LABEL_WRAP = 'wrap\0';
export const LABEL_EVENT_SIG = 'event-sig\0';
export const LABEL_LOG = 'log\0';
export const LABEL_PLATFORM_LOG = 'platform-log\0';
export const LABEL_INVITE_PACKAGE = 'invite-package\0';
export const LABEL_CREDENTIAL_SIG = 'credential-sig\0';
export const LABEL_REQUEST_WRAP = 'request-wrap\0';
export const LABEL_REQUEST_SIG = 'request-sig\0';
export const LABEL_DEDUPE = 'dedupe\0';
export const LABEL_RECOVERY_PROOF = 'recovery-proof\0';
export const LABEL_ENROLL = 'enroll\0';
export const LABEL_INVITE_REGISTER = 'invite-register\0';
