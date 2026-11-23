// The pairing link of an AI agent (11-agent-access.md section 5.1): `<origin>/connect#p=<pairing id>&f=<fingerprint>&
// c=<loopback port>&a=<agent name>&h=<computer name>`. The fragment never reaches the hub; it is parsed strictly:
// exactly these five parameters, each once (form encoding: `%XX`, `+` is a space), `p` a UUIDv7, `f` the 22-character
// key fingerprint (16 bytes b64u), `c` a decimal port 1024–65535 without leading zeros, `a` and `h` texts of the label
// rules (`crypto/agent.ts`). Anything else is no link (null): the page says it is not valid.
import { isLabelText, FINGERPRINT_BYTES } from '../crypto/agent';
import { isB64u } from '../encoding/b64u';
import { isUuid7 } from '../encoding/ids';

export const PORT_MIN = 1024;
export const PORT_MAX = 65535;
export const LINK_KEYS = ['p', 'f', 'c', 'a', 'h'] as const;
const FINGERPRINT_LENGTH = Math.ceil((FINGERPRINT_BYTES * 4) / 3);
const PORT_RE = /^[1-9][0-9]{3,4}$/;

export interface PairingLink {
  readonly pairingId: string;
  readonly fingerprint: string;
  readonly port: number;
  /** The agent's name and computer as the link states them (they prove nothing). */
  readonly name: string;
  readonly host: string;
}

/** The port of `c`, or null outside 1024–65535 or not in canonical decimal form. */
export function parsePort(text: string): number | null {
  if (!PORT_RE.test(text)) return null;
  const port = Number(text);
  return port >= PORT_MIN && port <= PORT_MAX ? port : null;
}

/** The pairing of a fragment (without `#`), or null if it is not exactly a valid pairing link. */
export function parsePairingLink(fragment: string): PairingLink | null {
  const entries = [...new URLSearchParams(fragment)];
  const values = new Map(entries);
  if (entries.length !== LINK_KEYS.length || values.size !== LINK_KEYS.length) return null;
  if (!LINK_KEYS.every((key) => values.has(key))) return null;
  const [p, f, c, a, h] = LINK_KEYS.map((key) => values.get(key)!);
  const port = parsePort(c!);
  if (!isUuid7(p) || f!.length !== FINGERPRINT_LENGTH || !isB64u(f) || port === null) return null;
  if (!isLabelText(a) || !isLabelText(h)) return null;
  return { pairingId: p, fingerprint: f, port, name: a, host: h };
}
