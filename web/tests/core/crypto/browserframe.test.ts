// The refusals of the live frame format (04-crypto.md section 4.13) that the vectors do not reach: key and salt sizes,
// empty and oversized pictures, an oversized ct after the watched session and workgroup, and a fresh random salt.
import { describe, expect, it } from 'vitest';
import * as browserframe from '../../../src/core/crypto/browserframe';
import { Secret } from '../../../src/core/crypto/secret';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';

const WK = Secret.of(new Uint8Array(32).fill(1));
const HEADER = {
  browser: 'br-00000000000a',
  session: '01930044-0000-7000-8000-000000000001',
  wg: '01930044-0000-7000-8000-000000000002',
  epoch: 1,
  seq: 0,
  ts: '2026-11-25T12:00:00.000Z',
};
const WATCHED = { session: HEADER.session, wg: HEADER.wg };
const JPEG = new Uint8Array([0xff, 0xd8, 1, 2, 3, 0xff, 0xd9]);

describe('browserframe', () => {
  it('seals with a fresh salt and opens again', async () => {
    const first = await browserframe.sealFrame(WK, HEADER, JPEG);
    const second = await browserframe.sealFrame(WK, HEADER, JPEG);
    expect(first.salt).not.toEqual(second.salt);
    expect(Object.keys(first).sort()).toEqual([...browserframe.FRAME_KEYS].sort());
    expect(await browserframe.openFrame(WK, first, WATCHED)).toEqual(JPEG);
    expect(browserframe.checkFrame(first)).toBe(first);
  });

  it('refuses keys, salts and pictures of the wrong size', async () => {
    const salt = new Uint8Array(32);
    expect(() => browserframe.frameKey(Secret.of(new Uint8Array(16)), salt, HEADER.browser)).toThrow('header_invalid');
    expect(() => browserframe.frameKey(WK, new Uint8Array(31), HEADER.browser)).toThrow('header_invalid');
    expect(() => browserframe.browserId(7)).toThrow('header_invalid');
    await expect(browserframe.sealFrame(WK, HEADER, new Uint8Array(0))).rejects.toMatchObject({
      code: 'size_exceeded',
    });
    const big = new Uint8Array(browserframe.FRAME_BYTES + 1);
    await expect(browserframe.sealFrame(WK, HEADER, big)).rejects.toMatchObject({ code: 'size_exceeded' });
    await expect(browserframe.sealFrame(WK, { ...HEADER, epoch: 0 }, JPEG)).rejects.toMatchObject({
      code: 'header_invalid',
    });
  });

  it('checks the size after the formats and the watched session and workgroup', async () => {
    const frame = await browserframe.sealFrame(WK, HEADER, JPEG);
    const big = { ...frame, ct: b64uEncode(new Uint8Array(browserframe.FRAME_CT_MAX + 1)) };
    expect(() => browserframe.checkFrame(big)).toThrow('size_exceeded');
    expect(() => browserframe.checkFrame({ ...big, seq: true })).toThrow('header_invalid');
    expect(() => browserframe.checkFrame(big, { session: HEADER.wg })).toThrow('header_invalid');
    expect(() => browserframe.checkFrame(big, { wg: HEADER.session })).toThrow('header_invalid');
    await expect(browserframe.openFrame(WK, frame, { ...WATCHED, wg: HEADER.session })).rejects.toMatchObject({
      code: 'header_invalid',
    });
    expect(b64uDecode(frame.ct).length).toBe(JPEG.length + 28);
  });
});
