// Live frames of a platform browser session (04-crypto.md section 4.13, ADR-085 section 6), twin of
// `agent_hub.crypto.browserframe` (vectors `contracts/vectors/browserframe.json`). The browser holds the workgroup
// key, so it opens the frames of the stream frame `browser_live` itself; the hub never sees the picture:
//
// - `k_frame = HKDF(WK_e, salt=frame.salt (32 B), info=P || "browser-frame\0" || browser_id)`,
// - `ct = b64u(nonce(12) || AES-256-GCM(k_frame, jpeg, AAD))`, `AAD = P || "browser-frame\0" ||
//   canonical({"browser", "epoch", "seq", "session", "ts", "wg"})`,
// - frame (JSON, clear except `ct`) with exactly `FRAME_KEYS`; no signature.
//
// Check order: key set and canonical JSON, the formats including a `ct` shorter than `FRAME_CT_MIN`
// (`header_invalid`), the watched `session` and `wg` (`header_invalid`), the size of `ct` (`size_exceeded`),
// decryption (`decrypt_failed`). Dropping a frame whose `seq` is not above the last shown one is the viewer's state.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { canonical } from '../encoding/json';
import * as aead from './aead';
import { fail } from './errors';
import * as F from './fields';
import { hkdfAesKey, KEY_BYTES, SALT_BYTES } from './kdf';
import { label } from './labels';
import { Secret } from './secret';
import { random } from './subtle';

export const VERSION = 1;
/** Info label of `k_frame` and AAD label of the ciphertext (without the prefix P). */
export const LABEL_BROWSER_FRAME = 'browser-frame\0';
/** At most this many JPEG bytes per frame. */
export const FRAME_BYTES = 512 * 1024;
export const FRAME_CT_MAX = FRAME_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES;
export const FRAME_CT_MIN = aead.NONCE_BYTES + aead.TAG_BYTES + 1;
export const MAX_EPOCH = 2 ** 31 - 1;
export const FRAME_KEYS = ['v', 'browser', 'session', 'wg', 'epoch', 'seq', 'ts', 'salt', 'ct'] as const;
const BROWSER_RE = /^br-[0-9a-f]{12}$/;

export type Frame = Record<(typeof FRAME_KEYS)[number], unknown>;

/** The header fields a frame's AAD covers. */
export interface FrameHeader {
  readonly browser: string;
  readonly session: string;
  readonly wg: string;
  readonly epoch: number;
  readonly seq: number;
  readonly ts: string;
}

/** A browser session ID `br-<12 hex>`. */
export function browserId(value: unknown): string {
  if (typeof value !== 'string' || !BROWSER_RE.test(value)) fail('header_invalid');
  return value;
}

/** `k_frame` as a non-extractable AES-256-GCM key; workgroup key and salt 32 bytes (`header_invalid`). */
export function frameKey(workgroupKey: Secret, salt: Uint8Array, browser: string): Promise<CryptoKey> {
  if (workgroupKey.length !== KEY_BYTES) fail('header_invalid');
  const info = concat(label(LABEL_BROWSER_FRAME), utf8(browserId(browser)));
  return hkdfAesKey(workgroupKey, info, F.requireBytes(salt, SALT_BYTES));
}

/** `P || "browser-frame\0" || canonical({"browser", "epoch", "seq", "session", "ts", "wg"})`. */
export function frameAad(header: FrameHeader): Uint8Array<ArrayBuffer> {
  const { browser, epoch, seq, session, ts, wg } = header;
  return concat(label(LABEL_BROWSER_FRAME), canonical({ browser, epoch, seq, session, ts, wg }));
}

/** Key set, formats, the expected `session` and `wg` when given, and the size of `ct`; returns the frame. */
export function checkFrame(value: unknown, expected: { session?: string; wg?: string } = {}): Frame {
  const frame = F.headerDict(value, FRAME_KEYS) as Frame;
  F.literal(frame.v, VERSION);
  browserId(frame.browser);
  F.uuid(frame.session);
  F.uuid(frame.wg);
  F.requireInt(frame.epoch, 1, MAX_EPOCH);
  F.requireInt(frame.seq, 0);
  F.time(frame.ts);
  F.b64uBytes(frame.salt, SALT_BYTES);
  const size = b64uDecode(frame.ct).length;
  if (size < FRAME_CT_MIN) fail('header_invalid');
  if (
    (expected.session !== undefined && frame.session !== expected.session) ||
    (expected.wg !== undefined && frame.wg !== expected.wg)
  ) {
    fail('header_invalid');
  }
  if (size > FRAME_CT_MAX) fail('size_exceeded');
  return frame;
}

/** The frame of one picture (an empty or too large picture is `size_exceeded`); salt and nonce random unless given. */
export async function sealFrame(
  workgroupKey: Secret,
  header: FrameHeader,
  jpeg: Uint8Array,
  salt: Uint8Array = random(SALT_BYTES),
  nonce?: Uint8Array,
): Promise<Frame> {
  if (jpeg.length < 1 || jpeg.length > FRAME_BYTES) fail('size_exceeded');
  const key = await frameKey(workgroupKey, salt, header.browser);
  const sealed = await aead.sealBlob(key, jpeg, frameAad(header), nonce);
  const { browser, session, wg, epoch, seq, ts } = header;
  const frame = { v: VERSION, browser, session, wg, epoch, seq, ts, salt: b64uEncode(salt), ct: b64uEncode(sealed) };
  return checkFrame(frame);
}

/** The JPEG bytes of a frame of the watched `session` in workgroup `wg`; `workgroupKey` is `WK_e` of its epoch. */
export async function openFrame(
  workgroupKey: Secret,
  value: unknown,
  watched: { session: string; wg: string },
): Promise<Uint8Array<ArrayBuffer>> {
  const frame = checkFrame(value, watched);
  const header = frame as unknown as FrameHeader;
  const key = await frameKey(workgroupKey, b64uDecode(frame.salt), header.browser);
  return aead.openBlob(key, b64uDecode(frame.ct), frameAad(header));
}
