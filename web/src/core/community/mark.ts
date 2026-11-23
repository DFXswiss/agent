// The sender mark of the community (12-community.md section 6, ADR-061 decision 6): `#` and the first four characters
// of RFC 4648 base32 of SHA-256 of the user ID text, so that two people with the same display name stay
// distinguishable without showing the ID.
import { utf8 } from '../encoding/bytes';
import { sha256 } from '../crypto/subtle';

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

/** `#` + the first four base32 characters (20 bits) of SHA-256(user ID). */
export async function senderMark(userId: string): Promise<string> {
  const digest = await sha256(utf8(userId));
  const bits = (digest[0]! << 16) | (digest[1]! << 8) | digest[2]!;
  let mark = '#';
  for (let shift = 19; shift >= 4; shift -= 5) mark += ALPHABET[(bits >> shift) & 31];
  return mark;
}
