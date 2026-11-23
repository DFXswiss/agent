// Dedupe tags (04-crypto.md section 4.10; field sets of 03-api.md section 9), twin of `agent_hub.crypto.dedupe`.
// `tag = b64u(HMAC-SHA256(dk_e, P || "dedupe\0" || canonical({"type": type, "fields": fields})))` with
// `dk_e = HKDF(WK_e, info=P||"dedupe")` (`kdf.dedupeKey`). `dedupeFields` builds the normalised field set:
//
// | Type | Fields (normalised) |
// |---|---|
// | `github.checkout` | `repo`, `ref` (resolved commit SHA, lowercase), `session` |
// | `github.create_branch` | `repo`, `branch` |
// | `github.commit` | `repo`, `branch`, `parent_sha`, `tree_sha` |
// | `github.push` | `repo`, `branch`, `commit_sha` |
// | `github.create_pull_request` | `repo`, `head`, `base` |
// | `github.comment` | `repo`, `number`, `body` = sha256_hex(UTF-8 body) |
// | `subagent.request` | `session` (parent), `prompt` = sha256_hex(UTF-8 purpose || 0x00 || UTF-8 prompt) |
//
// Repository names and SHAs are lowercased, branch names lose a leading `refs/heads/`; a value that is not printable
// ASCII is replaced by `sha256:<hex>` of its UTF-8 bytes. With an idempotency key the field set is
// `{"idempotency_key": key, "task": task_id}`.
import { b64uEncode } from '../encoding/b64u';
import { concat, copy, toHex, utf8 } from '../encoding/bytes';
import { canonical, isPlainObject, isPrintableAscii, isWellFormed } from '../encoding/json';
import { fail } from './errors';
import * as F from './fields';
import { label, LABEL_DEDUPE } from './labels';
import { sha256, subtle } from './subtle';

const HEADS = 'refs/heads/';
type Params = Record<string, unknown>;
type Fields = Record<string, string | number>;

/** Lowercase hex SHA-256 of the UTF-8 text (text that is not valid Unicode is `header_invalid`). */
export async function sha256Hex(text: string): Promise<string> {
  if (!isWellFormed(text)) fail('header_invalid');
  return toHex(await sha256(utf8(text)));
}

async function ascii(value: string): Promise<string> {
  return isPrintableAscii(value) ? value : `sha256:${await sha256Hex(value)}`;
}

const repo = async (p: Params) => (await ascii(F.text(p.repo))).toLowerCase();
const branch = async (p: Params, name: string) => {
  const value = F.text(p[name]);
  return ascii(value.startsWith(HEADS) ? value.slice(HEADS.length) : value);
};
const sha = async (p: Params, name: string) => (await ascii(F.text(p[name]))).toLowerCase();
const session = (p: Params) => F.uuid(p.session);

const BUILDERS: Record<string, (p: Params) => Promise<Fields>> = {
  'github.checkout': async (p) => ({ repo: await repo(p), ref: await sha(p, 'ref'), session: session(p) }),
  'github.create_branch': async (p) => ({ repo: await repo(p), branch: await branch(p, 'branch') }),
  'github.commit': async (p) => ({
    repo: await repo(p),
    branch: await branch(p, 'branch'),
    parent_sha: await sha(p, 'parent_sha'),
    tree_sha: await sha(p, 'tree_sha'),
  }),
  'github.push': async (p) => ({
    repo: await repo(p),
    branch: await branch(p, 'branch'),
    commit_sha: await sha(p, 'commit_sha'),
  }),
  'github.create_pull_request': async (p) => ({
    repo: await repo(p),
    head: await branch(p, 'head'),
    base: await branch(p, 'base'),
  }),
  'github.comment': async (p) => ({
    repo: await repo(p),
    number: F.requireInt(p.number, 1),
    body: await sha256Hex(F.text(p.body)),
  }),
  'subagent.request': async (p) => ({
    session: session(p),
    prompt: await sha256Hex(`${F.text(p.purpose)}\0${F.text(p.prompt)}`),
  }),
};

/** Operation types with duplicate protection (03-api.md section 9); the others have no dedupe fields. */
export const DEDUPE_TYPES = Object.keys(BUILDERS).sort();

/** The normalised field set of `opType` from its parameters plus `session` (checkout, subagent parent). */
export async function dedupeFields(opType: string, params: unknown): Promise<Fields> {
  const builder = Object.hasOwn(BUILDERS, opType) ? BUILDERS[opType] : undefined;
  if (builder === undefined || !isPlainObject(params)) fail('header_invalid');
  return builder(params);
}

/** `{"idempotency_key": key, "task": task_id}` for callers that pass an idempotency key. */
export async function idempotencyFields(key: unknown, taskId: unknown): Promise<Fields> {
  return { idempotency_key: await ascii(F.text(key)), task: F.uuid(taskId) };
}

/** `P || "dedupe\0" || canonical({"type": opType, "fields": fields})`. */
export function dedupeText(opType: string, fields: Record<string, unknown>): Uint8Array<ArrayBuffer> {
  return concat(label(LABEL_DEDUPE), canonical({ type: opType, fields }));
}

/** The b64u HMAC-SHA256 tag the hub compares (it never learns `dk` or the fields); `dk` from `kdf.dedupeKey`. */
export async function dedupeTag(dk: CryptoKey, opType: string, fields: Record<string, unknown>): Promise<string> {
  return b64uEncode(new Uint8Array(await subtle.sign('HMAC', dk, copy(dedupeText(opType, fields)))));
}
