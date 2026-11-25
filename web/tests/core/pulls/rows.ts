// Hub rows of pull-request links with bodies sealed like the device or a person seals them (KOR-48), for the core
// and UI tests of the pull-request views.
import { prLinkBodyKey, prLinkKey, prLinkTag, sealPrLink } from '../../../src/core/crypto/context';
import type { Secret } from '../../../src/core/crypto/secret';
import { epochKey, WG } from '../../../src/core/log/state';
import type { TaskWorld } from '../tasks/world';

export const SESSION = '0190f000-0000-7000-8000-0000000000a1';
export const LINKED_AT = '2026-11-24T12:00:00.000Z';

export interface RowOptions {
  readonly task: string;
  readonly ref: { readonly repo: string; readonly number: number };
  /** The person who linked it; absent: the device's session. */
  readonly linkedBy?: string;
  readonly wg?: string;
}

/** The workgroup key of epoch 1 the world's user holds. */
export const wgKey = (t: TaskWorld, wg = t.wg): Secret => t.world.held.get(t.me.id)!.get(epochKey(WG, wg, 1))!;

/** A `pr_link_row` of epoch 1 with its sealed body. */
export async function sealedRow(t: TaskWorld, options: RowOptions) {
  const wg = options.wg ?? t.wg;
  const key = wgKey(t, wg);
  const { repo, number } = options.ref;
  const tag = await prLinkTag(await prLinkKey(key), repo, number);
  const linkedBy = options.linkedBy ?? null;
  const origin = linkedBy !== null ? `user:${linkedBy}` : `device:${SESSION}`;
  const sealed = await sealPrLink(await prLinkBodyKey(key), {
    wgId: wg,
    epoch: 1,
    tag,
    taskId: options.task,
    origin,
    repo,
    number,
  });
  return {
    epoch: 1,
    tag,
    sealed,
    task_id: options.task,
    session_id: linkedBy === null ? SESSION : null,
    linked_by: linkedBy,
    created_at: LINKED_AT,
  };
}
