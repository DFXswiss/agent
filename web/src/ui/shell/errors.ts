// The catalog code of anything a flow throws: protocol errors carry their code, everything else is `unavailable`
// (network, unexpected) – the screens show the catalog text, never an exception message.
import { CryptoError } from '../../core/crypto/errors';
import { HubError } from '../../core/net/signedFetch';

export function errorCode(error: unknown): string {
  if (error instanceof CryptoError || error instanceof HubError) return error.code;
  return 'unavailable';
}
