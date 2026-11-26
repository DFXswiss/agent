// U-WEB-audit-filters: the filter state (URL query, hub query, time filter), rows, pages, the verification and the
// event links of the audit view (06-web.md section 9.2, 03-api.md section 4.11).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  ACTION_GROUPS,
  AUDIT_ACTIONS,
  AUDIT_PAGE,
  actorName,
  targetName,
  auditTarget,
  eventLink,
  filterQuery,
  hubQuery,
  inTime,
  agentMarker,
  isAuditRow,
  NO_FILTERS,
  parseAuditPage,
  parseFilters,
  parseVerify,
  pastRange,
} from '../../../src/core/monitoring/audit';
import { HubError } from '../../../src/core/net/signedFetch';
import { auditRow, DEV, iso, ORG, S1, T0, TASK, WG } from './fixtures';

describe('actions', () => {
  it('equals the closed list of the contracts', () => {
    const file = join(import.meta.dirname, '../../../../contracts/enums/audit-actions.json');
    const contract = (JSON.parse(readFileSync(file, 'utf8')) as { actions: { action: string }[] }).actions;
    expect([...AUDIT_ACTIONS]).toEqual(contract.map((item) => item.action).sort());
    expect(ACTION_GROUPS).toContain('log');
    expect(ACTION_GROUPS).toContain('read');
    expect([...ACTION_GROUPS]).toEqual([...ACTION_GROUPS].sort());
  });
});

describe('filter state', () => {
  it('reads the URL query and drops malformed values', () => {
    expect(parseFilters(new URLSearchParams(''))).toEqual(NO_FILTERS);
    const query = new URLSearchParams({
      action: 'operation',
      actor: ORG,
      wg: WG,
      task: 'not-a-uuid',
      session: S1,
      on_behalf_of: ORG,
      from: iso(T0),
      to: 'later',
    });
    expect(parseFilters(query)).toEqual({
      action: 'operation',
      actor: ORG,
      wg: WG,
      task: null,
      session: S1,
      on_behalf_of: ORG,
      from: T0,
      to: null,
    });
    expect(parseFilters(new URLSearchParams({ on_behalf_of: 'someone' })).on_behalf_of).toBeNull();
    expect(parseFilters(new URLSearchParams({ action: 'Bad Action', from: '' })).action).toBeNull();
    expect(parseFilters(new URLSearchParams({ action: 'a'.repeat(97) })).action).toBeNull();
  });

  it('writes the URL query and the hub query without the time filter', () => {
    const filters = { ...NO_FILTERS, action: 'log', task: TASK, from: T0, to: T0 + 1000 };
    expect(filterQuery(filters)).toBe(
      `action=log&task=${TASK}&from=${encodeURIComponent(iso(T0))}&to=${encodeURIComponent(iso(T0 + 1000))}`,
    );
    expect(parseFilters(new URLSearchParams(filterQuery(filters)))).toEqual(filters);
    expect(hubQuery(filters)).toBe(`action=log&task=${TASK}`);
    expect(filterQuery(NO_FILTERS)).toBe('');
  });

  it('builds the page requests', () => {
    expect(auditTarget(ORG, NO_FILTERS, null)).toBe(`/v1/orgs/${ORG}/audit?limit=${AUDIT_PAGE}`);
    expect(auditTarget(ORG, { ...NO_FILTERS, wg: WG }, 'cursor')).toBe(
      `/v1/orgs/${ORG}/audit?wg=${WG}&limit=${AUDIT_PAGE}&before=cursor`,
    );
  });

  it('applies the time filter and knows when paging cannot reach it', () => {
    const rows = [auditRow(3, T0 + 2000), auditRow(2, T0 + 1000), auditRow(1, T0)];
    const filters = { ...NO_FILTERS, from: T0 + 1000, to: T0 + 1500 };
    expect(rows.map((row) => inTime(row, filters))).toEqual([false, true, false]);
    expect(rows.every((row) => inTime(row, NO_FILTERS))).toBe(true);
    expect(pastRange(rows, filters)).toBe(true);
    expect(pastRange(rows.slice(0, 2), filters)).toBe(false);
    expect(pastRange([], filters)).toBe(false);
    expect(pastRange(rows, NO_FILTERS)).toBe(false);
  });
});

describe('rows and pages', () => {
  it('checks rows', () => {
    expect(isAuditRow(auditRow(1, T0))).toBe(true);
    expect(isAuditRow(auditRow(1, T0, { detail: { a: 'x', b: 2, c: true, d: null } }))).toBe(true);
    expect(isAuditRow(auditRow(1, T0, { detail: { a: {} } }))).toBe(false);
    expect(isAuditRow({ ...auditRow(1, T0), agent_id: '01a1ee56-5e00-7242-9e24-f87629ad2977' })).toBe(true);
    expect(isAuditRow({ ...auditRow(1, T0), agent_id: 7 })).toBe(false);
    const byAgent = { ...auditRow(1, T0), agent_id: 'a1' };
    const names = (id: string) => (id === 'a1' ? 'Claude Code' : null);
    expect(agentMarker(byAgent, names)).toBe('Claude Code');
    expect(agentMarker({ ...byAgent, agent_id: 'a2' }, names)).toBeNull();
    expect(agentMarker(auditRow(1, T0), names)).toBeUndefined();
    expect(isAuditRow(auditRow(1, T0, { detail: null }))).toBe(false);
    expect(isAuditRow(auditRow(1, T0, { seq: 1.5 }))).toBe(false);
    expect(isAuditRow(auditRow(1, T0, { at: 5 }))).toBe(false);
    expect(isAuditRow(auditRow(1, T0, { at: 'soon' }))).toBe(false);
    expect(isAuditRow(auditRow(1, T0, { result: null }))).toBe(false);
    expect(isAuditRow(auditRow(1, T0, { device_id: 4 }))).toBe(false);
    expect(isAuditRow(null)).toBe(false);
    expect(isAuditRow(auditRow(1, T0, { on_behalf_of: ORG }))).toBe(true);
    expect(isAuditRow(auditRow(1, T0, { on_behalf_of: null }))).toBe(false);
  });

  it('parses pages', () => {
    expect(parseAuditPage({ rows: [auditRow(2, T0), { seq: 'x' }], next_before: 'c' })).toEqual({
      rows: [auditRow(2, T0)],
      next: 'c',
      actors: {},
    });
    expect(parseAuditPage({ rows: [], next_before: null }).next).toBeNull();
    expect(() => parseAuditPage({ rows: {} })).toThrow(HubError);
    expect(() => parseAuditPage(null)).toThrow(HubError);
  });

  it('parses the verification', () => {
    const head = { seq: 9, hash: 'a'.repeat(64) };
    const checkpoint = { hub_key_id: 'hub-1', seq: 8, hash: 'b'.repeat(64), published_at: iso(T0), signature: 'sig' };
    expect(parseVerify({ ok: true, head, checkpoint })).toEqual({
      ok: true,
      head,
      checkpoint: { hub_key_id: 'hub-1', seq: 8, hash: 'b'.repeat(64), published_at: iso(T0) },
    });
    expect(parseVerify({ ok: false, head, checkpoint: null })).toEqual({ ok: false, head, checkpoint: null });
    for (const bad of [
      null,
      { ok: 'yes', head, checkpoint: null },
      { ok: true, head: { seq: 1 }, checkpoint: null },
      { ok: true, head, checkpoint: { ...checkpoint, hub_key_id: 1 } },
      { ok: true, head, checkpoint: { ...checkpoint, published_at: null } },
      { ok: true, head, checkpoint: 'x' },
    ]) {
      expect(() => parseVerify(bad)).toThrow(HubError);
    }
  });

  it('links the referenced event to its session or task', () => {
    const row = (extra: Record<string, unknown>) => parseAuditPage({ rows: [auditRow(1, T0, extra)] }).rows[0]!;
    expect(eventLink(ORG, row({ event_id: 'e', session_id: S1, task_id: TASK }))).toBe(`/o/${ORG}/s/${S1}`);
    expect(eventLink(ORG, row({ event_id: 'e', task_id: TASK }))).toBe(`/o/${ORG}/t/${TASK}`);
    expect(eventLink(ORG, row({ event_id: 'e', device_id: DEV }))).toBeNull();
    expect(eventLink(ORG, row({ task_id: TASK }))).toBeNull();
  });
});

describe('names', () => {
  const names = {
    member: (id: string) => (id === ORG ? 'Ada' : null),
    device: (id: string) => (id === DEV ? 'Mac' : null),
  };
  const row = (extra: Record<string, unknown>) => parseAuditPage({ rows: [auditRow(1, T0, extra)] }).rows[0]!;

  it('names users and devices as actors', () => {
    expect(actorName(row({}), names)).toBe('Ada');
    expect(actorName(row({ actor_kind: 'device', actor_id: DEV }), names)).toBe('Mac');
    expect(actorName(row({ actor_kind: 'hub', actor_id: null }), names)).toBeNull();
    expect(actorName(row({ actor_kind: 'session', actor_id: S1 }), names)).toBeNull();
  });

  it('names user and device targets', () => {
    expect(targetName(row({ target_kind: 'user', target_id: ORG }), names)).toBe('Ada');
    expect(targetName(row({ target_kind: 'device', target_id: DEV }), names)).toBe('Mac');
    expect(targetName(row({ target_kind: 'invite', target_id: 'inv' }), names)).toBeNull();
    expect(targetName(row({}), names)).toBeNull();
  });
});
