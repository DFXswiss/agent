// The user's connected agents (11-agent-access.md sections 4 and 5.3): list with opened labels (unknown agent when a
// label does not open), the agent each one replaced, the main orchestrator, malformed rows left out, failures with
// their code, revoking one and all with the log clean-up right after it; the fields of an agent with its own identity
// (ADR-082).
import { describe, expect, it } from 'vitest';
import { agentChanges, fetchAgentRows, loadAgents } from '../../../src/core/agents/list';
import { revokeAgent, revokeAllAgents } from '../../../src/core/agents/revoke';
import { sealLabel } from '../../../src/core/crypto/agent';
import { Identity } from '../../../src/core/crypto/keys';
import { HubError } from '../../../src/core/net/signedFetch';
import { agentName, mainOrchestrator, remoteOrchestrator } from '../../../src/core/store/slices/agents';
import { setIdentity } from '../../../src/core/store/slices/me';
import { ensureOrg } from '../../../src/core/store/slices/orgs';
import { AGENT, agentWorld, OTHER, USER } from './world';

const T = '2026-11-23T12:00:00.000Z';
const row = (agent_id: string, label_ct: string, extra: Record<string, unknown> = {}) => ({
  agent_id,
  state: 'active',
  label_ct,
  created_at: T,
  confirmed_at: T,
  claimed_at: T,
  last_used_at: null,
  revoked_at: null,
  replaces: null,
  ...extra,
});

describe('loadAgents', () => {
  it('opens the labels and keeps unknown agents', async () => {
    let agents: unknown[] = [];
    const w = await agentWorld(() => ({ agents }));
    const label = await sealLabel(w.identity.kemPublic(), AGENT, USER, { name: 'Claude Code', host: 'Mac' });
    const foreign = await sealLabel((await Identity.generate()).kemPublic(), OTHER, USER, { name: 'x', host: 'y' });
    agents = [
      row(AGENT, label, { replaces: OTHER }),
      row(OTHER, foreign, { state: 'revoked', revoked_at: T }),
      row('junk', label),
      row(OTHER, label, { replaces: 'passkey' }),
    ];
    await loadAgents(w.context);
    const entries = w.store.getState().agents.entries!;
    expect(entries.map((entry) => [entry.agentId, entry.label?.name ?? null, entry.replaces])).toEqual([
      [AGENT, 'Claude Code', OTHER],
      [OTHER, null, null],
    ]);
    expect(mainOrchestrator(entries)?.agentId).toBe(AGENT);
    expect(mainOrchestrator(entries, AGENT)).toBeNull();
    expect(mainOrchestrator(null)).toBeNull();
    expect(w.store.getState().agents.status).toBe('ready');
    expect(agentName(w.store.getState(), AGENT)).toBe('Claude Code');
    expect(agentName(w.store.getState(), OTHER)).toBeNull();
    expect(agentName(w.store.getState(), USER)).toBeNull();
    expect(entries.map((entry) => entry.remote)).toEqual([false, false]);
    expect(remoteOrchestrator(entries)).toBeNull();
  });

  it('marks agents paired from another computer; a row without the field is not remote (ADR-067)', async () => {
    let agents: unknown[] = [];
    const w = await agentWorld(() => ({ agents }));
    const label = await sealLabel(w.identity.kemPublic(), AGENT, USER, { name: 'Claude Code', host: 'server' });
    agents = [
      row(AGENT, label, { remote: true }),
      row(OTHER, label, { remote: false }),
      row(USER, label, { remote: 1 }),
    ];
    await loadAgents(w.context);
    const entries = w.store.getState().agents.entries!;
    expect(entries.map((entry) => [entry.agentId, entry.remote])).toEqual([
      [AGENT, true],
      [OTHER, false],
    ]);
    expect(remoteOrchestrator(entries)?.agentId).toBe(AGENT);
    expect(remoteOrchestrator(entries.slice(1))).toBeNull();
    expect(remoteOrchestrator(null)).toBeNull();
  });

  it('reads the fields of an agent with its own identity; a hub without them gives false and null (ADR-082)', async () => {
    let agents: unknown[] = [];
    const w = await agentWorld(() => ({ agents }));
    const label = await sealLabel(w.identity.kemPublic(), AGENT, USER, { name: 'Claude Code', host: 'Mac' });
    agents = [
      row(AGENT, label, { own_identity: true, held_identity: false, gen: 3, revoked_reason: null }),
      row(OTHER, label, { state: 'revoked', held_identity: true, gen: null, revoked_reason: 'replaced' }),
      row(USER, label),
      row(USER, label, { own_identity: 'yes' }),
      row(USER, label, { held_identity: 1 }),
      row(USER, label, { gen: 0 }),
      row(USER, label, { gen: 1.5 }),
      row(USER, label, { revoked_reason: 7 }),
      row(USER, label, { pub: 'x' }),
      row(USER, label, { pub: { kem: 'k' } }),
      row(USER, label, { cert: 1 }),
      row(USER, label, { pub: { kem: 'k', sig: 's' }, cert: 'c' }),
      row(USER, label, { pub: null, cert: null }),
    ];
    await loadAgents(w.context);
    const entries = w.store.getState().agents.entries!;
    expect(entries.map((e) => [e.agentId, e.ownIdentity, e.heldIdentity, e.gen, e.revokedReason])).toEqual([
      [AGENT, true, false, 3, null],
      [OTHER, false, true, null, 'replaced'],
      [USER, false, false, null, null],
      [USER, false, false, null, null],
      [USER, false, false, null, null],
    ]);
    expect((await fetchAgentRows(w.client)).map((r) => r.agent_id)).toEqual([AGENT, OTHER, USER, USER, USER]);
  });

  it('records failures with their code and drops answers after a sign-out', async () => {
    const w = await agentWorld(() => {
      throw new HubError('forbidden', 403);
    });
    await loadAgents(w.context);
    expect(w.store.getState().agents).toMatchObject({ status: 'error', code: 'forbidden' });
    const malformed = await agentWorld(() => ({ agents: 'x' }));
    await loadAgents(malformed.context);
    expect(malformed.store.getState().agents.code).toBe('unavailable');
    setIdentity(malformed.store, null);
    await loadAgents(malformed.context);
    expect(malformed.store.getState().agents.code).toBe('unauthorized');
    const gone = await agentWorld(() => {
      gone.store.reset();
      return { agents: [] };
    });
    await loadAgents(gone.context);
    expect(gone.store.getState().agents.entries).toBeNull();
    const failing = await agentWorld(() => {
      failing.store.reset();
      throw new Error('network');
    });
    await loadAgents(failing.context);
    expect(failing.store.getState().agents.status).toBe('idle');
  });
});

describe('revoking', () => {
  it('disconnects one or all, reloads and runs the log job of every org right after it', async () => {
    const calls: string[] = [];
    let answer: unknown = { revoked: 2 };
    const w = await agentWorld((method, target) => {
      calls.push(`${method} ${target}`);
      return method === 'GET' ? { agents: [] } : answer;
    });
    ensureOrg(w.store, 'org-b');
    ensureOrg(w.store, 'org-a');
    const refreshLog = async (org: string) => {
      calls.push(`log ${org}`);
      return true;
    };
    const context = { ...w.context, refreshLog };
    await revokeAgent(context, AGENT);
    expect(await revokeAllAgents(context)).toBe(2);
    answer = {};
    expect(await revokeAllAgents(context)).toBe(0);
    expect(calls.slice(0, 8)).toEqual([
      `DELETE /v1/me/agents/${AGENT}`,
      'GET /v1/me/agents',
      'log org-a',
      'log org-b',
      'DELETE /v1/me/agents',
      'GET /v1/me/agents',
      'log org-a',
      'log org-b',
    ]);
  });
});

describe('agentChanges', () => {
  const note = (id: string, kind: string, ref: string | null) => ({
    notification_id: id,
    kind,
    org_id: null,
    wg_id: null,
    task_id: null,
    ref,
    created_at: T,
    read_at: null,
  });

  it('counts the notifications of a remote pairing or a revoked agent (ADR-067 section 4.4)', () => {
    expect(agentChanges({})).toBe(0);
    const rows = {
      a: note('a', 'agent.account_change', `agent_paired_remote::${AGENT}`),
      b: note('b', 'agent.account_change', `agent_revoked:${OTHER}:${AGENT}`),
      c: note('c', 'agent.account_change', `passkey_removed:${OTHER}:x`),
      d: note('d', 'agent.account_change', null),
      e: note('e', 'whiteboard.entry', 'agent_paired_remote::x'),
    };
    expect(agentChanges(rows)).toBe(2);
  });
});
