// A sealed log in the task world (ADR-078 section 3): an org device signs the manifest, the chunks are sealed with
// `k_log` of the user's workgroup key, and the client double answers the two log routes.
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { ed25519Sign } from '../../../src/core/crypto/keys';
import { CHUNK_BYTES, digest, logKey, type Manifest, manifestText, sealChunk } from '../../../src/core/crypto/runlog';
import { epochKey } from '../../../src/core/log/state';
import type { LogSource } from '../../../src/core/logs/reader';
import { addKeys, setLog } from '../../../src/core/store/slices/orgs';
import { actors, type Actor } from '../log/world';
import { type TaskWorld, taskWorld } from '../tasks/world';

export const TASK = '0190f000-0000-7000-8000-000000000001';
export const SESSION = '0190f000-0000-7000-8000-0000000000a1';
export const OBJECT = '0190f000-0000-7000-8000-00000000f001';
export const REF = 'tr-0123456789ab';

export interface LogWorld {
  readonly t: TaskWorld;
  readonly device: Actor;
  readonly epoch: number;
}

/** The task world with an org device assigned to the workgroup. */
export async function logWorld(): Promise<LogWorld> {
  const t = await taskWorld();
  const [device] = await actors('log device');
  await t.world.admitDevice(t.ada, device, [t.wg]);
  setLog(t.store, t.org, t.world.log!);
  addKeys(t.store, t.org, t.world.held.get(t.me.id)!);
  return { t, device, epoch: t.world.state.epoch('wg', t.wg) };
}

export const logPath = (suffix = '') => `GET /v1/tasks/${TASK}/logs/${OBJECT}${suffix}`;

export interface Built {
  readonly manifest: Record<string, unknown>;
  readonly sig: string;
  readonly sealed: Uint8Array[];
  readonly source: LogSource;
}

export interface BuildOptions {
  /** Manifest fields to replace (signed as given). */
  readonly manifest?: Record<string, unknown>;
  /** The full output length (default: the output). */
  readonly bytes?: number;
  /** The pieces instead of fixed-size chunks of `output`. */
  readonly pieces?: Uint8Array[];
  /** Signs with this actor instead of the device. */
  readonly signer?: Actor;
}

/** Seals `output`, signs the manifest and answers both routes. */
export async function buildLog(w: LogWorld, output: Uint8Array, options: BuildOptions = {}): Promise<Built> {
  const { t, device, epoch } = w;
  const salt = new Uint8Array(32).fill(3);
  const wk = t.world.held.get(t.me.id)!.get(epochKey('wg', t.wg, epoch))!;
  const key = await logKey(wk, salt, OBJECT);
  const pieces = options.pieces ?? [];
  if (options.pieces === undefined) {
    for (let at = 0; at < output.length; at += CHUNK_BYTES) pieces.push(output.subarray(at, at + CHUNK_BYTES));
  }
  const sealed = await Promise.all(pieces.map((piece, index) => sealChunk(key, OBJECT, index, piece)));
  const stored = pieces.reduce((sum, piece) => sum + piece.length, 0);
  const bytes = options.bytes ?? stored;
  const manifest = {
    v: 1,
    kind: 'log',
    ref: REF,
    object: OBJECT,
    org: t.org,
    wg: t.wg,
    task: TASK,
    session: SESSION,
    author: device.id,
    epoch,
    salt: b64uEncode(salt),
    chunks: sealed.length,
    size: sealed.reduce((sum, chunk) => sum + chunk.length, 0),
    digest: await digest(sealed),
    bytes,
    stored,
    ts: '2026-11-24T12:00:00.000Z',
    ...options.manifest,
  };
  const signer = options.signer ?? device;
  const sig = b64uEncode(await ed25519Sign(signer.identity.sig.privateKey, manifestText(manifest as Manifest)));
  t.client.on(logPath(), () => ({
    manifest,
    sig,
    created_at: '2026-11-24T12:00:00.000Z',
    completed_at: '2026-11-24T12:00:01.000Z',
  }));
  sealed.forEach((chunk, index) => t.client.on(logPath(`/chunks/${index}`), () => ({ ct: b64uEncode(chunk) })));
  const source: LogSource = {
    wg: t.wg,
    task: TASK,
    ref: REF,
    log: { object_id: OBJECT, bytes, stored_bytes: stored, truncated: stored < bytes },
  };
  return { manifest, sig, sealed, source };
}

/** `length` bytes of ASCII text with a line break every 80 bytes. */
export function ascii(length: number, letter = 'a'): Uint8Array {
  const out = new Uint8Array(length).fill(letter.charCodeAt(0));
  for (let at = 79; at < length; at += 80) out[at] = 10;
  return out;
}
