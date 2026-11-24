// Byte helpers shared by the encoding and crypto modules (no dependency, runs in Node and the browser).

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });

/** UTF-8 bytes of `text` (only for well-formed text; callers check lone surrogates first where it matters). */
export function utf8(text: string): Uint8Array<ArrayBuffer> {
  return encoder.encode(text);
}

/** Strict UTF-8 decoding: invalid sequences throw, a byte order mark is kept as U+FEFF. */
export function fromUtf8(raw: Uint8Array): string {
  return decoder.decode(raw);
}

/** The concatenation of `parts` as a new array. */
export function concat(...parts: readonly Uint8Array[]): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** Lowercase hex. */
export function toHex(raw: Uint8Array): string {
  return Array.from(raw, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** Bytes of an even-length hex text (only for constants and test data; malformed text throws `TypeError`). */
export function fromHex(text: string): Uint8Array<ArrayBuffer> {
  if (!/^(?:[0-9a-fA-F]{2})*$/.test(text)) throw new TypeError('hex text expected');
  return Uint8Array.from(text.match(/../g) ?? [], (pair) => parseInt(pair, 16));
}

/** Byte-wise equality (not constant time: only for public values). */
export function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  return a.length === b.length && a.every((byte, index) => byte === b[index]);
}

/** A copy backed by its own `ArrayBuffer` (WebCrypto takes `BufferSource`s; views of shared buffers are copied). */
export function copy(raw: Uint8Array): Uint8Array<ArrayBuffer> {
  return new Uint8Array(raw);
}

/** `ArrayBuffer`/view → own `Uint8Array` copy. */
export function bytesOf(source: ArrayBuffer | ArrayBufferView): Uint8Array<ArrayBuffer> {
  return new Uint8Array(
    ArrayBuffer.isView(source)
      ? new Uint8Array(source.buffer, source.byteOffset, source.byteLength)
      : new Uint8Array(source),
  );
}
