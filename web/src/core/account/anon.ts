// Anonymous hub requests of the sign-in flows (03-api.md section 4.1: `anon` endpoints such as
// `POST /v1/invites/lookup`, `POST /v1/passkeys/register/begin|finish`, `POST /v1/passkeys/login/begin|finish`).
// They carry no session key, so the hub does not sign the answers (03-api.md section 1); nothing they return is
// trusted beyond what the client checks itself (options, statement, bundle decryption, verified logs). Errors map onto
// the error catalog exactly like signed requests (`errorOf`).
import { utf8 } from '../encoding/bytes';
import { errorOf, HubError, parseBody } from '../net/signedFetch';

/** A JSON request without credentials; resolves with the parsed 2xx body, rejects with `HubError`. */
export type AnonRequest = (method: string, target: string, body?: unknown) => Promise<unknown>;

/** `AnonRequest` on `fetch` (same-origin, no cookies, no cache, no redirects). */
export function anonClient(fetch: typeof globalThis.fetch): AnonRequest {
  return async (method, target, body) => {
    const init: RequestInit = {
      method,
      headers: { accept: 'application/json' },
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
    };
    if (body !== undefined) {
      init.headers = { accept: 'application/json', 'content-type': 'application/json' };
      init.body = utf8(JSON.stringify(body));
    }
    let raw: Uint8Array;
    let response: Response;
    try {
      response = await fetch(target, init);
      raw = new Uint8Array(await response.arrayBuffer());
    } catch {
      throw new HubError('unavailable', 0);
    }
    const parsed = parseBody(raw);
    if (!response.ok) throw errorOf(response.status, parsed);
    return parsed;
  };
}
