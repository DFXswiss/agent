// The attachment format (04-crypto.md section 4.14, ADR-102) beyond the shared vectors: a file of several chunks, the
// limits of pieces, files and lists, and every field rule of a reference.
import { describe, expect, it } from 'vitest';
import * as attachment from '../../../src/core/crypto/attachment';
import { Secret } from '../../../src/core/crypto/secret';
import { b64uEncode } from '../../../src/core/encoding/b64u';

const OBJECT = '0190f000-0000-7000-8000-00000000f001';
const OTHER = '0190f000-0000-7000-8000-00000000f002';
const WK = Secret.of(new Uint8Array(32).fill(1));

const seal = (data: Uint8Array, extra: Partial<Parameters<typeof attachment.sealAttachment>[1]> = {}) =>
  attachment.sealAttachment(WK, {
    objectId: OBJECT,
    name: 'report.pdf',
    type: 'application/pdf',
    epoch: 1,
    data,
    ...extra,
  });

describe('attachment', () => {
  it('seals and opens a file of several chunks with a random salt', async () => {
    const data = new Uint8Array(attachment.CHUNK_BYTES + 4).map((_, index) => index % 251);
    const { ref, chunks } = await seal(data);
    expect(chunks).toHaveLength(2);
    expect(attachment.chunkCount(data.length)).toBe(2);
    expect(chunks.reduce((sum, chunk) => sum + chunk.length, 0)).toBe(attachment.sealedSize(data.length));
    expect(ref.bytes).toBe(data.length);
    expect(await attachment.openAttachment(WK, ref, chunks)).toEqual(data);
    await expect(attachment.openAttachment(WK, ref, [chunks[1]!, chunks[0]!])).rejects.toMatchObject({
      code: 'header_invalid',
    });
    await expect(attachment.openAttachment(WK, ref, chunks.slice(0, 1))).rejects.toMatchObject({
      code: 'header_invalid',
    });
    const other = await seal(data);
    expect(other.ref.salt).not.toBe(ref.salt);
  });

  it('refuses keys, pieces, files and chunks of the wrong size', async () => {
    expect(() => attachment.attachmentKey(Secret.of(new Uint8Array(16)), new Uint8Array(32), OBJECT)).toThrow(
      'header_invalid',
    );
    expect(() => attachment.attachmentKey(WK, new Uint8Array(31), OBJECT)).toThrow('header_invalid');
    const key = await attachment.attachmentKey(WK, new Uint8Array(32), OBJECT);
    expect(() => attachment.sealChunk(key, OBJECT, 0, new Uint8Array(0))).toThrow('size_exceeded');
    expect(() => attachment.sealChunk(key, OBJECT, 0, new Uint8Array(attachment.CHUNK_BYTES + 1))).toThrow(
      'size_exceeded',
    );
    const big = new Uint8Array(attachment.CHUNK_CT_BYTES + 1);
    expect(() => attachment.openChunk(key, OBJECT, 0, big)).toThrow('size_exceeded');
    expect(() => attachment.chunkAad(OBJECT, attachment.MAX_CHUNKS)).toThrow('header_invalid');
    expect(() => attachment.split(new Uint8Array(0))).toThrow('size_exceeded');
    expect(() => attachment.split(new Uint8Array(attachment.MAX_BYTES + 1))).toThrow('size_exceeded');
    const { ref, chunks } = await seal(new Uint8Array([1, 2, 3]));
    await expect(attachment.checkChunks(ref, [big])).rejects.toMatchObject({ code: 'size_exceeded' });
    await expect(attachment.checkChunks(ref, [...chunks, chunks[0]!])).rejects.toMatchObject({
      code: 'header_invalid',
    });
    await expect(attachment.checkChunks(ref, [new Uint8Array([...chunks[0]!, 1])])).rejects.toMatchObject({
      code: 'header_invalid',
    });
  });

  it('needs one fixed nonce per piece and repeats the bytes with them', async () => {
    await expect(seal(new Uint8Array([1]), { nonces: [] })).rejects.toMatchObject({ code: 'header_invalid' });
    const fixed = { salt: new Uint8Array(32), nonces: [new Uint8Array(12)] };
    expect(await seal(new Uint8Array([1]), fixed)).toEqual(await seal(new Uint8Array([1]), fixed));
  });

  it.each(['', 'x'.repeat(256), 'a/b', 'a\\b', 'a\u0000b', 'tab\there', 'del\u007f', '.', '..', 7, null])(
    'refuses the name %j',
    (name) => {
      expect(() => attachment.checkName(name)).toThrow('header_invalid');
    },
  );

  it.each(['a', 'x'.repeat(255), '😀'.repeat(255), '...', '.env', 'Bildschirmfoto 2026.png'])(
    'keeps the name %j',
    (name) => {
      expect(attachment.checkName(name)).toBe(name);
    },
  );

  it.each(['', 'image', 'Image/png', 'image/', '/png', 'image/png; charset=x', `a/${'b'.repeat(126)}`, 5])(
    'refuses the media type %j',
    (type) => {
      expect(() => attachment.checkType(type)).toThrow('header_invalid');
    },
  );

  it('keeps media types and checks references and their lists', async () => {
    expect(attachment.checkType('text/x-c++')).toBe('text/x-c++');
    const { ref } = await seal(new Uint8Array([1]));
    expect(attachment.checkRef(ref)).toBe(ref);
    expect(attachment.checkRefs([])).toEqual([]);
    const other = { ...ref, object: OTHER };
    expect(attachment.checkRefs([ref, other])).toEqual([ref, other]);
    expect(() => attachment.checkRefs({ 0: ref })).toThrow('header_invalid');
    expect(() => attachment.checkRefs([ref, ref])).toThrow('header_invalid');
    expect(() => attachment.checkRefs(Array.from({ length: 11 }, () => ref))).toThrow('header_invalid');
    for (const [key, value] of [
      ['object', 'x'],
      ['bytes', 0],
      ['epoch', 0],
      ['salt', 'AAAA'],
      ['digest', b64uEncode(new Uint8Array(31))],
    ] as const) {
      expect(() => attachment.checkRef({ ...ref, [key]: value }), key).toThrow('header_invalid');
    }
    expect(() => attachment.checkRef({ ...ref, chunks: 1 })).toThrow('header_invalid');
  });
});
