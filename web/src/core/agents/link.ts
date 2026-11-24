// The pairing link of an AI agent (11-agent-access.md section 5.1): `<origin>/connect#p=<pairing id>&f=<fingerprint>&
// c=<loopback port>&a=<agent name>&h=<computer name>`. The fragment never reaches the hub; it is parsed strictly:
// exactly these five parameters, each once (form encoding: `%XX`, `+` is a space), `p` a UUIDv7, `f` the 22-character
// key fingerprint (16 bytes b64u), `c` a decimal port 1024–65535 without leading zeros, `a` and `h` texts of the label
// rules (`crypto/agent.ts`). Anything else is no link (null): the page says it is not valid.
//
// A pairing from another computer (ADR-067, section 5.4) has `m=remote` instead of `c`: exactly `p`, `f`, `m`, `a`,
// `h`. The mode of the link must agree with the hub's `remote` (`pairing.ts`), so neither a changed link nor a hub
// that flips the flag turns one mode into the other.
import { displayName } from '../account/genesis';
import { isLabelText, FINGERPRINT_BYTES } from '../crypto/agent';
import { isB64u } from '../encoding/b64u';
import { isUuid7 } from '../encoding/ids';

export const PORT_MIN = 1024;
export const PORT_MAX = 65535;
export const LINK_KEYS = ['p', 'f', 'c', 'a', 'h'] as const;
export const REMOTE_LINK_KEYS = ['p', 'f', 'm', 'a', 'h'] as const;
export const MODE_REMOTE = 'remote';

/** `loopback`: the agent listens on this computer; `remote`: it runs on another computer (ADR-067). */
export type PairingMode = 'loopback' | 'remote';
const FINGERPRINT_LENGTH = Math.ceil((FINGERPRINT_BYTES * 4) / 3);
const PORT_RE = /^[1-9][0-9]{3,4}$/;

export interface PairingLink {
  readonly mode: PairingMode;
  readonly pairingId: string;
  readonly fingerprint: string;
  /** The loopback port (`c`); null for a remote pairing. */
  readonly port: number | null;
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
  const mode: PairingMode = values.has('m') ? 'remote' : 'loopback';
  const keys = mode === 'remote' ? REMOTE_LINK_KEYS : LINK_KEYS;
  if (entries.length !== keys.length || values.size !== keys.length) return null;
  if (!keys.every((key) => values.has(key))) return null;
  const [p, f, c, a, h] = keys.map((key) => values.get(key)!);
  // `c` of a loopback link is the port; `m` of a remote link must be exactly `remote`
  const port = mode === 'remote' ? null : parsePort(c!);
  const modeValid = mode === 'remote' ? c === MODE_REMOTE : port !== null;
  if (!modeValid || !isUuid7(p) || f!.length !== FINGERPRINT_LENGTH || !isB64u(f)) return null;
  if (!isLabelText(a) || !isLabelText(h)) return null;
  return { mode, pairingId: p, fingerprint: f, port, name: a, host: h };
}

/** The fragment of a pairing link (the inverse of `parsePairingLink`). */
export function pairingFragment(link: PairingLink): string {
  const mode: [string, string] = link.mode === 'remote' ? ['m', MODE_REMOTE] : ['c', String(link.port)];
  return new URLSearchParams([
    ['p', link.pairingId],
    ['f', link.fingerprint],
    mode,
    ['a', link.name],
    ['h', link.host],
  ]).toString();
}

/** The invitation of an AI agent (11-agent-access.md section 5.2): the invitation code, then the pairing. */
export interface InviteFragment {
  /** The invitation code text (the part before the first `&`). */
  readonly code: string;
  /** The pairing that follows the code; null for a plain invitation. */
  readonly pairing: PairingLink | null;
  /** The display name the agent proposes (`u`), or null (the name field is shown). */
  readonly displayName: string | null;
  /** False when parameters follow the code but are not exactly a valid pairing (with an optional `u`). */
  readonly valid: boolean;
}

const DISPLAY_NAME_KEY = 'u';

/**
 * `/invite#<code>[&p=…&f=…&c=…&a=…&h=…[&u=<display name>]]` (11-agent-access.md section 5.2): the code stays the
 * first part; parameters after it must be exactly a pairing link and at most one display name. A name that is not
 * 1–80 characters after trimming makes the fragment invalid like a broken pairing.
 */
export function parseInviteFragment(fragment: string): InviteFragment {
  const split = fragment.indexOf('&');
  if (split < 0) return { code: fragment, pairing: null, displayName: null, valid: true };
  const code = fragment.slice(0, split);
  const invalid = { code, pairing: null, displayName: null, valid: false } as const;
  const params = new URLSearchParams(fragment.slice(split + 1));
  const names = params.getAll(DISPLAY_NAME_KEY);
  if (names.length > 1) return invalid;
  params.delete(DISPLAY_NAME_KEY);
  const pairing = parsePairingLink(params.toString());
  if (pairing === null) return invalid;
  if (names.length === 0) return { code, pairing, displayName: null, valid: true };
  const name = displayName(names[0]!);
  return name === null ? invalid : { code, pairing, displayName: name, valid: true };
}
