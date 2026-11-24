// Workgroup settings (06-web.md section 8.1, 03-api.md section 9): form parsing with the repository regex and the
// pattern set, the newest verified `settings.workgroup` event, sealing and sending with the concurrent-edit check.
import { describe, expect, it } from 'vitest';
import { openEvent } from '../../../src/core/crypto/event';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { HubError } from '../../../src/core/net/signedFetch';
import type { EventEntry } from '../../../src/core/store/slices/events';
import { putEvents } from '../../../src/core/store/slices/events';
import { blockOrg, ensureOrg, orgEntry } from '../../../src/core/store/slices/orgs';
import { epochKey, WG } from '../../../src/core/log/state';
import {
  checkBody,
  DEFAULTS,
  parseSettings,
  REPO_RE,
  saveSettings,
  selectCurrent,
  seqOf,
  settingsOf,
  toInput,
} from '../../../src/core/wg-settings/settings';
import { taskWorld, type TaskWorld } from '../tasks/world';

const VALID = { repositories: ['dfx/agent'], protected_branches: ['main'], pr_draft_default: false };

describe('parseSettings', () => {
  it('lowercases and de-duplicates repositories, keeps patterns and the draft flag', () => {
    expect(
      parseSettings({
        repositories: ' DFX/Agent \n\ndfx/agent\nowner/name.js',
        protectedBranches: 'main\nrelease/*\nmain',
        prDraftDefault: false,
      }),
    ).toEqual({
      ok: true,
      settings: {
        repositories: ['dfx/agent', 'owner/name.js'],
        protected_branches: ['main', 'release/*'],
        pr_draft_default: false,
      },
    });
  });

  it('reports each invalid line and too long lists', () => {
    const result = parseSettings({
      repositories: 'ok/repo\nno-slash\nbad/re po',
      protectedBranches: 'ma in',
      prDraftDefault: true,
    });
    expect(result).toEqual({
      ok: false,
      errors: [
        { field: 'repositories', line: 2, code: 'repo_invalid', value: 'no-slash' },
        { field: 'repositories', line: 3, code: 'repo_invalid', value: 'bad/re po' },
        { field: 'protected_branches', line: 1, code: 'pattern_invalid', value: 'ma in' },
      ],
    });
    const many = Array.from({ length: 101 }, (_, i) => `b${i}`).join('\n');
    expect(parseSettings({ repositories: '', protectedBranches: many, prDraftDefault: true })).toEqual({
      ok: false,
      errors: [{ field: 'protected_branches', line: 0, code: 'too_many', value: '101' }],
    });
  });

  it('applies the regex of 03-api.md section 9', () => {
    expect(REPO_RE.test(`${'a'.repeat(39)}/${'b'.repeat(100)}`)).toBe(true);
    expect(REPO_RE.test(`${'a'.repeat(40)}/b`)).toBe(false);
    expect(REPO_RE.test(`a/${'b'.repeat(101)}`)).toBe(false);
    expect(REPO_RE.test('a_b/c')).toBe(false);
    expect(REPO_RE.test('a/b/c')).toBe(false);
  });

  it('round-trips through the form input', () => {
    expect(parseSettings(toInput(VALID))).toEqual({ ok: true, settings: VALID });
    expect(toInput(DEFAULTS).protectedBranches).toBe(''); // ADR-078: no default
  });
});

describe('checkBody', () => {
  it('accepts the contract shape only', () => {
    expect(checkBody(VALID)).toEqual(VALID);
    for (const bad of [
      null,
      { ...VALID, extra: 1 },
      { ...VALID, repositories: ['DFX/agent'] },
      { ...VALID, protected_branches: ['a b'] },
      { ...VALID, pr_draft_default: 'yes' },
      { ...VALID, repositories: 'dfx/agent' },
    ]) {
      expect(checkBody(bad)).toBeNull();
    }
  });
});

function entry(
  t: TaskWorld,
  seq: number,
  body: unknown,
  status: EventEntry['status'] = 'verified',
  kind = 'settings.workgroup',
): EventEntry {
  const header = { kind, author: t.me.id, id: `s${seq}` };
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status,
    code: null,
    header,
    body,
  };
}

describe('selectCurrent', () => {
  it('is the newest verified settings event; invalid bodies are reported', async () => {
    const t = await taskWorld();
    expect(selectCurrent(t.wg)(t.store.getState())).toEqual({ status: 'none' });
    putEvents(t.store, t.wg, [
      entry(t, 5, VALID),
      entry(t, 3, { ...VALID, pr_draft_default: true }),
      entry(t, 9, VALID, 'unverifiable'),
      entry(t, 8, { title: 'x' }, 'verified', 'task.meta'),
    ]);
    const current = selectCurrent(t.wg)(t.store.getState());
    expect(current).toEqual({ status: 'set', seq: 5, settings: VALID, author: t.me.id });
    expect(settingsOf(current)).toEqual(VALID);
    expect(seqOf(current)).toBe(5);
    putEvents(t.store, t.wg, [entry(t, 6, { junk: true })]);
    const invalid = selectCurrent(t.wg)(t.store.getState());
    expect(invalid).toEqual({ status: 'invalid', seq: 6 });
    expect(settingsOf(invalid)).toEqual(DEFAULTS);
    expect(seqOf({ status: 'none' })).toBe(0);
  });
});

describe('saveSettings', () => {
  it('seals the settings with the workgroup key and reads the own event back', async () => {
    const t = await taskWorld();
    t.client.on('POST /v1/events', (body) => {
      const event = (body as { events: { event: { header: { id: string } } }[] }).events[0]!.event;
      return { results: [{ id: event.header.id, scope_id: t.wg, scope_seq: 12, replayed: false }] };
    });
    expect(await saveSettings(t.context, { org: t.org, wg: t.wg, settings: VALID, base: 0 })).toBe(12);
    const sent = (
      t.client.of('POST /v1/events')[0]!.body as { events: { event: { header: Record<string, unknown> } }[] }
    ).events[0]!.event;
    expect(sent.header).toMatchObject({
      kind: 'settings.workgroup',
      scope: 'wg',
      scope_id: t.wg,
      wg: t.wg,
      task: '',
      author: t.me.id,
    });
    const key = orgEntry(t.store.getState().orgs, t.org).keys.get(epochKey(WG, t.wg, sent.header.epoch as number))!;
    expect(JSON.parse(fromUtf8(await openEvent(key, sent, t.me.identity.sigPublic())))).toEqual(VALID);
    expect(t.ingested).toEqual([
      { org: t.org, scope: t.wg, items: [{ scope_seq: 12, received_at: null, event: sent }] },
    ]);
  });

  it('refuses a save over a newer event unless told to overwrite', async () => {
    const t = await taskWorld();
    putEvents(t.store, t.wg, [entry(t, 5, VALID)]);
    t.client.on('POST /v1/events', (body) => {
      const event = (body as { events: { event: { header: { id: string } } }[] }).events[0]!.event;
      return { results: [{ id: event.header.id, scope_id: t.wg, scope_seq: 7 }] };
    });
    await expect(saveSettings(t.context, { org: t.org, wg: t.wg, settings: VALID, base: 3 })).rejects.toMatchObject({
      code: 'settings_conflict',
    });
    expect(await saveSettings(t.context, { org: t.org, wg: t.wg, settings: VALID, base: 3, overwrite: true })).toBe(7);
  });

  it('refuses readers, invalid bodies, missing keys, blocked and unknown orgs, and anonymous users', async () => {
    const reader = await taskWorld({ member: true });
    const other = [...reader.world.state.workgroups.keys()].find((wg) => wg !== reader.wg);
    await expect(
      saveSettings(reader.context, { org: reader.org, wg: other ?? 'none', settings: VALID, base: 0 }),
    ).rejects.toMatchObject({ code: 'forbidden' });
    const t = await taskWorld();
    await expect(
      saveSettings(t.context, { org: t.org, wg: t.wg, settings: { ...VALID, repositories: ['A/B'] }, base: 0 }),
    ).rejects.toMatchObject({ code: 'params_invalid' });
    const keyless = await taskWorld();
    keyless.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [keyless.org]: { ...orgs.byId[keyless.org]!, keys: new Map() } },
    }));
    await expect(
      saveSettings(keyless.context, { org: keyless.org, wg: keyless.wg, settings: VALID, base: 0 }),
    ).rejects.toMatchObject({
      code: 'epoch_stale',
    });
    ensureOrg(t.store, 'other-org');
    await expect(
      saveSettings(t.context, { org: 'other-org', wg: t.wg, settings: VALID, base: 0 }),
    ).rejects.toMatchObject({
      code: 'log_empty',
    });
    blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: null });
    await expect(saveSettings(t.context, { org: t.org, wg: t.wg, settings: VALID, base: 0 })).rejects.toMatchObject({
      code: 'log_fork',
    });
    t.store.reset();
    await expect(saveSettings(t.context, { org: t.org, wg: t.wg, settings: VALID, base: 0 })).rejects.toMatchObject({
      code: 'unauthorized',
    });
  });

  it('refuses an answer for another event', async () => {
    const t = await taskWorld();
    for (const answer of [{ results: [{ id: 'other', scope_seq: 1 }] }, { results: [] }, null]) {
      t.client.on('POST /v1/events', () => answer);
      await expect(saveSettings(t.context, { org: t.org, wg: t.wg, settings: VALID, base: 0 })).rejects.toBeInstanceOf(
        HubError,
      );
    }
  });
});
