// The user's AI agent of the Playwright suite (11-agent-access.md sections 4 and 5.1): fresh keys, the pairing at the
// hub with its proof of possession, the `/connect` link, the claim with the handover secret of the loopback form, and
// requests signed with `agent-request-v1` under `agent.<32 hex>` (answers are not response-checked: the test's own
// side). The loopback listener is replaced by a route of the page (`catchHandover`). A pairing from another computer
// (ADR-067, `pair(origin, { remote: true })`) works like the agent tool: the hub's nonce gives the agent code
// (`agentCode`), the agent fetches the sealed handover (`GET /v1/agent/handover`), checks the return code the user
// passes back and only then claims (`finishRemote`).
import type { Page, Request } from '@playwright/test';
import {
  agentCode,
  claim,
  claimHash,
  fingerprint,
  openHandover,
  parseCode,
  RETURN_CODE_LENGTH,
  returnCode,
  sealIdentity,
  sealLabel,
} from '../../src/core/crypto/agent';
import { ed25519Sign, Identity } from '../../src/core/crypto/keys';
import { random } from '../../src/core/crypto/subtle';
import { b64uDecode, b64uEncode } from '../../src/core/encoding/b64u';
import { utf8 } from '../../src/core/encoding/bytes';
import { canonical } from '../../src/core/encoding/json';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatSeconds } from '../../src/core/encoding/time';
import { concat } from '../../src/core/encoding/bytes';
import { label, LABEL_AGENT_PAIR } from '../../src/core/crypto/labels';
import { requestText, sha256Hex } from '../../src/core/net/responseCheck';

export const LOOPBACK_PORT = 40123;

export class TestAgent {
  private constructor(
    readonly origin: string,
    readonly id: string,
    readonly keys: Identity,
    /** The hub's nonce of a remote pairing (b64u), else null. */
    readonly nonce: string | null = null,
  ) {}

  /** Fresh keys and an open pairing at the hub (`remote`: a pairing from another computer, ADR-067). */
  static async pair(origin: string, options: { readonly remote?: boolean } = {}): Promise<TestAgent> {
    const keys = await Identity.generate();
    const id = uuid7();
    const pub = keys.public();
    const text = concat(label(LABEL_AGENT_PAIR), canonical({ kem: pub.kem, pairing_id: id, sig: pub.sig }));
    const pop = b64uEncode(await keys.sign(text));
    const response = await fetch(new URL('/v1/agent-pairings', origin), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ pairing_id: id, pub, pop, ...(options.remote && { remote: true }) }),
    });
    if (!response.ok) throw new Error(`pairing refused: ${await response.text()}`);
    const answer = (await response.json()) as { remote?: boolean; nonce?: string };
    if (options.remote && (answer.remote !== true || typeof answer.nonce !== 'string')) {
      throw new Error('the hub did not create a remote pairing');
    }
    return new TestAgent(origin, id, keys, answer.nonce ?? null);
  }

  /** The agent code of a remote pairing as the agent tool shows it (`XXXXX-XXXXX`). */
  async agentCode(): Promise<string> {
    const fp = await fingerprint(this.keys.kemPublic(), this.keys.sigPublic());
    const code = await agentCode(this.id, fp, b64uDecode(this.nonce, 16), new URL(this.origin).origin);
    return `${code.slice(0, 5)}-${code.slice(5)}`;
  }

  /**
   * Finishes a remote pairing like `pair --wait --return-code`: fetches and opens the handover, refuses a return code
   * that does not match (`pairing_failed`, nothing claimed), else claims; resolves with the claim's answer.
   */
  async finishRemote(typed: string): Promise<{ status: number; body: unknown }> {
    const fetched = await this.request('GET', '/v1/agent/handover');
    if (fetched.status !== 200) return fetched;
    const { user_id: userId, handover_ct: sealed } = fetched.body as { user_id: string; handover_ct: string };
    const handover = await openHandover(this.keys.kem, this.id, userId, sealed);
    if (parseCode(typed, RETURN_CODE_LENGTH) !== (await returnCode(handover.secret, this.id, userId))) {
      return { status: 0, body: 'pairing_failed' };
    }
    return this.claim(b64uEncode(handover.secret), userId);
  }

  /** The fragment of the `/connect` link (a wrong fingerprint on request). */
  async fragment(options: { readonly name?: string; readonly host?: string; readonly forged?: boolean } = {}) {
    const fp = options.forged
      ? b64uEncode(random(16))
      : await fingerprint(this.keys.kemPublic(), this.keys.sigPublic());
    const params = new URLSearchParams({
      p: this.id,
      f: fp,
      ...(this.nonce === null ? { c: String(LOOPBACK_PORT) } : { m: 'remote' }),
      a: options.name ?? 'Claude Code',
      h: options.host ?? 'Test-Mac',
    });
    return params.toString();
  }

  /** A request signed with the agent's key; resolves with status and parsed body. */
  async request(method: string, target: string, body?: unknown): Promise<{ status: number; body: unknown }> {
    const content = body === undefined ? new Uint8Array(0) : utf8(JSON.stringify(body));
    const keyId = `agent.${this.id.replaceAll('-', '')}`;
    const time = formatSeconds(Math.floor(Date.now() / 1000));
    const nonce = b64uEncode(random(16));
    const text = requestText(keyId, method, new URL(this.origin).host, target, time, nonce, await sha256Hex(content));
    const headers: Record<string, string> = {
      'agent-key': keyId,
      'agent-time': time,
      'agent-nonce': nonce,
      'agent-signature': b64uEncode(await ed25519Sign(this.keys.sig.privateKey, text)),
    };
    if (body !== undefined) headers['content-type'] = 'application/json';
    const response = await fetch(new URL(target, this.origin), {
      method,
      headers,
      ...(body !== undefined && { body: content }),
    });
    return { status: response.status, body: await response.json().catch(() => null) };
  }

  /**
   * This agent tries to confirm the pairing of `other` for `userId` with the user's `identity`; the hub refuses it
   * (only the user's passkey confirms a main orchestrator, concept idea 34).
   */
  async confirm(other: TestAgent, userId: string, identity: Identity, name: string) {
    const r = random(32);
    const body = {
      pairing_id: other.id,
      bundle_ct: await sealIdentity(other.keys.kemPublic(), r, other.id, userId, identity),
      label_ct: await sealLabel(identity.kemPublic(), other.id, userId, { name, host: 'Test-Mac' }),
      claim_hash: b64uEncode(await claimHash(await claim(r, other.id, userId))),
    };
    return this.request('POST', '/v1/me/agents', body);
  }

  /** `POST /v1/agent/claim` with the claim of the handover secret `r` (b64u) for `userId`. */
  async claim(r: string, userId: string): Promise<{ status: number; body: unknown }> {
    const raw = await claim(b64uDecode(r, 32), this.id, userId);
    return this.request('POST', '/v1/agent/claim', { claim: b64uEncode(raw) });
  }
}

export interface Handover {
  readonly method: string;
  readonly fields: URLSearchParams;
  readonly navigation: boolean;
}

/**
 * Stands in for the agent's loopback listener: the next request to `http://127.0.0.1:<port>/agent-pairing` is
 * answered like the agent tool does (`303` to `<origin>/settings?agent=connected`); resolves with what was sent.
 */
export function catchHandover(page: Page, origin: string, before?: (handover: Handover) => Promise<void>) {
  return new Promise<Handover>((resolve) => {
    void page.route(`http://127.0.0.1:${LOOPBACK_PORT}/**`, async (route) => {
      const request: Request = route.request();
      const handover = {
        method: request.method(),
        fields: new URLSearchParams(request.postData() ?? ''),
        navigation: request.isNavigationRequest(),
      };
      await before?.(handover);
      await route.fulfill({ status: 303, headers: { location: `${origin}/settings?agent=connected` } });
      resolve(handover);
    });
  });
}
