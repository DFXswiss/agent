// Test world of the whiteboards: the task world (verified org, keys, scriptable client) with a real event reader
// behind `ingest`, a second workgroup "Research" (the admin writes, `bob` reads), and sealing helpers for whiteboard
// events of any author, as the hub would store them.
import { bodyJson } from '../../../src/core/encoding/json';
import { newHeader, sealEvent } from '../../../src/core/crypto/event';
import { epochKey, ORG, WG } from '../../../src/core/log/state';
import type { StoredEvent } from '../../../src/core/net/sse';
import { EventReader } from '../../../src/core/store/reader';
import { addKeys, setLog } from '../../../src/core/store/slices/orgs';
import type { BoardLocation } from '../../../src/core/whiteboards/location';
import { actor, type Actor, idOf, TS } from '../log/world';
import { type TaskWorld, taskWorld, type WorldOptions } from '../tasks/world';

export interface BoardWorld extends TaskWorld {
  readonly reader: EventReader;
  readonly research: string;
  readonly bob: Actor;
  /** The next `scope_seq` per scope. */
  readonly seqs: Map<string, number>;
}

/** `member`: act as bob (a member who reads Research and writes the default workgroup); else as ada (the admin). */
export async function boardWorld(options: WorldOptions = {}): Promise<BoardWorld> {
  const t = await taskWorld({ ...options, private: false });
  const bob = options.member ? t.me : await actor('bob');
  const research = await idOf('research');
  if (!options.member) await t.world.admitUser(t.ada, bob, 'member');
  await t.world.batch(t.ada, [
    { type: 'wg_create', wg: research, epoch: 1 },
    { type: 'grant', subject: bob.id, wg: research, access: 'read' },
  ]);
  setLog(t.store, t.org, t.world.log!);
  addKeys(t.store, t.org, t.world.held.get(t.me.id)!);
  const reader = new EventReader(t.store, { trigger: async () => true });
  t.context.ingest.mockImplementation(async (org: string, scope: string, items: readonly StoredEvent[]) => {
    t.ingested.push({ org, scope, items });
    await reader.ingest(org, scope, items);
  });
  return { ...t, reader, research, bob, seqs: new Map() };
}

let counter = 0;

/** A whiteboard event at `location` sealed by `author` (any right) with the scope's current key. */
export async function boardEvent(
  w: BoardWorld,
  location: BoardLocation,
  kind: string,
  ref: string,
  body: unknown,
  author: Actor = w.ada,
): Promise<unknown> {
  const scope = location.level === 'org' ? ORG : WG;
  const scopeId = location.level === 'org' ? location.org : location.wg!;
  const epoch = w.world.state.epoch(scope, scopeId);
  const key = w.world.held.get(w.ada.id)!.get(epochKey(scope, scopeId, epoch))!;
  counter += 1;
  const header = newHeader({
    id: await idOf(`board event ${counter}`),
    org: location.org,
    scope,
    scope_id: scopeId,
    epoch,
    author: author.id,
    kind,
    ts: TS,
    lh: w.world.log!.head.seq,
    wg: location.level === 'org' ? '' : location.wg!,
    task: location.task ?? '',
    ref,
  });
  return sealEvent(key, header, bodyJson(body), author.identity.sig.privateKey);
}

/** Stores events as the hub would (next `scope_seq` of the scope) and reads them; returns the stored items. */
export async function deliver(
  w: BoardWorld,
  location: BoardLocation,
  events: readonly unknown[],
): Promise<StoredEvent[]> {
  const scopeId = location.level === 'org' ? location.org : location.wg!;
  const items = events.map((event) => {
    const seq = (w.seqs.get(scopeId) ?? 0) + 1;
    w.seqs.set(scopeId, seq);
    return { scope_seq: seq, received_at: '2026-11-21T12:00:01.000Z', event };
  });
  await w.reader.ingest(location.org, scopeId, items);
  return items;
}

/** A fresh entry ID. */
export const entryId = (label: string) => idOf(`entry ${label}`);
