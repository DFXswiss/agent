// UTF-8 text of one page of a log (ADR-079 section 3): a log is split into chunks of a fixed number of output bytes,
// so a multi-byte character can straddle two chunks. A page shows one chunk: it leaves out the continuation bytes at
// its start (they end the character the previous page completes) and completes its last character with the first
// bytes of the next chunk. Anything else that is not valid UTF-8 is shown as U+FFFD (the output is untrusted text).
import { concat } from '../encoding/bytes';

/** A byte `10xxxxxx`: the continuation of a multi-byte character. */
const continuation = (byte: number): boolean => (byte & 0xc0) === 0x80;

/** How many continuation bytes the last character of `bytes` still needs (0 when it is complete). */
export function missingBytes(bytes: Uint8Array): number {
  for (let back = 1; back <= Math.min(4, bytes.length); back += 1) {
    const byte = bytes[bytes.length - back]!;
    if (continuation(byte)) continue;
    const size = byte >= 0xf0 ? 4 : byte >= 0xe0 ? 3 : byte >= 0xc0 ? 2 : 1;
    return Math.max(0, size - back);
  }
  return 0;
}

/**
 * The text of `bytes`: without the leading continuation bytes when `skipLead` (the page does not start the stored
 * output), and with the last character completed from `next` (the following chunk, null for the last one).
 */
export function decodeWindow(bytes: Uint8Array, skipLead: boolean, next: Uint8Array | null): string {
  let start = 0;
  while (skipLead && start < Math.min(3, bytes.length) && continuation(bytes[start]!)) start += 1;
  const need = next === null ? 0 : missingBytes(bytes);
  const body = bytes.subarray(start);
  return new TextDecoder('utf-8', { ignoreBOM: true }).decode(need > 0 ? concat(body, next!.subarray(0, need)) : body);
}
