// The user's connected agents (11-agent-access.md sections 4 and 5.3): list with opened labels (unknown agent when a
// label does not open), the agent each one replaced, the main orchestrator, malformed rows left out, failures with
// their code, revoking one and all.
import { describe, expect, it } from 'vitest';
import { loadAgents, revokeAgent, revokeAllAgents } from '../../../src/core/agents/list';
import { sealLabel } from '../../../src/core/crypto/agent';
import { Identity } from '../../../src/core/crypto/keys';
import { HubError } from '../../../src/core/net/signedFetch';
import { agentName, mainOrchestrator } from '../../../src/core/store/slices/agents';
import { setIdentity } from '../../../src/core/store/slices/me';
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
  it('disconnects one or all and reloads', async () => {
    const calls: string[] = [];
    let answer: unknown = { revoked: 2 };
    const w = await agentWorld((method, target) => {
      calls.push(`${method} ${target}`);
      return method === 'GET' ? { agents: [] } : answer;
    });
    await revokeAgent(w.context, AGENT);
    expect(await revokeAllAgents(w.context)).toBe(2);
    answer = {};
    expect(await revokeAllAgents(w.context)).toBe(0);
    expect(calls.slice(0, 4)).toEqual([
      `DELETE /v1/me/agents/${AGENT}`,
      'GET /v1/me/agents',
      'DELETE /v1/me/agents',
      'GET /v1/me/agents',
    ]);
  });
});
