// A small simulated organization for the log tests: actors with deterministic identities, a verified log built only
// through `buildBatch`, the wraps the hub would store and, per actor, the scope keys it obtained by opening the wraps
// addressed to it (so every test proves the wraps end to end, not only their signatures).
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { utf8 } from '../../../src/core/encoding/bytes';
import { uuid7 } from '../../../src/core/encoding/ids';
import { formatTime } from '../../../src/core/encoding/time';
import { inviteSecrets } from '../../../src/core/crypto/invite';
import { Identity } from '../../../src/core/crypto/keys';
import { Secret } from '../../../src/core/crypto/secret';
import { sha256 } from '../../../src/core/crypto/subtle';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import { buildPlatformEntries, type Entry, type LogFields } from '../../../src/core/log/entries';
import type { KeyRing } from '../../../src/core/log/catchup';
import { type PlatformState, verifyPlatformLog } from '../../../src/core/log/platform';
import { type BatchInput, buildBatch, type BuiltBatch } from '../../../src/core/log/rotation';
import { epochKey } from '../../../src/core/log/state';
import type { VerifiedLog } from '../../../src/core/log/verify';
import type { WrapRecord } from '../../../src/core/log/wraps';
import { signEnrollment } from '../../../src/core/crypto/enroll';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';

export const T0 = Date.UTC(2026, 10, 21, 12, 0, 0);
export const TS = formatTime(T0);
export const EXPIRES = formatTime(T0 + 72 * 3600 * 1000);

/** 32 deterministic bytes for `label`. */
export const seed = async (label: string): Promise<Uint8Array> => sha256(utf8(`wcr-02 test ${label}`));

/** A deterministic UUIDv7 for `label`. */
export async function idOf(label: string): Promise<string> {
  return uuid7(T0, (await seed(`id ${label}`)).slice(0, 10));
}

export interface Actor {
  readonly name: string;
  readonly id: string;
  readonly identity: Identity;
}

/** An actor with deterministic keys. */
export async function actor(name: string): Promise<Actor> {
  return {
    name,
    id: await idOf(name),
    identity: await Identity.fromRaw(await seed(`kem ${name}`), await seed(`sig ${name}`)),
  };
}

/** Several actors at once, as a tuple. */
export async function actors<const N extends readonly string[]>(
  ...names: N
): Promise<{ -readonly [K in keyof N]: Actor }> {
  return (await Promise.all(names.map(actor))) as { -readonly [K in keyof N]: Actor };
}

/** The invitation secrets of a deterministic code for `label`. */
export async function inviteFor(label: string) {
  return inviteSecrets((await seed(`code ${label}`)).slice(0, 16));
}

/** A fresh scope key source with deterministic keys (`key 1`, `key 2`, …). */
export function keySource(prefix: string): () => Secret {
  let n = 0;
  return () => {
    n += 1;
    return Secret.of(new Uint8Array(32).fill(n).map((byte, i) => (byte + i + prefix.length) & 0xff));
  };
}

/** The platform root and a platform log with certified devices (`certify` names), built through the builders. */
export async function platformWith(
  devices: readonly Actor[],
): Promise<{ root: string; entries: Entry[]; state: PlatformState; seqs: Map<string, number> }> {
  const rootPair = await ed25519KeyPair(await seed('platform root'));
  const root = b64uEncode(rootPair.publicKey);
  const list: { type: string; [key: string]: unknown }[] = [{ type: 'root', pub: root }];
  const seqs = new Map<string, number>();
  for (const device of devices) {
    const secrets = await inviteFor(`platform ${device.name}`);
    list.push({ type: 'device_invite', invite_id: secrets.inviteId, pk_inv: secrets.pkInv, expires: EXPIRES });
    const body = await signEnrollment(
      {
        device_id: device.id,
        kind: 'platform',
        invite_id: secrets.inviteId,
        pub: device.identity.public(),
        agent_version: '1.0.0',
        os: 'linux',
        arch: 'arm64',
        capacity: {
          vm_memory_budget_mib: 1,
          vm_memory_used_mib: 0,
          vm_slots_total: 1,
          vm_slots_free: 1,
          pool_ready: 0,
        },
      },
      device.identity.sig,
      secrets.signing,
    );
    seqs.set(device.id, list.length);
    list.push({
      type: 'device_certify',
      device: device.id,
      pub: body.pub,
      invite_id: secrets.inviteId,
      enroll_sig: body.enroll_sig,
      inv_sig: body.inv_sig,
      agent_version: body.agent_version,
      os: body.os,
      arch: body.arch,
      capacity: body.capacity,
    });
  }
  const entries = await buildPlatformEntries(null, rootPair.privateKey, list, TS);
  return { root, entries, state: (await verifyPlatformLog(entries, root)).state, seqs };
}

/** The simulated org. */
export class World {
  log: VerifiedLog | null = null;
  readonly wraps: WrapRecord[] = [];
  readonly held = new Map<string, Map<string, Secret>>();
  readonly actors = new Map<string, Actor>();
  readonly batches: BuiltBatch[] = [];
  platform: PlatformState | null = null;
  private readonly newKey = keySource('world');

  constructor(
    readonly org: string,
    readonly defaultWg: string,
  ) {}

  static async create(founder: Actor, options: { private?: boolean } = {}): Promise<World> {
    const world = new World(await idOf('org'), await idOf('default wg'));
    world.add(founder);
    await world.batch(founder, [
      {
        type: 'genesis',
        subject: founder.id,
        pub: founder.identity.public(),
        default_wg: world.defaultWg,
        private: options.private ?? false,
      },
    ]);
    return world;
  }

  add(...actors: Actor[]): void {
    for (const a of actors) this.actors.set(a.id, a);
  }

  get state() {
    return this.log!.state;
  }

  /** The keys `actor` holds (opened from its wraps). */
  ring(actorId: string): KeyRing {
    return (scope, scopeId, epoch) => this.held.get(actorId)?.get(epochKey(scope, scopeId, epoch)) ?? null;
  }

  /** Builds a batch as `author`, "sends" it (the hub stores entries and wraps) and opens the new wraps. */
  async batch(
    author: Actor,
    entries: LogFields[],
    extra: Partial<Omit<BatchInput, 'log' | 'author' | 'entries'>> & { invite?: CryptoKey } = {},
  ): Promise<BuiltBatch> {
    const { invite, ...rest } = extra;
    const built = await buildBatch({
      log: this.log,
      org: this.org,
      author: { id: author.id, identity: author.identity, ...(invite && { invite }) },
      entries,
      keys: this.ring(author.id),
      platform: this.platform,
      ts: TS,
      newKey: this.newKey,
      ...rest,
    });
    await this.store(built.body.wraps);
    this.log = built.log;
    this.batches.push(built);
    return built;
  }

  /** Stores wraps and opens those addressed to known actors. */
  async store(wraps: readonly WrapRecord[]): Promise<void> {
    for (const wrap of wraps) {
      this.wraps.push(wrap);
      const recipient = this.actors.get(wrap.recipient_id);
      if (recipient === undefined) continue;
      const target = { scope: wrap.scope, scope_id: wrap.scope_id, epoch: wrap.epoch, recipient: wrap.recipient_id };
      const key = await unwrapKey(recipient.identity.kem, target, wrap.wrap);
      const mine = this.held.get(recipient.id) ?? new Map<string, Secret>();
      mine.set(epochKey(wrap.scope, wrap.scope_id, wrap.epoch), key);
      this.held.set(recipient.id, mine);
    }
  }

  /** Invites and admits a user (`invite` by `inviter`, `accept` by the user with its self-wraps). */
  async admitUser(
    inviter: Actor,
    user: Actor,
    role: 'admin' | 'member' = 'member',
    wgs: Record<string, 'read' | 'write'> = {},
  ): Promise<void> {
    this.add(user);
    const secrets = await inviteFor(`user ${user.name} ${this.log!.head.seq}`);
    await this.batch(inviter, [
      { type: 'invite', invite_id: secrets.inviteId, pk_inv: secrets.pkInv, kind: 'user', role, wgs, expires: EXPIRES },
    ]);
    await this.batch(
      user,
      [{ type: 'accept', invite_id: secrets.inviteId, subject: user.id, kind: 'user', pub: user.identity.public() }],
      { invite: secrets.signing.privateKey, keys: this.ring(inviter.id) },
    );
  }

  /** Invites and admits an org device assigned to `wgs`. */
  async admitDevice(inviter: Actor, device: Actor, wgs: string[]): Promise<void> {
    this.add(device);
    const secrets = await inviteFor(`device ${device.name} ${this.log!.head.seq}`);
    await this.batch(inviter, [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'device',
        wgs: Object.fromEntries(wgs.map((wg) => [wg, 'device' as const])),
        expires: EXPIRES,
      },
    ]);
    await this.batch(
      device,
      [
        {
          type: 'accept',
          invite_id: secrets.inviteId,
          subject: device.id,
          kind: 'device',
          pub: device.identity.public(),
        },
      ],
      { invite: secrets.signing.privateKey, keys: this.ring(inviter.id) },
    );
  }

  /** The key `actorId` holds for the current epoch of a scope, as bytes (b64u) for comparisons. */
  currentKey(actorId: string, scope: string, scopeId: string): string | null {
    const key = this.ring(actorId)(scope, scopeId, this.state.epoch(scope, scopeId));
    return key === null ? null : key.reveal((raw) => b64uEncode(raw));
  }
}

/** Decodes b64u (shortcut). */
export const b = (text: string) => b64uDecode(text);
