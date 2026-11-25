// Pull-request links (KOR-48, ADR-084 section 1): rows of the hub checked and opened with the workgroup key of their
// epoch (legacy rows, missing keys, bodies bound to another task, origin or pull request), the paged lists of a
// workgroup and of a task, every page once for the situation view, the lookup under every held epoch, linking under
// the current epoch and unlinking a person's link – against the core task world with real keys and sealed bodies.
import { describe, expect, it } from 'vitest';
import { prLinkBodyKey, prLinkKey, prLinkTag, sealPrLink } from '../../../src/core/crypto/context';
import { Secret } from '../../../src/core/crypto/secret';
import { epochKey, WG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import {
  ALL_LIMIT,
  ensureLinks,
  findLinks,
  heldEpochs,
  isLinkRow,
  LinkError,
  linkErrorCode,
  linkPull,
  loadLinks,
  MAX_PAGES,
  openLink,
  originOf,
  taskLinks,
  unlinkPull,
} from '../../../src/core/pulls/links';
import { addKeys } from '../../../src/core/store/slices/orgs';
import { linkList, listKey, type PullLink } from '../../../src/core/pulls/state';
import { taskWorld, type TaskWorld } from '../tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const OTHER = '0190f000-0000-7000-8000-000000000002';
const SESSION = '0190f000-0000-7000-8000-0000000000a1';
const AT = '2026-11-24T12:00:00.000Z';
const REF = { repo: 'Example-Org/Repo', number: 17 };

const keyOf = (t: TaskWorld, epoch = 1): Secret => t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, epoch))!;

interface RowOptions {
  readonly ref?: { repo: string; number: number };
  readonly task?: string;
  readonly session?: string | null;
  readonly linkedBy?: string | null;
  readonly epoch?: number;
  readonly key?: Secret;
  /** Seal for another task than the row names. */
  readonly sealTask?: string;
}

/** A hub row with a body sealed like the device or a person seals it. */
async function row(t: TaskWorld, options: RowOptions = {}) {
  const ref = options.ref ?? REF;
  const epoch = options.epoch ?? 1;
  const key = options.key ?? keyOf(t, epoch);
  const task = options.task ?? TASK;
  const session = options.session === undefined ? SESSION : options.session;
  const linkedBy = options.linkedBy ?? null;
  const tag = await prLinkTag(await prLinkKey(key), ref.repo, ref.number);
  const origin = linkedBy !== null ? `user:${linkedBy}` : `device:${session}`;
  const sealed = await sealPrLink(await prLinkBodyKey(key), {
    wgId: t.wg,
    epoch,
    tag,
    taskId: options.sealTask ?? task,
    origin,
    repo: ref.repo,
    number: ref.number,
  });
  return {
    epoch,
    tag,
    sealed,
    task_id: task,
    session_id: linkedBy === null ? session : null,
    linked_by: linkedBy,
    created_at: AT,
  };
}

const listRoute = (t: TaskWorld) => `GET /v1/workgroups/${t.wg}/pr-links`;

describe('isLinkRow', () => {
  it('accepts the rows of the contracts and of an older hub', () => {
    const base = { task_id: TASK, session_id: SESSION, linked_by: null, sealed: null, created_at: AT };
    expect(isLinkRow({ ...base, epoch: 1, tag: 'x' })).toBe(true);
    expect(isLinkRow({ task_id: TASK, session_id: SESSION, created_at: AT })).toBe(true);
    expect(isLinkRow({ ...base, session_id: null, linked_by: TASK, sealed: 's' })).toBe(true);
    for (const bad of [
      null,
      { ...base, epoch: 0 },
      { ...base, epoch: 1.5 },
      { ...base, tag: 3 },
      { ...base, sealed: 5 },
      { ...base, task_id: 'task' },
      { ...base, session_id: 'x' },
      { ...base, linked_by: 'x' },
      { ...base, linked_by: TASK },
      { ...base, session_id: null },
      { ...base, created_at: 1 },
    ]) {
      expect(isLinkRow(bad)).toBe(false);
    }
  });

  it('names the origin of the body', () => {
    expect(originOf({ session_id: SESSION, linked_by: null })).toBe(`device:${SESSION}`);
    expect(originOf({ session_id: null, linked_by: TASK })).toBe(`user:${TASK}`);
  });
});

describe('openLink', () => {
  it('opens a device link and a person link', async () => {
    const t = await taskWorld();
    const device = await row(t);
    expect(await openLink(t.store, t.org, t.wg, 1, device.tag, device)).toEqual({
      wg: t.wg,
      epoch: 1,
      tag: device.tag,
      taskId: TASK,
      sessionId: SESSION,
      linkedBy: null,
      createdAt: AT,
      pull: REF,
      code: null,
    });
    const person = await row(t, { linkedBy: t.me.id });
    const { session_id: _unused, ...withoutSession } = person;
    const opened = await openLink(t.store, t.org, t.wg, 1, person.tag, withoutSession as never);
    expect(opened).toMatchObject({ sessionId: null, linkedBy: t.me.id, pull: REF });
  });

  it('keeps the metadata of a link whose details are unavailable', async () => {
    const t = await taskWorld();
    const device = await row(t);
    const legacy = await openLink(t.store, t.org, t.wg, 1, device.tag, { ...device, sealed: null });
    expect(legacy).toMatchObject({ taskId: TASK, pull: null, code: 'legacy' });
    const { sealed: _gone, ...older } = device;
    expect(await openLink(t.store, t.org, t.wg, 1, device.tag, older as never)).toMatchObject({ code: 'legacy' });
    expect(await openLink(t.store, t.org, t.wg, 9, device.tag, device)).toMatchObject({ code: 'epoch_stale' });
    // a hub that moves a body to another task, turns a device link into a person's or swaps the tag
    const moved = await row(t, { sealTask: OTHER });
    expect(await openLink(t.store, t.org, t.wg, 1, moved.tag, moved)).toMatchObject({ code: 'decrypt_failed' });
    const turned = { ...device, session_id: null, linked_by: t.me.id };
    expect(await openLink(t.store, t.org, t.wg, 1, device.tag, turned)).toMatchObject({ code: 'decrypt_failed' });
    expect(await openLink(t.store, t.org, t.wg, 1, 'A'.repeat(43), device)).toMatchObject({ pull: null });
  });
});

describe('loadLinks', () => {
  it('loads the first page and the next page of a workgroup', async () => {
    const t = await taskWorld();
    const first = await row(t);
    const second = await row(t, { ref: { repo: 'a/b', number: 2 }, task: OTHER });
    const targets: string[] = [];
    t.client.on(listRoute(t), (_body, target) => {
      targets.push(target);
      return target.includes('cursor=')
        ? { items: [second, first], next_cursor: null }
        : { items: [first], next_cursor: 'c1' };
    });
    await loadLinks(t.context, t.org, t.wg, null);
    const key = listKey(t.wg, null);
    expect(linkList(t.store.getState().pulls, key)).toMatchObject({ status: 'ready', nextCursor: 'c1' });
    await loadLinks(t.context, t.org, t.wg, null, true);
    const list = linkList(t.store.getState().pulls, key);
    expect(list.ids).toHaveLength(2);
    expect(list.nextCursor).toBeNull();
    expect(targets).toEqual([
      `/v1/workgroups/${t.wg}/pr-links?limit=100`,
      `/v1/workgroups/${t.wg}/pr-links?cursor=c1&limit=100`,
    ]);
    // no further page, and never a second load of a list that is loading
    await loadLinks(t.context, t.org, t.wg, null, true);
    await loadLinks(t.context, t.org, t.wg, OTHER, true);
    expect(targets).toHaveLength(2);
    expect(taskLinks(t.store, t.wg, OTHER).map((link) => link.pull)).toEqual([{ repo: 'a/b', number: 2 }]);
  });

  it('leaves out the rows of other tasks in a task list and records failures', async () => {
    const t = await taskWorld();
    const own = await row(t);
    const foreign = await row(t, { task: OTHER, ref: { repo: 'a/b', number: 3 } });
    t.client.on(listRoute(t), (_body, target) => {
      expect(target).toContain(`task=${TASK}`);
      return { items: [own, foreign], next_cursor: null };
    });
    await loadLinks(t.context, t.org, t.wg, TASK);
    const key = listKey(t.wg, TASK);
    expect(linkList(t.store.getState().pulls, key).ids).toHaveLength(1);
    for (const answer of [
      { items: 'x', next_cursor: null },
      { items: [{ ...own, epoch: undefined }], next_cursor: null },
      { items: [{ ...own, tag: undefined }], next_cursor: null },
      { items: [], next_cursor: 5 },
      null,
    ]) {
      t.client.on(listRoute(t), () => answer);
      await loadLinks(t.context, t.org, t.wg, TASK);
      expect(linkList(t.store.getState().pulls, key)).toMatchObject({ status: 'error', code: 'unavailable' });
    }
  });

  it('writes nothing after a sign-out', async () => {
    const t = await taskWorld();
    t.client.on(listRoute(t), () => {
      t.store.reset();
      return { items: [], next_cursor: null };
    });
    await loadLinks(t.context, t.org, t.wg, null);
    expect(t.store.getState().pulls.lists).toEqual({});
    t.client.on(listRoute(t), () => {
      t.store.reset();
      throw new HubError('forbidden', 403);
    });
    await loadLinks(t.context, t.org, t.wg, null);
    expect(t.store.getState().pulls.lists).toEqual({});
  });
});

describe('ensureLinks', () => {
  it('reads every page once per store generation, at most MAX_PAGES', async () => {
    const t = await taskWorld();
    const link = await row(t);
    let calls = 0;
    t.client.on(listRoute(t), (_body, target) => {
      calls += 1;
      expect(target).toContain(`limit=${ALL_LIMIT}`);
      return { items: [link], next_cursor: `c${calls}` };
    });
    await Promise.all([ensureLinks(t.context, t.org, t.wg), ensureLinks(t.context, t.org, t.wg)]);
    expect(calls).toBe(MAX_PAGES);
    expect(taskLinks(t.store, t.wg, TASK)).toHaveLength(1);
    t.client.on(listRoute(t), () => ({ items: [], next_cursor: null }));
    await ensureLinks(t.context, t.org, t.wg);
    expect(calls).toBe(MAX_PAGES);
  });

  it('stops at the last page, forgets a failure and writes nothing after a sign-out', async () => {
    const t = await taskWorld();
    t.client.on(listRoute(t), () => {
      throw new HubError('unavailable', 503);
    });
    await ensureLinks(t.context, t.org, t.wg);
    expect(linkList(t.store.getState().pulls, listKey(t.wg, null)).status).toBe('error');
    let calls = 0;
    t.client.on(listRoute(t), () => {
      calls += 1;
      return { items: [], next_cursor: null };
    });
    await ensureLinks(t.context, t.org, t.wg);
    expect(calls).toBe(1);
    expect(linkList(t.store.getState().pulls, listKey(t.wg, null)).status).toBe('ready');
    // a sign-out during the read: nothing is written for the next user
    t.client.on(listRoute(t), () => {
      t.store.reset();
      return { items: [], next_cursor: null };
    });
    t.store.reset();
    await ensureLinks(t.context, t.org, t.wg);
    expect(t.store.getState().pulls.lists).toEqual({});
    t.client.on(listRoute(t), () => {
      t.store.reset();
      throw new HubError('unavailable', 503);
    });
    await ensureLinks(t.context, t.org, t.wg);
    expect(t.store.getState().pulls.lists).toEqual({});
  });
});

describe('findLinks', () => {
  it('looks the pull request up under every held epoch, newest first', async () => {
    const t = await taskWorld();
    const second = Secret.of(new Uint8Array(32).fill(9));
    addKeys(t.store, t.org, new Map([[epochKey(WG, t.wg, 2), second]]));
    addKeys(t.store, t.org, new Map([[epochKey(WG, 'other-wg', 5), second]]));
    expect(heldEpochs(t.store, t.org, t.wg)).toEqual([2, 1]);
    const old = await row(t, { task: OTHER });
    const seen: string[] = [];
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links/1/${old.tag}`, () => ({
      task_id: OTHER,
      session_id: SESSION,
      created_at: AT,
      sealed: old.sealed,
    }));
    const original = t.client.request.bind(t.client);
    t.client.request = async (method, target, options) => {
      seen.push(target);
      return original(method, target, options);
    };
    const found = await findLinks(t.context, t.org, t.wg, REF);
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ epoch: 1, taskId: OTHER, pull: REF });
    expect(seen.map((target) => target.split('/')[5])).toEqual(['2', '1']);
  });

  it('passes other hub errors on and refuses malformed rows', async () => {
    const t = await taskWorld();
    const tag = await prLinkTag(await prLinkKey(keyOf(t)), REF.repo, REF.number);
    const route = `GET /v1/workgroups/${t.wg}/pr-links/1/${tag}`;
    t.client.on(route, () => {
      throw new HubError('forbidden', 403);
    });
    await expect(findLinks(t.context, t.org, t.wg, REF)).rejects.toMatchObject({ code: 'forbidden' });
    t.client.on(route, () => {
      throw new Error('network');
    });
    await expect(findLinks(t.context, t.org, t.wg, REF)).rejects.toThrow('network');
    t.client.on(route, () => ({ task_id: 'x' }));
    await expect(findLinks(t.context, t.org, t.wg, REF)).rejects.toMatchObject({ code: 'unavailable' });
  });
});

describe('linkPull', () => {
  it('links a pull request under the current epoch with a body the browser opens again', async () => {
    const t = await taskWorld();
    const tag = await prLinkTag(await prLinkKey(keyOf(t)), REF.repo, REF.number);
    t.client.on(listRoute(t), () => ({ items: [], next_cursor: null }));
    await loadLinks(t.context, t.org, t.wg, null);
    await loadLinks(t.context, t.org, t.wg, TASK);
    t.client.on(`POST /v1/workgroups/${t.wg}/pr-links`, (body) => {
      const sent = body as { epoch: number; tag: string; sealed: string; task_id: string };
      expect(sent).toMatchObject({ epoch: 1, tag, task_id: TASK });
      return {
        created: true,
        link: { ...sent, session_id: null, linked_by: t.me.id, created_at: AT },
      };
    });
    const outcome = await linkPull(t.context, t.org, t.wg, TASK, REF);
    expect(outcome).toMatchObject({ status: 'created', link: { pull: REF, linkedBy: t.me.id } });
    const state = t.store.getState().pulls;
    expect(linkList(state, listKey(t.wg, null)).ids).toHaveLength(1);
    expect(linkList(state, listKey(t.wg, TASK)).ids).toHaveLength(1);
    expect(state.lists[listKey(t.wg, OTHER)]).toBeUndefined();
  });

  it('shows a pull request linked already and changes nothing', async () => {
    const t = await taskWorld();
    const existing = await row(t, { task: OTHER });
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links/1/${existing.tag}`, () => existing);
    const outcome = await linkPull(t.context, t.org, t.wg, TASK, REF);
    expect(outcome).toMatchObject({ status: 'exists', links: [{ taskId: OTHER }] });
    expect(t.client.of(`POST /v1/workgroups/${t.wg}/pr-links`)).toHaveLength(0);
  });

  it('takes the answer for a tag linked meanwhile', async () => {
    const t = await taskWorld();
    const meanwhile = await row(t, { task: OTHER });
    t.client.on(`POST /v1/workgroups/${t.wg}/pr-links`, () => ({ created: false, link: meanwhile }));
    const outcome = await linkPull(t.context, t.org, t.wg, TASK, REF);
    expect(outcome).toMatchObject({ status: 'exists', links: [{ taskId: OTHER, pull: REF }] });
    expect(t.store.getState().pulls.links).toEqual({});
  });

  it('refuses without write access, a missing key and a malformed answer', async () => {
    const t = await taskWorld();
    await expect(linkPull(t.context, t.org, 'other-wg', TASK, REF)).rejects.toMatchObject({ code: 'forbidden' });
    await expect(linkPull(t.context, 'other-org', t.wg, TASK, REF)).rejects.toBeInstanceOf(LinkError);
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(linkPull(t.context, t.org, t.wg, TASK, REF)).rejects.toMatchObject({ code: 'forbidden' });
    const u = await taskWorld();
    u.client.on(`POST /v1/workgroups/${u.wg}/pr-links`, () => ({ created: 'yes' }));
    await expect(linkPull(u.context, u.org, u.wg, TASK, REF)).rejects.toMatchObject({ code: 'unavailable' });
    u.client.on(`POST /v1/workgroups/${u.wg}/pr-links`, () => null);
    await expect(linkPull(u.context, u.org, u.wg, TASK, REF)).rejects.toMatchObject({ code: 'unavailable' });
    u.client.on(`POST /v1/workgroups/${u.wg}/pr-links`, () => ({ created: true, link: { task_id: TASK } }));
    await expect(linkPull(u.context, u.org, u.wg, TASK, REF)).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('asks the log job once for the current key, then refuses with epoch_stale', async () => {
    const t = await taskWorld();
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, keys: new Map() } },
    }));
    await expect(linkPull(t.context, t.org, t.wg, TASK, REF)).rejects.toMatchObject({ code: 'epoch_stale' });
    expect(t.context.refreshLog).toHaveBeenCalledTimes(1);
    t.context.refreshLog.mockImplementation(async () => {
      addKeys(t.store, t.org, new Map([[epochKey(WG, t.wg, 1), keyOf(t)]]));
      return true;
    });
    t.client.on(`POST /v1/workgroups/${t.wg}/pr-links`, (body) => ({
      created: true,
      link: { ...(body as object), session_id: null, linked_by: t.me.id, created_at: AT },
    }));
    expect(await linkPull(t.context, t.org, t.wg, TASK, REF)).toMatchObject({ status: 'created' });
  });
});

describe('unlinkPull', () => {
  const link = (t: TaskWorld, linkedBy: string | null): PullLink => ({
    wg: t.wg,
    epoch: 1,
    tag: 'tag_1',
    taskId: TASK,
    sessionId: linkedBy === null ? SESSION : null,
    linkedBy,
    createdAt: AT,
    pull: REF,
    code: null,
  });

  it("removes a person's link (404 counts as done) and refuses a device's", async () => {
    const t = await taskWorld();
    const route = `DELETE /v1/workgroups/${t.wg}/pr-links/1/tag_1`;
    t.client.on(route, () => ({ deleted: true }));
    t.client.on(listRoute(t), () => ({ items: [], next_cursor: null }));
    await loadLinks(t.context, t.org, t.wg, null);
    const own = link(t, t.me.id);
    t.store.update('pulls', (state) => ({
      ...state,
      links: { [`${t.wg} 1 tag_1`]: own },
      lists: {
        ...state.lists,
        [listKey(t.wg, null)]: { ...state.lists[listKey(t.wg, null)]!, ids: [`${t.wg} 1 tag_1`] },
      },
    }));
    await unlinkPull(t.context, own);
    expect(t.store.getState().pulls.links).toEqual({});
    expect(linkList(t.store.getState().pulls, listKey(t.wg, null)).ids).toEqual([]);
    t.client.on(route, () => {
      throw new HubError('not_found', 404);
    });
    await unlinkPull(t.context, own);
    t.client.on(route, () => {
      throw new HubError('forbidden', 403);
    });
    await expect(unlinkPull(t.context, own)).rejects.toMatchObject({ code: 'forbidden' });
    t.client.on(route, () => {
      throw new Error('network');
    });
    await expect(unlinkPull(t.context, own)).rejects.toThrow('network');
    const refused = await unlinkPull(t.context, link(t, null)).catch((error: unknown) => error);
    expect(linkErrorCode(refused)).toBe('id_conflict');
    expect(linkErrorCode(new HubError('forbidden', 403))).toBe('forbidden');
  });
});
