// The refusals of the run-log format (04-crypto.md section 4.12) that the vectors do not reach: key and salt sizes,
// empty and oversized pieces, oversized sealed chunks and a manifest whose `ref` is no text.
import { describe, expect, it } from 'vitest';
import * as runlog from '../../../src/core/crypto/runlog';
import { Secret } from '../../../src/core/crypto/secret';

const OBJECT = '0190f000-0000-7000-8000-00000000f001';
const key = () => runlog.logKey(Secret.of(new Uint8Array(32).fill(1)), new Uint8Array(32).fill(2), OBJECT);

describe('runlog refusals', () => {
  it('refuses a workgroup key of the wrong size', () => {
    expect(() => runlog.logKey(Secret.of(new Uint8Array(16)), new Uint8Array(32), OBJECT)).toThrow('header_invalid');
  });

  it('refuses empty and oversized pieces and oversized sealed chunks', async () => {
    const k = await key();
    expect(() => runlog.sealChunk(k, OBJECT, 0, new Uint8Array(0))).toThrow('size_exceeded');
    expect(() => runlog.sealChunk(k, OBJECT, 0, new Uint8Array(runlog.CHUNK_BYTES + 1))).toThrow('size_exceeded');
    const big = new Uint8Array(runlog.CHUNK_CT_BYTES + 1);
    expect(() => runlog.openChunk(k, OBJECT, 0, big)).toThrow('size_exceeded');
    await expect(runlog.checkChunks({} as runlog.Manifest, [big])).rejects.toMatchObject({ code: 'size_exceeded' });
  });

  it('refuses a manifest whose ref is no text', () => {
    const manifest = Object.fromEntries(runlog.MANIFEST_KEYS.map((name) => [name, 1]));
    expect(() => runlog.checkManifest({ ...manifest, v: 1, kind: 'log', ref: 7 })).toThrow('header_invalid');
  });
});
