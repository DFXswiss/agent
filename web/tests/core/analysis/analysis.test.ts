// U-WEB-analysis (ADR-097 sections 1, 5 and 6): the period presets, the fixed title and prompt against the vectors of
// contracts/tasks/analysis.json (the same text as the agent tool), the analysis task through the ordinary send path,
// the recognition of an analysis task and "Adopt" into the workgroup or organization whiteboard as the person.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  adoptBoard,
  adoptLevels,
  adoptOutput,
  analysisOf,
  analysisPrompt,
  analysisTitle,
  DAY_MS,
  DEFAULT_DAYS,
  isAnalysis,
  PERIODS,
  period,
  prepareAnalysis,
  SCOPES,
  TEMPLATES,
} from '../../../src/core/analysis/analysis';
import { openEvent } from '../../../src/core/crypto/event';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { epochKey, ORG, WG } from '../../../src/core/log/state';
import { HubError } from '../../../src/core/net/signedFetch';
import { orgEntry } from '../../../src/core/store/slices/orgs';
import { setHubOffset } from '../../../src/core/store/slices/tasks';
import { sendTask } from '../../../src/core/tasks/send';
import { orgBoard, wgBoard } from '../../../src/core/whiteboards/location';
import { taskRow, taskWorld } from '../tasks/world';

interface Vectors {
  readonly templates: typeof TEMPLATES;
  readonly scopes: typeof SCOPES;
  readonly cases: readonly {
    readonly language: 'de' | 'en';
    readonly scope: 'org' | 'wg';
    readonly name: string | null;
    readonly since: string;
    readonly until: string;
    readonly title: string;
    readonly prompt: string;
  }[];
}

const VECTORS = JSON.parse(
  readFileSync(join(import.meta.dirname, '../../../../contracts/tasks/analysis.json'), 'utf8'),
) as Vectors;

describe('title and prompt', () => {
  it('carries the templates and scopes of the contract', () => {
    expect(TEMPLATES).toEqual(VECTORS.templates);
    expect(SCOPES).toEqual(VECTORS.scopes);
  });

  it.each(VECTORS.cases)('$language $scope $name', (vector) => {
    const analysis = { scope: vector.scope, since: vector.since, until: vector.until };
    expect(analysisTitle(vector.language)).toBe(vector.title);
    expect(analysisPrompt(vector.language, analysis, vector.name ?? 'ignored')).toBe(vector.prompt);
  });

  it('replaces in one pass: placeholders in a name stay', () => {
    const analysis = { scope: 'wg' as const, since: 'S', until: 'U' };
    expect(analysisPrompt('en', analysis, '{since} {until} {scope}')).toBe(
      'Optimize processes: analyse workgroup {since} {until} {scope} from S to U and propose improvements.',
    );
  });
});

describe('period', () => {
  it('offers 1, 7, 30 and 90 days, 7 by default, ending now on the hub clock', async () => {
    const t = await taskWorld();
    expect(PERIODS).toEqual([1, 7, 30, 90]);
    expect(DEFAULT_DAYS).toBe(7);
    t.clock.now = Date.parse('2026-11-26T10:00:00.000Z');
    expect(period(t.context, 7)).toEqual({ since: '2026-11-19T10:00:00.000Z', until: '2026-11-26T10:00:00.000Z' });
    setHubOffset(t.store, -1500);
    expect(period(t.context, 1)).toEqual({ since: '2026-11-25T09:59:58.500Z', until: '2026-11-26T09:59:58.500Z' });
    expect(DAY_MS).toBe(86_400_000);
  });
});

describe('prepareAnalysis', () => {
  it('seals the fixed title and prompt and puts the analysis into the body; sends like any task', async () => {
    const t = await taskWorld();
    t.clock.now = Date.parse('2026-11-26T08:30:00.000Z');
    const prepared = await prepareAnalysis(t.context, {
      org: t.org,
      wg: t.wg,
      name: 'Backend',
      scope: 'wg',
      days: 30,
      language: 'en',
    });
    const analysis = { scope: 'wg', since: '2026-10-27T08:30:00.000Z', until: '2026-11-26T08:30:00.000Z' };
    expect(prepared.body.analysis).toEqual(analysis);
    expect(prepared.title).toBe('Optimize processes');
    expect(prepared.prompt).toBe(VECTORS.cases[1]!.prompt);
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const [meta, prompt] = prepared.body.events;
    const sig = b64uDecode(t.me.identity.public().sig);
    expect(JSON.parse(fromUtf8(await openEvent(key, meta, sig)))).toEqual({ title: 'Optimize processes' });
    expect(JSON.parse(fromUtf8(await openEvent(key, prompt, sig)))).toEqual({
      text: VECTORS.cases[1]!.prompt,
      attachments: [],
    });
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    t.client.on('POST /v1/tasks', (body) => ({
      task: taskRow(t, { task_id: (body as { task_id: string }).task_id, analysis }),
      session: { session_id: 's1' },
      events: [],
    }));
    const sent = await sendTask(t.context, prepared);
    expect(isAnalysis(sent.task)).toBe(true);
    expect(t.client.of('POST /v1/tasks')[0]!.body).toMatchObject({ analysis });
  });

  it('writes the German prompt of the organization scope', async () => {
    const t = await taskWorld();
    t.clock.now = Date.parse('2026-11-26T00:00:00.000Z');
    const prepared = await prepareAnalysis(t.context, {
      org: t.org,
      wg: t.wg,
      name: 'Default',
      scope: 'org',
      days: 1,
      language: 'de',
    });
    expect(prepared.title).toBe('Prozesse optimieren');
    expect(prepared.prompt).toBe(VECTORS.cases[2]!.prompt);
    expect(prepared.body.analysis).toMatchObject({ scope: 'org' });
  });
});

describe('analysis of a task', () => {
  it('accepts a well-formed analysis only', () => {
    const good = { scope: 'org', since: 'a', until: 'b' };
    expect(analysisOf(good)).toEqual(good);
    expect(analysisOf({ ...good, scope: 'wg' })).toEqual({ ...good, scope: 'wg' });
    for (const bad of [
      null,
      undefined,
      'x',
      { ...good, scope: 'task' },
      { ...good, since: 1 },
      { ...good, until: null },
    ])
      expect(analysisOf(bad)).toBeNull();
    expect(isAnalysis({ analysis: good })).toBe(true);
    expect(isAnalysis({ analysis: null })).toBe(false);
    expect(isAnalysis({})).toBe(false);
  });
});

describe('adopt', () => {
  it('offers the boards the person may write: an admin both, a member the workgroup, nobody without the log', async () => {
    const admin = await taskWorld();
    const task = { org_id: admin.org, wg_id: admin.wg };
    const state = (t: typeof admin) => orgEntry(t.store.getState().orgs, t.org).log!.state;
    expect(adoptLevels(state(admin), admin.me.id, task)).toEqual(['workgroup', 'org']);
    expect(adoptLevels(null, admin.me.id, task)).toEqual([]);
    const member = await taskWorld({ member: true });
    expect(adoptLevels(state(member), member.me.id, { org_id: member.org, wg_id: member.wg })).toEqual(['workgroup']);
    expect(adoptBoard(task, 'org')).toEqual(orgBoard(admin.org));
    expect(adoptBoard(task, 'workgroup')).toEqual(wgBoard(admin.org, admin.wg));
  });

  it('writes the text as a new entry of the chosen board, sealed and signed by the person', async () => {
    const t = await taskWorld();
    const task = { org_id: t.org, wg_id: t.wg };
    t.client.on('POST /v1/events', () => ({ results: [{ scope_seq: 5 }] }));
    const report = '# Efficiency\n- Task 1';
    const wgEntry = await adoptOutput(t.context, task, 'workgroup', report);
    const orgEntryId = await adoptOutput(t.context, task, 'org', report);
    const [first, second] = t.client
      .of('POST /v1/events')
      .map((call) => (call.body as { events: { event: { header: Record<string, unknown> } }[] }).events[0]!);
    expect(first).not.toHaveProperty('notify');
    expect(first!.event.header).toMatchObject({
      kind: 'whiteboard.entry',
      scope: WG,
      wg: t.wg,
      task: '',
      ref: wgEntry,
    });
    expect(second!.event.header).toMatchObject({ kind: 'whiteboard.entry', scope: ORG, wg: '', ref: orgEntryId });
    const sig = b64uDecode(t.me.identity.public().sig);
    const key = t.world.held.get(t.me.id)!.get(epochKey(ORG, t.org, 1))!;
    expect(JSON.parse(fromUtf8(await openEvent(key, second!.event, sig)))).toEqual({ level: 'org', text: report });
    expect(second!.event.header.author).toBe(t.me.id);
  });

  it('is refused without the right to write the board', async () => {
    const t = await taskWorld({ member: true });
    await expect(adoptOutput(t.context, { org_id: t.org, wg_id: t.wg }, 'org', 'x')).rejects.toMatchObject({
      code: 'forbidden',
    });
    t.client.on('POST /v1/events', () => {
      throw new HubError('rate_limited', 429);
    });
    await expect(adoptOutput(t.context, { org_id: t.org, wg_id: t.wg }, 'workgroup', 'x')).rejects.toMatchObject({
      code: 'rate_limited',
    });
  });
});
