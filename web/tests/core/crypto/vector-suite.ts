// CON-02 (TypeScript side): the shared vectors of contracts/vectors/ (read directly from the repository) checked
// against src/core byte for byte. The suite is framework-free so that it runs unchanged in Vitest (Node WebCrypto,
// tests/core/crypto/vectors.test.ts) and in Chromium (browser WebCrypto, e2e/core-interop.spec.ts).
//
// Positive vectors are reproduced, not only opened: every seal is repeated with the vector's fixed nonce, salt or
// ephemeral key and must give the identical bytes. Negative vectors are replayed through `OPS` – the TypeScript twin
// of `agent_hub.crypto.vectors.OPS` – and must fail with exactly the stated code.
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { concat, fromHex, fromUtf8, toHex, utf8 } from '../../../src/core/encoding/bytes';
import { uuid7 } from '../../../src/core/encoding/ids';
import { bodyJson, canonical, canonicalText, parseJson } from '../../../src/core/encoding/json';
import { formatTime, parseTime } from '../../../src/core/encoding/time';
import { aesKey, seal as aeadSeal } from '../../../src/core/crypto/aead';
import * as bundle from '../../../src/core/crypto/bundle';
import * as credential from '../../../src/core/crypto/credential';
import * as dedupe from '../../../src/core/crypto/dedupe';
import * as enroll from '../../../src/core/crypto/enroll';
import { CryptoError } from '../../../src/core/crypto/errors';
import * as event from '../../../src/core/crypto/event';
import * as hpke from '../../../src/core/crypto/hpke';
import * as invite from '../../../src/core/crypto/invite';
import * as kdf from '../../../src/core/crypto/kdf';
import * as keys from '../../../src/core/crypto/keys';
import { PREFIX } from '../../../src/core/crypto/labels';
import * as recovery from '../../../src/core/crypto/recovery';
import * as request from '../../../src/core/crypto/request';
import { Secret } from '../../../src/core/crypto/secret';
import { subtle } from '../../../src/core/crypto/subtle';
import * as wrap from '../../../src/core/crypto/wrap';
import { deriveTitle } from '../../../src/core/tasks/title';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- vector files are untyped JSON
type Any = any;

const TEXTS = import.meta.glob<string>('../../../../contracts/vectors/*.json', {
  eager: true,
  query: '?raw',
  import: 'default',
});

/**
 * Every vector file of the repository by name (`encoding`, `log-events`, …), read as text and parsed by the strict
 * `parseJson` of the core (unsafe integers allowed: `canonical_unsafe_integer` carries 2**53 on purpose).
 */
export const VECTOR_FILES: Record<string, Any> = Object.fromEntries(
  Object.entries(TEXTS).map(([path, text]) => [
    /([^/]+)\.json$/.exec(path)![1]!,
    parseJson(utf8(text), { unsafeIntegers: 'number' }),
  ]),
);

/** The task title cases (`contracts/tasks/title.json`, data shared with the runner, outside the crypto vectors). */
export const TITLE_CASES: Any = parseJson(
  utf8(
    import.meta.glob<string>('../../../../contracts/tasks/title.json', {
      eager: true,
      query: '?raw',
      import: 'default',
    })['../../../../contracts/tasks/title.json']!,
  ),
);

/** The vector files this package consumes (04-crypto.md sections 1-4); the title cases come on top. */
export const CONSUMED = [
  'bundle',
  'credential',
  'dedupe',
  'encoding',
  'enroll',
  'event',
  'invite',
  'kdf',
  'keys',
  'recovery',
  'request',
  'wrap',
];

export interface Check {
  readonly name: string;
  readonly run: () => Promise<void>;
}

export class Mismatch extends Error {}

/** JSON with object keys sorted (by UTF-16 code unit; only for comparing values). */
export function stable(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    item !== null && typeof item === 'object' && !Array.isArray(item) && !(item instanceof Uint8Array)
      ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      : item,
  );
}

export function same(actual: unknown, expected: unknown, what: string): void {
  const normalise = (value: unknown) => (value instanceof Uint8Array ? b64uEncode(value) : value);
  const [left, right] = [stable(normalise(actual)), stable(normalise(expected))];
  if (left !== right) throw new Mismatch(`${what}: ${left.slice(0, 200)} != ${right.slice(0, 200)}`);
}

const b = (value: unknown) => b64uDecode(value);
const file = (name: string): Any => (name === 'title' ? TITLE_CASES : VECTOR_FILES[name]);
const PROBE_NONCE = new Uint8Array(12);
const PROBE = utf8('agent.dfx.swiss key probe');

/** Proves that a non-extractable AES-GCM key equals raw bytes: both encrypt a probe block identically. */
export async function sameAesKey(key: CryptoKey, raw: unknown, what: string): Promise<void> {
  same(
    await aeadSeal(key, PROBE_NONCE, PROBE, PROBE),
    await aeadSeal(await aesKey(b(raw)), PROBE_NONCE, PROBE, PROBE),
    what,
  );
}

/** Proves that a non-extractable HMAC key equals raw bytes: both sign a probe identically. */
export async function sameHmacKey(key: CryptoKey, raw: unknown, what: string): Promise<void> {
  const imported = await subtle.importKey('raw', b(raw), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  same(
    new Uint8Array(await subtle.sign('HMAC', key, PROBE)),
    new Uint8Array(await subtle.sign('HMAC', imported, PROBE)),
    what,
  );
}

/** A P-256 key from its scalar (PKCS#8 without the optional public key) and its 65-byte public point. */
export async function p256FromScalar(scalar: Uint8Array): Promise<keys.KeyPair> {
  const prefix = fromHex('3041020100301306072a8648ce3d020106082a8648ce3d0301070427302502010104' + '20');
  const pkcs8 = concat(prefix, scalar);
  const algorithm = { name: 'ECDSA', namedCurve: 'P-256' };
  const jwk = await subtle.exportKey('jwk', await subtle.importKey('pkcs8', pkcs8, algorithm, true, ['sign']));
  const privateKey = await subtle.importKey('pkcs8', pkcs8, algorithm, false, ['sign']);
  return { privateKey, publicKey: concat(new Uint8Array([4]), b(jwk.x), b(jwk.y)) };
}

const identity = (name: string) => {
  const actor = file('keys').identities[name];
  return keys.Identity.fromRaw(b(actor.kem_sk), b(actor.sig_sk));
};
const target = (a: Any): wrap.WrapTarget => ({
  scope: a.scope,
  scope_id: a.scope_id,
  epoch: a.epoch,
  recipient: a.recipient,
});
const saltNonce = (header: Any) => [b(header.salt), b(header.nonce)] as const;
const kem = (raw: unknown) => keys.x25519KeyPair(b(raw));

/** The TypeScript twin of `agent_hub.crypto.vectors.OPS` (the operations negative vectors name). */
export const OPS: Record<string, (a: Any) => Promise<unknown>> = {
  b64u_decode: async (a) => b64uDecode(a.text, a.length ?? undefined),
  canonical: async (a) => canonical(a.value),
  parse_time: async (a) => parseTime(a.text),
  kek_from_prf: async (a) => kdf.kekFromPrf(Secret.of(b(a.prf_output)), b(a.credential_id)),
  kek_from_recovery_code: async (a) => kdf.kekFromRecoveryCode(b(a.code)),
  recovery_id: async (a) => recovery.recoveryId(b(a.code)),
  invite_secrets: async (a) => invite.inviteSecrets(b(a.code)),
  ed25519_verify: async (a) => keys.ed25519Verify(b(a.public_key), b(a.signature), b(a.message)),
  p256_verify_raw: async (a) => keys.p256VerifyRaw(b(a.public_key), b(a.signature), b(a.message)),
  open_bundle: async (a) => bundle.openBundle(await aesKey(b(a.kek)), a.user_id, a.slot, a.sealed),
  hpke_open: async (a) => hpke.open(await kem(a.recipient_kem_sk), b(a.info), b(a.sealed)),
  unwrap_key: async (a) => wrap.unwrapKey(await kem(a.recipient_kem_sk), target(a), a.wrap),
  verify_wrap: async (a) => wrap.verifyWrap(b(a.wrapper_sig_pk), target(a), a.wrap, a.sig),
  verify_event: async (a) => event.verifyEvent(a.event, b(a.author_sig_pk)),
  open_event: async (a) => event.openEvent(Secret.of(b(a.scope_key)), a.event, b(a.author_sig_pk)),
  verify_credential: async (a) => credential.verifyCredential(a.record, b(a.author_sig_pk)),
  open_credential: async (a) => credential.openCredential(Secret.of(b(a.cred_key)), a.record, b(a.author_sig_pk)),
  verify_request: async (a) => request.verifyRequest(a.envelope, b(a.user_sig_pk)),
  open_request: async (a) => request.openRequest(await kem(a.device_kem_sk), a.envelope, b(a.user_sig_pk)),
  verify_response: async (a) => request.verifyResponse(a.envelope, b(a.device_sig_pk)),
  open_response: async (a) => request.openResponse(await kem(a.user_kem_sk), a.envelope, b(a.device_sig_pk)),
  parse_code: async (a) => invite.parseCode(a.text),
  parse_recovery_code: async (a) => recovery.parseRecoveryCode(a.text),
  open_package: async (a) => invite.openPackage(await invite.inviteSecrets(b(a.code)), a.sealed),
  verify_proof: async (a) => recovery.verifyProof(b(a.identity_sig_pk), a.user_id, b(a.challenge), a.proof),
  verify_enrollment: async (a) => enroll.verifyEnrollment(a.body, b(a.pk_inv)),
  verify_register: async (a) => invite.verifyRegister(b(a.pk_inv), a.user_id, b(a.challenge), a.inv_sig),
  dedupe_fields: async (a) => dedupe.dedupeFields(a.type, a.params),
};

/** Runs `op(args)` and returns the error code it fails with (`null` if it succeeds; other errors propagate). */
export async function refusal(op: string, args: unknown): Promise<string | null> {
  try {
    await OPS[op]!(args);
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return null;
}

/** One check per negative vector of every consumed file. */
export function negativeChecks(): Check[] {
  return [...CONSUMED, 'title'].flatMap((name) =>
    (file(name).invalid as Any[]).map((vector) => ({
      name: `${name}: invalid ${vector.op}:${vector.name}`,
      run: async () => {
        if (!Object.hasOwn(OPS, vector.op)) throw new Mismatch(`unknown op ${vector.op}`);
        same(await refusal(vector.op, vector.args), vector.error, `${vector.op}:${vector.name}`);
      },
    })),
  );
}

const POSITIVE: Record<string, (data: Any) => Promise<void>> = {
  encoding: async (data) => {
    for (const item of data.b64u) {
      same(b64uEncode(fromHex(item.hex)), item.b64u, 'b64u encode');
      same(toHex(b64uDecode(item.b64u)), item.hex, 'b64u decode');
    }
    for (const item of data.canonical) same(canonicalText(item.value), item.canonical, 'canonical');
    for (const item of data.body_json) {
      same(fromUtf8(bodyJson(item.value)), item.body_json, 'body JSON');
      same(parseJson(utf8(item.body_json)), item.value, 'body JSON parse');
    }
    for (const item of data.time) {
      same(formatTime(item.unix_ms), item.text, 'time format');
      same(parseTime(item.text), item.unix_ms, 'time parse');
    }
    for (const item of data.uuid7) same(uuid7(item.unix_ms, fromHex(item.random_hex)), item.uuid, 'uuid7');
  },
  kdf: async (data) => {
    same(PREFIX, data.prefix, 'prefix');
    same(await kdf.prfInput(), data.prf_input, 'prf input');
    const k = data.kek_from_prf;
    await sameAesKey(await kdf.kekFromPrf(Secret.of(b(k.prf_output)), b(k.credential_id)), k.kek, 'KEK_i');
    await sameAesKey(await kdf.kekFromRecoveryCode(b(data.recovery.code)), data.recovery.kek, 'KEK_rec');
    same(await recovery.recoveryId(b(data.recovery.code)), data.recovery.recovery_id, 'recovery_id');
    const inv = data.invite;
    const secrets = await invite.inviteSecrets(b(inv.code));
    same(secrets.inviteId, inv.invite_id, 'invite_id');
    same(secrets.pkInv, inv.pk_inv, 'pk_inv');
    await sameAesKey(secrets.packageKey, inv.package_key, 'package_key');
    same(
      (await kdf.inviteSignSeed(b(inv.code))).reveal((raw) => raw),
      inv.sk_inv_seed,
      'sk_inv seed',
    );
    const ev = data.event_key;
    await sameAesKey(await kdf.eventKey(Secret.of(b(ev.scope_key)), b(ev.salt)), ev.key, 'k_event');
    await sameHmacKey(await kdf.dedupeKey(Secret.of(b(data.dedupe_key.workgroup_key))), data.dedupe_key.key, 'dk');
    const ck = data.credential_key;
    await sameAesKey(await kdf.credentialKey(Secret.of(b(ck.credential_scope_key)), b(ck.salt)), ck.key, 'k_cred');
    const st = data.storage_key;
    await sameAesKey(await kdf.storageKey(Secret.of(b(st.device_sig_seed))), st.key, 'storage key');
  },
  keys: async (data) => {
    for (const [name, actor] of Object.entries<Any>(data.identities)) {
      same((await identity(name)).public(), { kem: actor.kem, sig: actor.sig }, `identity ${name}`);
    }
    const ed = data.ed25519;
    await keys.ed25519Verify(b(ed.public_key), b(ed.signature), b(ed.message));
    same(await (await identity('alice')).sign(b(ed.message)), ed.signature, 'Ed25519 signature');
    const p = data.p256;
    const session = await p256FromScalar(b(p.private_scalar));
    same(session.publicKey, p.public_key, 'P-256 public point');
    await keys.p256VerifyRaw(b(p.public_key), b(p.signature), b(p.message));
    await keys.p256VerifyRaw(session.publicKey, await keys.p256SignRaw(session.privateKey, b(p.message)), b(p.message));
  },
  bundle: async (data) => {
    const alice = await identity('alice');
    same(fromUtf8(bundle.bundlePlaintext(alice)), data.plaintext, 'bundle plaintext');
    const pk = data.passkey;
    same(bundle.passkeySlot(b(pk.credential_id)), pk.slot, 'slot');
    same(bundle.bundleAad(data.user_id, pk.slot), pk.aad, 'bundle AAD');
    const kek = await kdf.kekFromPrf(Secret.of(b(pk.prf_output)), b(pk.credential_id));
    same(await bundle.sealBundle(kek, data.user_id, pk.slot, alice, b(pk.nonce)), pk.sealed, 'passkey slot');
    same((await bundle.openBundle(kek, data.user_id, pk.slot, pk.sealed)).public(), alice.public(), 'open passkey');
    const rec = data.recovery;
    const kekRec = await kdf.kekFromRecoveryCode(b(rec.code));
    same(await bundle.sealBundle(kekRec, data.user_id, rec.slot, alice, b(rec.nonce)), rec.sealed, 'recovery slot');
    same(
      (await bundle.openBundle(kekRec, data.user_id, rec.slot, rec.sealed)).public(),
      alice.public(),
      'open recovery',
    );
  },
  wrap: async (data) => {
    const key = Secret.of(b(data.scope_key));
    const alice = await identity('alice');
    for (const w of data.wraps) {
      same(wrap.wrapInfo(target(w)), w.info, 'wrap info');
      same(await wrap.wrapKey(b(w.recipient_kem_pk), target(w), key, b(w.ephemeral_sk)), w.wrap, 'wrap');
      const setup = await hpke.setupValues(b(w.ephemeral_sk), b(w.recipient_kem_pk), b(w.info));
      same(
        [setup.shared_secret, setup.key, setup.base_nonce].map(b64uEncode),
        [w.shared_secret, w.key, w.base_nonce],
        'HPKE setup',
      );
      const opened = await wrap.unwrapKey(await kem(w.recipient_kem_sk), target(w), w.wrap);
      same(
        opened.reveal((raw) => raw),
        data.scope_key,
        'unwrap',
      );
      same(w.wrapper_sig_pk, alice.public().sig, 'wrapper');
      same(await wrap.signWrap(alice.sig.privateKey, target(w), w.wrap), w.sig, 'wrap signature');
      await wrap.verifyWrap(b(w.wrapper_sig_pk), target(w), w.wrap, w.sig);
    }
  },
  event: async (data) => {
    const alice = await identity('alice');
    for (const item of data.events) {
      const sealed = item.event;
      const key = Secret.of(b(item.scope_key));
      same(fromUtf8(canonical(sealed.header)), item.aad, 'event AAD');
      await sameAesKey(await kdf.eventKey(key, b(sealed.header.salt)), item.k_event, 'k_event');
      const { salt: _salt, nonce: _nonce, ...fields } = sealed.header;
      same(event.newHeader(fields), fields, 'event header fields');
      const mine = await event.sealEvent(
        key,
        fields,
        utf8(item.body_json),
        alice.sig.privateKey,
        saltNonce(sealed.header),
      );
      same(mine, sealed, 'sealed event');
      same(fromUtf8(await event.openEvent(key, sealed, b(item.author_sig_pk))), item.body_json, 'event body');
      same(await event.verifyEvent(sealed, b(item.author_sig_pk)), sealed.header, 'verified header');
    }
    same([data.limits.ct_max, data.limits.terminal_ct_max], [event.MAX_CT_BYTES, event.MAX_TERMINAL_CT_BYTES], 'E8');
  },
  credential: async (data) => {
    const alice = await identity('alice');
    const record = data.record;
    const key = Secret.of(b(data.cred_key));
    await sameAesKey(await kdf.credentialKey(key, b(record.header.salt)), data.k_cred, 'k_cred');
    const { salt: _salt, nonce: _nonce, ...fields } = record.header;
    same(credential.newHeader(fields), fields, 'credential header fields');
    const mine = await credential.sealCredential(
      key,
      fields,
      utf8(data.body_json),
      alice.sig.privateKey,
      saltNonce(record.header),
    );
    same(mine, record, 'sealed credential');
    same(
      fromUtf8(await credential.openCredential(key, record, b(data.author_sig_pk))),
      data.body_json,
      'credential body',
    );
  },
  request: async (data) => {
    const alice = await identity('alice');
    const device = await identity('device');
    same([alice.public().kem, alice.public().sig], [data.user.kem_pk, data.user.sig_pk], 'user keys');
    same([device.public().kem, device.public().sig], [data.device.kem_pk, data.device.sig_pk], 'device keys');
    for (const item of data.requests) {
      const env = item.envelope;
      const mine = await request.sealRequest(
        env.header,
        device.kemPublic(),
        utf8(item.body_json),
        alice.sig.privateKey,
        b(item.ephemeral_sk),
      );
      same(mine, env, 'request envelope');
      same(fromUtf8(await request.openRequest(device.kem, env, alice.sigPublic())), item.body_json, 'request body');
    }
    for (const item of data.responses) {
      const env = item.envelope;
      const mine = await request.sealResponse(
        env.header,
        alice.kemPublic(),
        utf8(item.body_json),
        device.sig.privateKey,
        b(item.ephemeral_sk),
      );
      same(mine, env, 'response envelope');
      same(fromUtf8(await request.openResponse(alice.kem, env, device.sigPublic())), item.body_json, 'response body');
    }
  },
  invite: async (data) => {
    const code = b(data.code);
    same(invite.formatCode(code), data.text, 'code text');
    for (const item of data.parse) same(invite.parseCode(item.text), item.code, `parse ${item.text}`);
    const secrets = await invite.inviteSecrets(code);
    same([secrets.inviteId, secrets.pkInv], [data.invite_id, data.pk_inv], 'invite secrets');
    await sameAesKey(secrets.packageKey, data.package_key, 'package key');
    same(fromUtf8(bodyJson(data.package)), data.package_body_json, 'package body JSON');
    same(invite.packageAad(secrets.inviteId), data.package_aad, 'package AAD');
    same(await invite.sealPackage(secrets, data.package, b(data.nonce)), data.sealed, 'sealed package');
    same(await invite.openPackage(secrets, data.sealed), data.package, 'opened package');
    const proof = data.register;
    same(invite.registerText(proof.user_id, b(proof.challenge)), proof.text, 'register text');
    same(await invite.signRegister(secrets, proof.user_id, b(proof.challenge)), proof.inv_sig, 'inv_sig');
    await invite.verifyRegister(b(proof.pk_inv), proof.user_id, b(proof.challenge), proof.inv_sig);
  },
  recovery: async (data) => {
    const code = b(data.code);
    same(recovery.formatRecoveryCode(code), data.text, 'recovery text');
    same(recovery.parseRecoveryCode(data.text), data.code, 'recovery parse');
    same(await recovery.recoveryId(code), data.recovery_id, 'recovery_id');
    await sameAesKey(await kdf.kekFromRecoveryCode(code), data.kek, 'KEK_rec');
    const proof = data.proof;
    same(recovery.proofText(proof.user_id, b(proof.challenge)), proof.text, 'proof text');
    const alice = await identity('alice');
    same(await recovery.signProof(alice.sig.privateKey, proof.user_id, b(proof.challenge)), proof.proof, 'proof');
    await recovery.verifyProof(b(proof.identity_sig_pk), proof.user_id, b(proof.challenge), proof.proof);
  },
  enroll: async (data) => {
    same(fromUtf8(enroll.enrollmentText(data.body)), data.text, 'enrollment text');
    const secrets = await invite.inviteSecrets(b(data.invite_code));
    same(secrets.pkInv, data.pk_inv, 'pk_inv');
    const device = await identity('device');
    same(await enroll.signEnrollment(data.body, device.sig, secrets.signing), data.body, 'signed enrollment');
    await enroll.verifyEnrollment(data.body, b(data.pk_inv));
  },
  dedupe: async (data) => {
    const dk = await kdf.dedupeKey(Secret.of(b(data.workgroup_key)));
    await sameHmacKey(dk, data.dedupe_key, 'dedupe key');
    same(
      dedupe.DEDUPE_TYPES,
      [...new Set(data.tags.filter((t: Any) => t.params).map((t: Any) => t.type))].sort(),
      'types',
    );
    for (const tag of data.tags) {
      const fields = tag.params
        ? await dedupe.dedupeFields(tag.type, tag.params)
        : await dedupe.idempotencyFields(tag.idempotency_key, tag.task);
      same(fields, tag.fields, `fields ${tag.type}`);
      same(await dedupe.dedupeTag(dk, tag.type, fields), tag.tag, `tag ${tag.type}`);
    }
  },
  title: async (data) => {
    for (const item of data.cases) same(deriveTitle(item.prompt), item.title, `title ${item.name}`);
  },
};

/** One check per consumed file (all positive vectors of that file). */
export function positiveChecks(): Check[] {
  return [...CONSUMED, 'title'].map((name) => ({
    name: `${name}: positive vectors`,
    run: () => POSITIVE[name]!(file(name)),
  }));
}

export interface Outcome {
  readonly name: string;
  readonly error: string | null;
}

/** Runs the checks one after the other and reports each outcome. */
export async function runChecks(checks: readonly Check[]): Promise<Outcome[]> {
  const outcomes: Outcome[] = [];
  for (const check of checks) {
    try {
      await check.run();
      outcomes.push({ name: check.name, error: null });
    } catch (error) {
      outcomes.push({ name: check.name, error: String(error) });
    }
  }
  return outcomes;
}

/** Runs every check and reports each outcome (the browser entry point of e2e/core-interop.spec.ts). */
export async function runAll(): Promise<Outcome[]> {
  return runChecks([...positiveChecks(), ...negativeChecks()]);
}
