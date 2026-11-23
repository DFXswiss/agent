// Error codes of the crypto layer (04-crypto.md section 8), the TypeScript twin of `agent_hub.crypto.errors`.
// Every refusal of src/core/{encoding,crypto,passkey} is a `CryptoError` whose `code` is one of these closed lists;
// the message is the code itself and never carries key material, plaintext or input data.

/** 04-crypto.md section 8, "Crypto/format". */
export const CRYPTO_CODES = [
  'non_ascii',
  'header_invalid',
  'signature_invalid',
  'decrypt_failed',
  'prf_length',
  'prf_missing',
  'recovery_code_length',
  'hpke_short',
  'invite_code_length',
  'invite_code_invalid',
  'event_conflict',
  'epoch_stale',
  'kind_not_allowed',
  'size_exceeded',
  'wrap_unauthorized',
  'platform_root_mismatch',
] as const;

/** 04-crypto.md section 8, "Log" (raised by src/core/log, which builds on this package). */
export const LOG_CODES = [
  'seq_conflict',
  'log_empty',
  'log_chain',
  'log_genesis',
  'log_accept',
  'log_signature',
  'log_rights',
  'log_epoch',
  'log_type',
  'log_rollback',
  'log_fork',
  'invite_not_in_log',
  'invite_consumed',
  'invite_expired',
  'invite_revoked',
  'author_not_writer',
  'last_admin',
  'rotation_required',
] as const;

export type CryptoCode = (typeof CRYPTO_CODES)[number];
export type LogCode = (typeof LOG_CODES)[number];
export type ErrorCode = CryptoCode | LogCode;

/** A refusal with a code of 04-crypto.md section 8; `message` is the code. */
export class CryptoError extends Error {
  readonly code: ErrorCode;

  constructor(code: ErrorCode) {
    super(code);
    this.name = 'CryptoError';
    this.code = code;
  }
}

/** Throws `CryptoError(code)`. */
export function fail(code: ErrorCode): never {
  throw new CryptoError(code);
}
