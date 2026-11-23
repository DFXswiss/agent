// A simulated hub for the account-flow tests: the anonymous sign-in endpoints of 03-api.md section 4.1 and the
// signed endpoints the flows use, in memory. Signed requests are checked like `agent_hub.auth.gate` (agent-request-v1,
// P-256 raw signature of the session key certified at login/finish) and every answer to them is signed with
// agent-response-v1 under the fixture hub key, so the real `SignedClient` accepts it. WebAuthn attestations and
// assertions are not verified (the hub's own tests do that); the statement is parsed to learn the session key.
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { fromUtf8, utf8 } from '../../../src/core/encoding/bytes';
import { uuid7 } from '../../../src/core/encoding/ids';
import { formatSeconds, formatTime } from '../../../src/core/encoding/time';
import { ed25519KeyPair, ed25519Sign, p256VerifyRaw } from '../../../src/core/crypto/keys';
import { prfInput } from '../../../src/core/crypto/kdf';
import { verifyProof } from '../../../src/core/crypto/recovery';
import { formatCode, generateCode, inviteSecrets, parseCode, verifyRegister } from '../../../src/core/crypto/invite';
import { codeToInviteId } from '../../../src/core/account/register';
import { random, sha256 } from '../../../src/core/crypto/subtle';
import { entryHash } from '../../../src/core/log/entries';
import { requestText, responseText, sha256Hex } from '../../../src/core/net/responseCheck';
import { sessionKeyId } from '../../../src/core/passkey/statement';
import fixture from '../../fixtures/net/signing.json';

export const ORIGIN = 'https://hub.example:8443';
export const HOST = 'hub.example:8443';
export const T0 = Date.UTC(2026, 10, 21, 12, 0, 0);

export const CONFIG = {
  rp_id: 'hub.example',
  origin: ORIGIN,
  platform_root_key: 'A'.repeat(43),
  hub_key: { id: fixture.hub_key.id, public_key: fixture.hub_key.public_key },
  version: '0.1.0',
  limits: {},
  models: { platform: [], api_key: [], subscription: [] },
  defaults: {},
};

const hubKeyPair = async () => ed25519KeyPair(await sha256(utf8('agent.dfx.swiss web-02 test hub key')));

export interface SimUser {
  id: string;
  identity: { kem_pub: string; sig_pub: string } | null;
  recoveryId: string | null;
  bundles: Map<string, string>;
  credentials: Set<string>;
  prfSeen: Set<string>;
  settings: { model_access: string; model_id: string | null; locale: string };
  orgs: string[];
  /** A recovery registered a new passkey: the recovery slot may be replaced once (HUB-03 decision 19). */
  recoveryRotation?: boolean;
}

export interface SimOrg {
  id: string;
  defaultWg: string;
  owner: string;
  entries: unknown[];
  wraps: Record<string, unknown>[];
  events: unknown[];
}

type Handler = (body: unknown, user: SimUser | null, target: string, key: string | null) => unknown;

export class HubFailure {
  constructor(
    readonly status: number,
    readonly error: string,
  ) {}
}

export class SimHub {
  readonly invites = new Map<
    string,
    { scope: string; kind: string; registered: string | null; used: boolean; pkInv?: string }
  >();
  readonly #pending: { code: string; scope: string; kind: string }[] = [];
  #settled: Promise<void> = Promise.resolve();
  readonly users = new Map<string, SimUser>();
  readonly orgs = new Map<string, SimOrg>();
  readonly sessions = new Map<string, { user: string; point: Uint8Array; ended: boolean; credential?: string }>();
  readonly ceremonies = new Map<string, { kind: string; user?: string; hubValue?: string; challenge?: string }>();
  /** `recovery/begin` answers `429 rate_limited` with this `Retry-After` (seconds; '' for none) while set. */
  retryAfter: string | null = null;
  readonly credentialUser = new Map<string, string>();
  readonly log: { method: string; target: string; body: string; signed: boolean }[] = [];
  /** Answer a path with a failure instead (`METHOD target` → failure or answer override). */
  readonly override = new Map<string, HubFailure | ((body: unknown) => unknown)>();
  now = T0;

  /** A new invitation; answers its code text (what the link carries). Registration needs `inv_sig` (ADR-043). */
  addInvite(scope: 'platform' | 'org' = 'platform', kind = 'user'): string {
    const code = formatCode(generateCode());
    this.#pending.push({ code, scope, kind });
    return code;
  }

  /** The `invite_id` of a code text. */
  async idOf(code: string): Promise<string> {
    return codeToInviteId(code);
  }

  /** A new invitation; answers its `invite_id`. */
  async addInviteId(scope: 'platform' | 'org' = 'platform', kind = 'user'): Promise<string> {
    return this.idOf(this.addInvite(scope, kind));
  }

  /**
   * Invitations of `addInvite` become known by their `invite_id` (deriving needs WebCrypto, hence lazily). Requests
   * run concurrently (the live app polls), so every caller waits for the derivations queued before it.
   */
  settle(): Promise<void> {
    this.#settled = this.#settled.then(async () => {
      for (const { code, scope, kind } of this.#pending.splice(0)) {
        const secrets = await inviteSecrets(parseCode(code));
        this.invites.set(secrets.inviteId, { scope, kind, registered: null, used: false, pkInv: secrets.pkInv });
      }
    });
    return this.#settled;
  }

  readonly fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const target = String(input);
    const method = init?.method ?? 'GET';
    const headers = Object.fromEntries(new Headers(init?.headers).entries());
    const raw = init?.body === undefined ? new Uint8Array(0) : new Uint8Array(init.body as Uint8Array);
    const signed = headers['agent-key'] !== undefined;
    this.log.push({ method, target, body: fromUtf8(raw), signed });
    await this.settle();
    if (this.retryAfter !== null && target === '/v1/recovery/begin') {
      const limited = new Headers({ 'content-type': 'application/json' });
      if (this.retryAfter !== '') limited.set('retry-after', this.retryAfter);
      return new Response(JSON.stringify({ error: 'rate_limited' }), { status: 429, headers: limited });
    }
    const body = raw.length === 0 ? undefined : (JSON.parse(fromUtf8(raw)) as unknown);
    let user: SimUser | null = null;
    if (signed) {
      const session = this.sessions.get(headers['agent-key']!);
      if (session === undefined || session.ended) return this.#answer(401, { error: 'key_unknown' }, null);
      const text = requestText(
        headers['agent-key']!,
        method,
        HOST,
        target,
        headers['agent-time']!,
        headers['agent-nonce']!,
        await sha256Hex(raw),
      );
      await p256VerifyRaw(session.point, b64uDecode(headers['agent-signature']), text);
      user = this.users.get(session.user)!;
    }
    const key = `${method} ${target.split('?')[0]}`;
    const override = this.override.get(key);
    let result: unknown;
    try {
      if (override instanceof HubFailure) throw override;
      result =
        override !== undefined
          ? await override(body)
          : await this.#route(method, target, body, user, signed ? headers['agent-key']! : null);
    } catch (error) {
      if (!(error instanceof HubFailure)) throw error;
      return this.#answer(error.status, { error: error.error }, signed ? headers : null);
    }
    return this.#answer(200, result, signed ? headers : null);
  };

  async #answer(status: number, body: unknown, request: Record<string, string> | null): Promise<Response> {
    const raw = utf8(JSON.stringify(body));
    const out = new Headers({ 'content-type': 'application/json' });
    if (request !== null) {
      const hub = await hubKeyPair();
      const hubTime = formatSeconds(Math.floor(this.now / 1000));
      const sent = { keyId: request['agent-key']!, nonce: request['agent-nonce']!, agentTime: request['agent-time']! };
      const signature = await ed25519Sign(hub.privateKey, responseText(sent, status, hubTime, await sha256Hex(raw)));
      out.set('agent-hub-key', fixture.hub_key.id);
      out.set('agent-hub-time', hubTime);
      out.set('agent-hub-signature', b64uEncode(signature));
    }
    return new Response(raw, { status, headers: out });
  }

  async #route(
    method: string,
    target: string,
    body: unknown,
    user: SimUser | null,
    key: string | null,
  ): Promise<unknown> {
    const path = target.split('?')[0]!;
    const data = (body ?? {}) as Record<string, unknown>;
    if (method === 'POST' && path === '/v1/invites/lookup') return this.#lookup(String(data.invite_id));
    if (method === 'POST' && path === '/v1/passkeys/register/begin') return this.#registerBegin(data, user);
    if (method === 'POST' && path === '/v1/passkeys/register/finish') return this.#registerFinish(data);
    if (method === 'POST' && path === '/v1/passkeys/login/begin') return this.#loginBegin();
    if (method === 'POST' && path === '/v1/passkeys/login/finish') return this.#loginFinish(data);
    if (method === 'POST' && path === '/v1/recovery/begin') return this.#recoveryBegin(data);
    if (method === 'POST' && path === '/v1/recovery/finish') return this.#recoveryFinish(data);
    if (user === null) throw new HubFailure(401, 'unauthorized');
    const handler = this.#signed(method, path);
    return handler(body, user, target, key);
  }

  #signed(method: string, path: string): Handler {
    if (method === 'GET' && path === '/v1/me') return (_b, user) => this.me(user!);
    if (method === 'GET' && path === '/v1/me/bundles/recovery')
      return (_b, user) => ({ bundle_ct: user!.bundles.get('recovery') });
    if (method === 'PUT' && path.startsWith('/v1/me/bundles/passkey:')) {
      return (body, user) => {
        const slot = path.slice('/v1/me/bundles/'.length);
        user!.bundles.set(slot, String((body as Record<string, unknown>).bundle_ct));
        user!.prfSeen.add(slot.slice('passkey:'.length));
        return {};
      };
    }
    if (method === 'POST' && path === '/v1/me/recovery/begin') {
      return async (_b, user) => {
        const ceremony = uuid7(this.now);
        const challenge = b64uEncode(random(32));
        this.ceremonies.set(ceremony, { kind: 'rotate', user: user!.id, challenge });
        return { ceremony_id: ceremony, challenge, options: await this.#requestOptions() };
      };
    }
    if (method === 'PUT' && path === '/v1/me/bundles/recovery') {
      return (body, user) => {
        const data = body as { bundle_ct: string; new_recovery_id: string; assertion?: Record<string, string> };
        if (data.assertion === undefined) {
          if (user!.recoveryRotation !== true) throw new HubFailure(403, 'forbidden');
        } else {
          // KOR-01: an assertion of this user's passkey for its own ceremony (challenge and signature are checked
          // by the hub's tests; the fake authenticator signs nothing)
          const ceremony = this.ceremonies.get(data.assertion.ceremony_id!);
          this.ceremonies.delete(data.assertion.ceremony_id!);
          if (ceremony?.kind !== 'rotate' || ceremony.user !== user!.id) throw new HubFailure(400, 'ceremony_invalid');
          if (!user!.credentials.has(data.assertion.credential_id!)) throw new HubFailure(400, 'credential_invalid');
        }
        user!.recoveryRotation = false;
        user!.recoveryId = data.new_recovery_id;
        user!.bundles.set('recovery', data.bundle_ct);
        return {};
      };
    }
    if (method === 'DELETE' && path === '/v1/me/registration') return (_b, user) => this.#abandon(user!);
    const passkey = /^\/v1\/passkeys\/([^/]+)$/.exec(path);
    if (method === 'DELETE' && passkey) return (_b, user) => this.#removePasskey(user!, passkey[1]!);
    if (method === 'PUT' && path === '/v1/me/settings') {
      return (body, user) => {
        user!.settings = body as SimUser['settings'];
        return user!.settings;
      };
    }
    if (method === 'POST' && path === '/v1/session/end') {
      // only the caller's session, like `agent_hub.auth.sessions.end_session`
      return (_b, _user, _target, key) => {
        this.sessions.get(key!)!.ended = true;
        return {};
      };
    }
    if (method === 'POST' && path === '/v1/orgs') return (body, user) => this.#createOrg(body, user!);
    if (method === 'GET' && path === '/v1/platform/log') return () => ({ entries: [], head: null, next_after: null });
    if (method === 'GET' && path === '/v1/wraps') {
      return (_b, user) => ({
        wraps: [...this.orgs.values()].flatMap((org) => org.wraps.filter((wrap) => wrap.recipient_id === user!.id)),
      });
    }
    const orgLog = /^\/v1\/orgs\/([^/]+)\/log$/.exec(path);
    if (method === 'GET' && orgLog) return () => this.#orgLog(orgLog[1]!);
    throw new HubFailure(404, 'not_found');
  }

  me(user: SimUser): Record<string, unknown> {
    return {
      user_id: user.id,
      identity: user.identity,
      settings: user.settings,
      orgs: user.orgs.map((id) => {
        const org = this.orgs.get(id)!;
        return { org_id: id, role: 'admin', private: true, default_wg: org.defaultWg, log_head: { seq: 0, hash: 'x' } };
      }),
      platform_roles: [],
      unread_notifications: 0,
      passkeys: [...user.credentials].reverse().map((id) => ({
        credential_id: id,
        created_at: formatTime(T0),
        last_used_at: null,
        prf_seen: user.prfSeen.has(id),
      })),
    };
  }

  /** `DELETE /v1/me/registration` (KOR-22): an unfinished registration (unused invitation, no org) is withdrawn. */
  #abandon(user: SimUser): unknown {
    const invite = [...this.invites.values()].find((item) => item.registered === user.id && !item.used);
    if (invite === undefined || user.orgs.length > 0) throw new HubFailure(403, 'forbidden');
    invite.registered = null;
    this.users.delete(user.id);
    for (const id of user.credentials) this.credentialUser.delete(id);
    for (const session of this.sessions.values()) if (session.user === user.id) session.ended = true;
    return {};
  }

  #removePasskey(user: SimUser, id: string): unknown {
    if (!user.credentials.has(id)) throw new HubFailure(404, 'not_found');
    if (user.credentials.size === 1) throw new HubFailure(403, 'last_passkey');
    user.credentials.delete(id);
    user.bundles.delete(`passkey:${id}`);
    this.credentialUser.delete(id);
    for (const session of this.sessions.values()) if (session.credential === id) session.ended = true;
    return {};
  }

  async #creationOptions(user: string, exclude: Iterable<string> = []): Promise<Record<string, unknown>> {
    const handle = b64uEncode(
      Uint8Array.from(
        user
          .replace(/-/g, '')
          .match(/../g)!
          .map((h) => parseInt(h, 16)),
      ),
    );
    return {
      rp: { id: 'hub.example', name: 'agent.dfx.swiss' },
      user: { id: handle, name: user, displayName: user },
      challenge: b64uEncode(random(32)),
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
      authenticatorSelection: { residentKey: 'required', userVerification: 'required', requireResidentKey: true },
      extensions: { prf: { eval: { first: b64uEncode(await prfInput()) } } },
      timeout: 300000,
      attestation: 'none',
      excludeCredentials: [...exclude].map((id) => ({ type: 'public-key', id })),
    };
  }

  async #recoveryBegin(data: Record<string, unknown>): Promise<unknown> {
    const user = [...this.users.values()].find((item) => item.recoveryId === data.recovery_id);
    if (user === undefined) throw new HubFailure(404, 'not_found');
    const challenge = b64uEncode(random(32));
    this.ceremonies.set(uuid7(this.now), { kind: 'proof', user: user.id, hubValue: challenge });
    return { user_id: user.id, bundle_ct: user.bundles.get('recovery'), challenge };
  }

  async #recoveryFinish(data: Record<string, unknown>): Promise<unknown> {
    const found = [...this.ceremonies].find(
      ([, item]) => item.kind === 'proof' && item.user === data.user_id && item.hubValue === data.challenge,
    );
    if (found === undefined) throw new HubFailure(400, 'ceremony_invalid');
    this.ceremonies.delete(found[0]);
    const user = this.users.get(String(data.user_id))!;
    try {
      await verifyProof(b64uDecode(user.identity!.sig_pub), user.id, b64uDecode(data.challenge), data.proof);
    } catch {
      throw new HubFailure(401, 'signature_invalid');
    }
    const ceremony = uuid7(this.now);
    this.ceremonies.set(ceremony, { kind: 'recovery', user: user.id });
    return { ceremony_id: ceremony, options: await this.#creationOptions(user.id, user.credentials) };
  }

  #lookup(id: string): unknown {
    const invite = this.invites.get(id);
    if (invite === undefined || invite.used) throw new HubFailure(404, 'invite_unknown');
    return {
      kind: invite.kind,
      scope: invite.scope,
      expires_at: formatTime(this.now + 3600_000),
      registered: invite.registered !== null,
    };
  }

  async #registerBegin(data: Record<string, unknown>, signedIn: SimUser | null): Promise<unknown> {
    if (data.add === true) {
      if (signedIn === null) throw new HubFailure(401, 'unauthorized');
      const ceremony = uuid7(this.now);
      this.ceremonies.set(ceremony, { kind: 'add', user: signedIn.id });
      const options = await this.#creationOptions(signedIn.id, signedIn.credentials);
      return { ceremony_id: ceremony, user_id: signedIn.id, options };
    }
    const invite = this.invites.get(String(data.invite_id));
    if (invite === undefined || invite.used || invite.registered !== null) throw new HubFailure(404, 'invite_unknown');
    const user = uuid7(this.now);
    const ceremony = uuid7(this.now);
    const challenge = b64uEncode(random(32));
    this.ceremonies.set(ceremony, { kind: 'register', user, hubValue: String(data.invite_id), challenge });
    const handle = b64uEncode(
      Uint8Array.from(
        user
          .replace(/-/g, '')
          .match(/../g)!
          .map((h) => parseInt(h, 16)),
      ),
    );
    return {
      ceremony_id: ceremony,
      user_id: user,
      options: {
        rp: { id: 'hub.example', name: 'agent.dfx.swiss' },
        user: { id: handle, name: user, displayName: user },
        challenge,
        pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
        authenticatorSelection: { residentKey: 'required', userVerification: 'required', requireResidentKey: true },
        extensions: { prf: { eval: { first: b64uEncode(await prfInput()) } } },
        timeout: 300000,
        attestation: 'none',
        excludeCredentials: [],
      },
    };
  }

  async #registerFinish(data: Record<string, unknown>): Promise<unknown> {
    const ceremony = this.ceremonies.get(String(data.ceremony_id));
    const kinds = ['register', 'add', 'recovery'];
    if (ceremony === undefined || !kinds.includes(ceremony.kind)) throw new HubFailure(400, 'ceremony_invalid');
    this.ceremonies.delete(String(data.ceremony_id));
    const credential = data.credential as Record<string, unknown>;
    if (ceremony.kind !== 'register') {
      const further = this.users.get(ceremony.user!)!;
      further.credentials.add(String(credential.id));
      this.credentialUser.set(String(credential.id), further.id);
      if (ceremony.kind === 'recovery') further.recoveryRotation = true;
      return { user_id: further.id, credential_id: credential.id };
    }
    const identity = data.identity as { kem_pub: string; sig_pub: string };
    const recovery = data.recovery as { recovery_id: string; bundle_ct: string };
    const id = ceremony.user!;
    const invited = this.invites.get(ceremony.hubValue!)!;
    try {
      await verifyRegister(b64uDecode(invited.pkInv!), id, b64uDecode(ceremony.challenge!), data.inv_sig);
    } catch {
      throw new HubFailure(401, 'signature_invalid');
    }
    this.users.set(id, {
      id,
      identity,
      recoveryId: recovery.recovery_id,
      bundles: new Map([['recovery', recovery.bundle_ct]]),
      credentials: new Set([String(credential.id)]),
      prfSeen: new Set(),
      settings: { model_access: 'auto', model_id: null, locale: 'en' },
      orgs: [],
    });
    this.credentialUser.set(String(credential.id), id);
    invited.registered = id;
    return { user_id: id, credential_id: credential.id };
  }

  async #requestOptions(): Promise<unknown> {
    return {
      rpId: 'hub.example',
      allowCredentials: [],
      userVerification: 'required',
      extensions: { prf: { eval: { first: b64uEncode(await prfInput()) } } },
      timeout: 300000,
    };
  }

  async #loginBegin(): Promise<unknown> {
    const ceremony = uuid7(this.now);
    const hubValue = b64uEncode(random(32));
    this.ceremonies.set(ceremony, { kind: 'login', hubValue });
    return { ceremony_id: ceremony, hub_value: hubValue, options: await this.#requestOptions() };
  }

  async #loginFinish(data: Record<string, unknown>): Promise<unknown> {
    const ceremony = this.ceremonies.get(String(data.ceremony_id));
    if (ceremony === undefined || ceremony.kind !== 'login') throw new HubFailure(400, 'ceremony_invalid');
    this.ceremonies.delete(String(data.ceremony_id));
    const certificate = data.certificate as Record<string, string>;
    const statement = b64uDecode(certificate.statement);
    const lines = fromUtf8(statement).split('\n');
    if (lines[3] !== ceremony.hubValue) throw new HubFailure(400, 'challenge_mismatch');
    const userId = this.credentialUser.get(certificate.credential_id!);
    if (userId === undefined) throw new HubFailure(400, 'credential_invalid');
    const keyId = await sessionKeyId(statement);
    this.sessions.set(keyId, {
      user: userId,
      point: b64uDecode(lines[2]),
      ended: false,
      credential: certificate.credential_id!,
    });
    const user = this.users.get(userId)!;
    const slot = `passkey:${certificate.credential_id}`;
    const answer: Record<string, unknown> = {
      user_id: userId,
      key_id: keyId,
      expires_at: formatTime(this.now + 43080_000),
      prf_expected: user.prfSeen.has(certificate.credential_id!),
    };
    const bundle = user.bundles.get(slot);
    if (bundle !== undefined) answer.bundle_ct = bundle;
    return answer;
  }

  async #createOrg(body: unknown, user: SimUser): Promise<unknown> {
    const data = body as { genesis: { body: Record<string, unknown> }; wraps: Record<string, unknown>[]; events: [] };
    const genesis = data.genesis.body;
    const id = String(genesis.org);
    const existing = this.orgs.get(id);
    if (existing !== undefined) return { org_id: id, default_wg: existing.defaultWg, replayed: true };
    const org: SimOrg = {
      id,
      defaultWg: String(genesis.default_wg),
      owner: user.id,
      entries: [data.genesis],
      wraps: data.wraps.map((wrap) => ({ ...wrap, wrapped_by: user.id })),
      events: data.events,
    };
    this.orgs.set(id, org);
    user.orgs.push(id);
    return { org_id: id, default_wg: org.defaultWg };
  }

  async #orgLog(id: string): Promise<unknown> {
    const org = this.orgs.get(id);
    if (org === undefined) throw new HubFailure(404, 'not_found');
    const last = org.entries.length - 1;
    return {
      entries: org.entries.map((entry, seq) => ({ seq, entry })),
      head: { seq: last, hash: await entryHash(org.entries[last]) },
      next_after: null,
    };
  }
}
