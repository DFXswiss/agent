// Secret byte strings (scope keys, PRF outputs, private key seeds). 06-web.md section 2.3: keys live in memory as
// non-extractable `CryptoKey`s or as raw bytes in closures. A `Secret` keeps its bytes in a private field: they are
// not enumerable, not serialisable (`JSON.stringify` and `String` show a placeholder) and reach code only inside
// `reveal`, as a fresh copy.
import { copy } from '../encoding/bytes';
import { random } from './subtle';

export class Secret {
  readonly #raw: Uint8Array<ArrayBuffer>;

  private constructor(raw: Uint8Array<ArrayBuffer>) {
    this.#raw = raw;
  }

  /** A secret holding a copy of `raw`. */
  static of(raw: Uint8Array): Secret {
    return new Secret(copy(raw));
  }

  /** `n` fresh random bytes (scope keys: 32). */
  static random(n = 32): Secret {
    return new Secret(random(n));
  }

  get length(): number {
    return this.#raw.length;
  }

  /** Calls `consume` with a copy of the bytes and returns its result. */
  reveal<T>(consume: (raw: Uint8Array<ArrayBuffer>) => T): T {
    return consume(copy(this.#raw));
  }

  toJSON(): string {
    return '[secret]';
  }

  toString(): string {
    return '[secret]';
  }
}
