// Operator side of the Playwright suite: platform invitations through root-signed requests (03-api.md section 1,
// `agent-request-v1` with key ID `platform-root`), exactly what `agent-hub platform invite-user` does. The test
// platform root key is public (seed SHA-256("agent.dfx.swiss test platform root key"), decision WEB-01 12), so the
// suite derives its private key. Parallel workers append to the same platform log: a `seq_conflict` re-reads the
// log and tries again.
import { b64uEncode } from '../../src/core/encoding/b64u';
import { utf8 } from '../../src/core/encoding/bytes';
import { formatSeconds, formatTime } from '../../src/core/encoding/time';
import { formatCode, generateCode, inviteSecrets } from '../../src/core/crypto/invite';
import { ed25519KeyPair, ed25519Sign } from '../../src/core/crypto/keys';
import { random, sha256 } from '../../src/core/crypto/subtle';
import { buildPlatformEntries, entryHash, type Entry, type PlatformFields } from '../../src/core/log/entries';
import { requestText, sha256Hex } from '../../src/core/net/responseCheck';

const ROOT = 'platform-root';
const rootKey = async () => ed25519KeyPair(await sha256(utf8('agent.dfx.swiss test platform root key')));

async function rootRequest(origin: string, method: string, target: string, body?: unknown): Promise<Response> {
  const key = await rootKey();
  const content = body === undefined ? new Uint8Array(0) : utf8(JSON.stringify(body));
  const time = formatSeconds(Math.floor(Date.now() / 1000));
  const nonce = b64uEncode(random(16));
  const text = requestText(ROOT, method, new URL(origin).host, target, time, nonce, await sha256Hex(content));
  const headers: Record<string, string> = {
    'agent-key': ROOT,
    'agent-time': time,
    'agent-nonce': nonce,
    'agent-signature': b64uEncode(await ed25519Sign(key.privateKey, text)),
  };
  if (body !== undefined) headers['content-type'] = 'application/json';
  return fetch(new URL(target, origin), { method, headers, ...(body !== undefined && { body: content }) });
}

/** Appends platform-log entries after the current head (the `root` entry first on an empty log). */
export async function appendPlatform(origin: string, fields: readonly PlatformFields[]): Promise<void> {
  const key = await rootKey();
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const listing = (await (await rootRequest(origin, 'GET', '/v1/platform/enrollments')).json()) as { log: Entry[] };
    const log = listing.log;
    const head = log.length === 0 ? null : { seq: log.length - 1, hash: await entryHash(log[log.length - 1]) };
    const planned = log.length === 0 ? [{ type: 'root', pub: b64uEncode(key.publicKey) }, ...fields] : [...fields];
    const entries = await buildPlatformEntries(head, key.privateKey, planned);
    const response = await rootRequest(origin, 'POST', '/v1/platform/log', { expected_seq: log.length, entries });
    if (response.ok) return;
    const refusal = (await response.json()) as { error?: string };
    if (refusal.error !== 'seq_conflict') throw new Error(`platform log append refused: ${JSON.stringify(refusal)}`);
  }
  throw new Error('platform log append kept conflicting');
}

/** A new platform invitation (`user_invite`); returns the code text, which only the test keeps. */
export async function platformInvite(origin: string, hours = 72): Promise<{ code: string; inviteId: string }> {
  const raw = generateCode();
  const { inviteId: id, pkInv } = await inviteSecrets(raw);
  await appendPlatform(origin, [
    { type: 'user_invite', invite_id: id, pk_inv: pkInv, expires: formatTime(Date.now() + hours * 3600_000) },
  ]);
  return { code: formatCode(raw), inviteId: id };
}
