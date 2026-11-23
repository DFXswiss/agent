// Signed requests of the browser (03-api.md section 1, 06-web.md section 2.6): every non-anonymous request carries
// `Agent-Key`, `Agent-Time`, `Agent-Nonce` and `Agent-Signature` = P-256 raw `r || s` of the session key over
// `agent-request-v1`; every response is checked against the hub key of `/v1/config` before its body is used
// (`responseCheck.ts`). Hub errors are mapped onto the error catalog (`HubError.code`). Less than five minutes before
// the session expires, and on `401 session_expired`, the client reports a session notice (the UI shows the
// non-blocking "Sign in again" prompt; WEB-03).
import { b64uEncode } from '../encoding/b64u';
import { fromUtf8, utf8 } from '../encoding/bytes';
import { formatSeconds } from '../encoding/time';
import { p256SignRaw } from '../crypto/keys';
import { random } from '../crypto/subtle';
import errorCatalog from '../i18n/errors.en.json';
import { checkResponse, type HubKey, requestText, type SentRequest, sha256Hex } from './responseCheck';

export const NONCE_BYTES = 16;
/** The renewal prompt appears this long before `expires_at` (06-web.md section 2.6). */
export const RENEW_BEFORE_MS = 5 * 60 * 1000;

/** The certified session key of the signed-in browser. */
export interface Session {
  /** `session.<32 hex>`. */
  readonly keyId: string;
  /** Non-extractable P-256 private key. */
  readonly privateKey: CryptoKey;
  /** `expires_at` of the certificate (Unix ms). */
  readonly expiresAt: number;
}

export type SessionNotice = 'expiring' | 'expired';

const KNOWN_CODES: ReadonlySet<string> = new Set(Object.keys(errorCatalog));

/**
 * Statuses the hub answers before its signature gate, hence unsigned (`agent_hub.http.pipeline`: malformed request
 * or body, failed gate, rate limit), and 5xx of anything in front of the hub. Every other answer to a signed request
 * is signed by the hub, so an unsigned 403/404/409 is forged (it would drop a cursor or stop the channel).
 */
export function unsignedAllowed(status: number): boolean {
  return status === 400 || status === 401 || status === 413 || status === 429 || status >= 500;
}

/** The code a status maps to when the body names no known code. */
function statusCode(status: number): string {
  if (status === 401) return 'unauthorized';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 409) return 'id_conflict';
  if (status === 413) return 'size_exceeded';
  if (status === 429) return 'rate_limited';
  if (status >= 500 || status === 0) return 'unavailable';
  return 'request_invalid';
}

/** A refused request: the catalog code, the HTTP status (0 without a response) and the hub's `detail`. */
export class HubError extends Error {
  readonly code: string;
  readonly status: number;
  readonly detail: unknown;

  constructor(code: string, status: number, detail: unknown = null) {
    super(code);
    this.name = 'HubError';
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

/** Parses JSON text; null for anything that is not JSON. */
export function parseBody(raw: Uint8Array): unknown {
  try {
    return JSON.parse(fromUtf8(raw)) as unknown;
  } catch {
    return null;
  }
}

/** The error of a non-2xx answer: a known catalog code from `{"error"}`, otherwise the code of the status. */
export function errorOf(status: number, body: unknown): HubError {
  const data = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {};
  const code = typeof data.error === 'string' && KNOWN_CODES.has(data.error) ? data.error : statusCode(status);
  return new HubError(code, status, data.detail ?? null);
}

export interface ClientOptions {
  /** `fetch` of the page (or a test double). */
  readonly fetch: typeof globalThis.fetch;
  /** The hub's host line: host and non-default port of `/v1/config.origin`. */
  readonly host: string;
  readonly hubKey: HubKey;
  /** The current session, null when signed out. */
  readonly session: () => Session | null;
  readonly now?: () => number;
  readonly onSession?: (notice: SessionNotice) => void;
}

export interface RequestOptions {
  readonly body?: unknown;
  readonly signal?: AbortSignal;
  readonly headers?: Readonly<Record<string, string>>;
}

/** The host line of `agent-request-v1` for a hub origin (`https://hub.example` → `hub.example`). */
export function hostOf(origin: string): string {
  return new URL(origin).host;
}

/** A client that signs every request with the session key and verifies every answer. */
export class SignedClient {
  readonly #options: ClientOptions;
  readonly #now: () => number;

  constructor(options: ClientOptions) {
    this.#options = options;
    this.#now = options.now ?? Date.now;
  }

  /** Reports `expiring` if the session ends within `RENEW_BEFORE_MS`; returns the session. */
  checkSession(): Session {
    const session = this.#options.session();
    if (session === null) throw new HubError('unauthorized', 401);
    if (session.expiresAt - this.#now() < RENEW_BEFORE_MS) this.#notify('expiring');
    return session;
  }

  #notify(notice: SessionNotice): void {
    this.#options.onSession?.(notice);
  }

  async #send(method: string, target: string, options: RequestOptions, accept: string) {
    const session = this.checkSession();
    const body = options.body === undefined ? new Uint8Array(0) : utf8(JSON.stringify(options.body));
    const sent: SentRequest = {
      keyId: session.keyId,
      nonce: b64uEncode(random(NONCE_BYTES)),
      agentTime: formatSeconds(Math.floor(this.#now() / 1000)),
    };
    const text = requestText(
      sent.keyId,
      method,
      this.#options.host,
      target,
      sent.agentTime,
      sent.nonce,
      await sha256Hex(body),
    );
    const signature = b64uEncode(await p256SignRaw(session.privateKey, text));
    const headers: Record<string, string> = {
      accept,
      ...options.headers,
      'agent-key': sent.keyId,
      'agent-time': sent.agentTime,
      'agent-nonce': sent.nonce,
      'agent-signature': signature,
    };
    const init: RequestInit = { method, headers, credentials: 'omit', cache: 'no-store', redirect: 'error' };
    if (options.body !== undefined) {
      headers['content-type'] = 'application/json';
      init.body = body;
    }
    if (options.signal !== undefined) init.signal = options.signal;
    let response: Response;
    try {
      response = await this.#options.fetch(target, init);
    } catch (error) {
      if (options.signal?.aborted === true) throw error;
      throw new HubError('unavailable', 0);
    }
    return { response, sent };
  }

  #fail(error: HubError): never {
    if (error.code === 'session_expired') this.#notify('expired');
    throw error;
  }

  /**
   * A signed JSON request. Resolves with the verified body of a 2xx answer; rejects with `HubError` for an error
   * answer (signed or, for refusals before the hub's signature gate, unsigned) and with `signature_invalid` /
   * `time_skew` for an answer whose signature does not verify (also an unsigned 2xx answer).
   */
  async request(method: string, target: string, options: RequestOptions = {}): Promise<unknown> {
    const { response, sent } = await this.#send(method, target, options, 'application/json');
    let raw: Uint8Array;
    try {
      raw = new Uint8Array(await response.arrayBuffer());
    } catch (error) {
      if (options.signal?.aborted === true) throw error;
      throw new HubError('unavailable', 0);
    }
    const refusal = await checkResponse(
      this.#options.hubKey,
      sent,
      response.status,
      response.headers,
      raw,
      this.#now(),
    );
    const body = parseBody(raw);
    if (refusal === 'unsigned' && unsignedAllowed(response.status)) this.#fail(errorOf(response.status, body));
    if (refusal !== null)
      throw new HubError(refusal === 'time_skew' ? 'time_skew' : 'signature_invalid', response.status);
    if (!response.ok) this.#fail(errorOf(response.status, body));
    return body;
  }

  /** `GET` with the signed request headers; the stream is not response-signed (03-api.md section 1). */
  async stream(target: string, signal: AbortSignal): Promise<Response> {
    const { response } = await this.#send('GET', target, { signal }, 'text/event-stream');
    if (!response.ok) {
      const raw = new Uint8Array(await response.arrayBuffer().catch(() => new ArrayBuffer(0)));
      this.#fail(errorOf(response.status, parseBody(raw)));
    }
    return response;
  }
}
