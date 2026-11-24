// Notification texts (06-web.md section 6.2): built client-side from the kind and decrypted names (fallbacks when not
// read yet), the click targets, and the names read from verified events (org, workgroup, task title, member or device
// as the subject of an accepted invitation).
import { describe, expect, it } from 'vitest';
import type { EventEntry } from '../../../src/core/store/slices/events';
import { putEvents } from '../../../src/core/store/slices/events';
import { createStore } from '../../../src/core/store';
import { setAgents } from '../../../src/core/store/slices/agents';
import {
  agentChange,
  AGENT_ACTIONS,
  attentionOf,
  type Names,
  notificationMessage,
  notificationNames,
  notificationTarget,
} from '../../../src/core/notifications/text';
import { notification } from './rows';

const ORG = '0190f000-0000-7000-8000-00000000000a';
const WG = '0190f000-0000-7000-8000-00000000000b';
const TASK = '0190f000-0000-7000-8000-00000000000c';
const USER = '0190f000-0000-7000-8000-00000000000d';
const DEVICE = '0190f000-0000-7000-8000-00000000000e';

const NAMES = { org: 'Acme', wg: 'Research', task: 'Fix the build', subject: 'Bob', agent: 'Claude Code' };
const FALLBACK = { org: 'an organization', wg: 'a workgroup', task: 'a task', subject: 'Someone', agent: 'an agent' };
const NONE = { org: null, wg: null, task: null, subject: null, agent: null };

describe('notificationMessage', () => {
  it('names the level of a whiteboard entry', () => {
    expect(notificationMessage(notification('n1'), NAMES, FALLBACK)).toEqual({
      key: 'notifications.text.whiteboard.org',
      params: { org: 'Acme' },
    });
    expect(notificationMessage(notification('n1', { wg_id: WG }), NAMES, FALLBACK)).toEqual({
      key: 'notifications.text.whiteboard.workgroup',
      params: { wg: 'Research', org: 'Acme' },
    });
    expect(notificationMessage(notification('n1', { wg_id: WG, task_id: TASK }), NAMES, FALLBACK)).toEqual({
      key: 'notifications.text.whiteboard.task',
      params: { task: 'Fix the build', org: 'Acme' },
    });
  });

  it('builds the other kinds and falls back for missing names', () => {
    const message = (kind: string, names: Names = NAMES) =>
      notificationMessage(notification('n1', { kind, task_id: TASK, wg_id: WG }), names, FALLBACK);
    expect(message('task.idle')).toEqual({
      key: 'notifications.text.task.idle',
      params: { task: 'Fix the build', org: 'Acme' },
    });
    expect(message('task.failed', NONE)).toEqual({
      key: 'notifications.text.task.failed',
      params: { task: 'a task', org: 'an organization' },
    });
    expect(message('invite.accepted', NONE)).toEqual({
      key: 'notifications.text.invite.accepted',
      params: { subject: 'Someone', org: 'an organization' },
    });
    expect(message('rotation.required')).toEqual({
      key: 'notifications.text.rotation.required',
      params: { org: 'Acme' },
    });
    expect(message('later.kind')).toEqual({ key: 'notifications.text.unknown', params: { org: 'Acme' } });
  });

  it('names the admitting admin of org.admitted, or falls back (ADR-060)', () => {
    const row = notification('n1', { kind: 'org.admitted', org_id: ORG, ref: USER });
    expect(notificationMessage(row, NAMES, FALLBACK)).toEqual({
      key: 'notifications.text.org.admitted',
      params: { subject: 'Bob', org: 'Acme' },
    });
    expect(notificationMessage(row, NONE, FALLBACK)).toEqual({
      key: 'notifications.text.org.admitted',
      params: { subject: 'Someone', org: 'an organization' },
    });
    expect(notificationTarget(row)).toBe(`/o/${ORG}`);
  });

  it('names the category and the reason of a task that needs attention (KOR-39)', () => {
    const message = (ref: string | null) =>
      notificationMessage(
        notification('n1', { kind: 'task.attention', task_id: TASK, wg_id: WG, ref }),
        NAMES,
        FALLBACK,
      );
    expect(message('needs_decision:question:3')).toEqual({
      key: 'notifications.text.task.attention.needs_decision',
      params: { task: 'Fix the build', org: 'Acme' },
      reason: 'attention.reason.question',
    });
    expect(message('blocked:device_unreachable:wg')).toEqual({
      key: 'notifications.text.task.attention.blocked',
      params: { task: 'Fix the build', org: 'Acme' },
      reason: 'attention.reason.device_unreachable',
    });
    expect(message('blocked:later_reason:4').reason).toBe('attention.generic.blocked');
    for (const ref of [null, 'working:working:1', 'needs_decision::1', '']) {
      expect(message(ref)).toEqual({
        key: 'notifications.text.task.attention',
        params: { task: 'Fix the build', org: 'Acme' },
      });
    }
    expect(attentionOf({ ref: 'needs_decision:approval:2' })).toEqual({
      category: 'needs_decision',
      reason: 'approval',
    });
  });
});

describe('notificationTarget', () => {
  it('leads to the board, the task or the members', () => {
    expect(notificationTarget(notification('n1', { org_id: ORG }))).toBe(`/o/${ORG}/whiteboard`);
    expect(notificationTarget(notification('n1', { org_id: ORG, wg_id: WG }))).toBe(`/o/${ORG}/w/${WG}/whiteboard`);
    expect(notificationTarget(notification('n1', { org_id: ORG, wg_id: WG, task_id: TASK }))).toBe(
      `/o/${ORG}/t/${TASK}?tab=whiteboard`,
    );
    expect(notificationTarget(notification('n1', { org_id: ORG, kind: 'task.idle', task_id: TASK }))).toBe(
      `/o/${ORG}/t/${TASK}`,
    );
    expect(notificationTarget(notification('n1', { org_id: ORG, kind: 'task.failed' }))).toBe(`/o/${ORG}`);
    expect(notificationTarget(notification('n1', { org_id: ORG, kind: 'invite.accepted' }))).toBe(`/o/${ORG}/members`);
    expect(notificationTarget(notification('n1', { org_id: ORG, kind: 'rotation.required' }))).toBe(
      `/o/${ORG}/members`,
    );
    expect(notificationTarget(notification('n1', { org_id: ORG, kind: 'later.kind' }))).toBe(`/o/${ORG}`);
    // the situation view (the org's home) holds the entry of a task that needs attention
    expect(notificationTarget(notification('n1', { org_id: ORG, kind: 'task.attention', task_id: TASK }))).toBe(
      `/o/${ORG}`,
    );
  });
});

let seq = 0;
function verified(scopeId: string, header: Record<string, unknown>, body: unknown, status = 'verified'): EventEntry {
  seq += 1;
  return {
    org: ORG,
    scopeId,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status: status as EventEntry['status'],
    code: null,
    header: status === 'verified' ? header : null,
    body,
  };
}

describe('notificationNames', () => {
  it('reads org, workgroup, task title and the accepted member from verified events', () => {
    const store = createStore();
    putEvents(store, ORG, [
      verified(ORG, { kind: 'org.profile', author: USER }, { name: 'Acme' }),
      verified(ORG, { kind: 'wg.profile', wg: WG, author: USER }, { name: 'Research' }),
      verified(ORG, { kind: 'member.profile', ref: USER, author: USER }, { display_name: 'Bob' }),
    ]);
    putEvents(store, WG, [verified(WG, { kind: 'task.meta', task: TASK, id: 't1' }, { title: 'Fix the build' })]);
    const state = store.getState();
    const accepted = notification('n1', { kind: 'invite.accepted', org_id: ORG, ref: USER });
    expect(notificationNames(state, accepted)).toEqual({
      org: 'Acme',
      wg: null,
      task: null,
      subject: 'Bob',
      agent: null,
    });
    const task = notification('n2', { kind: 'task.idle', org_id: ORG, wg_id: WG, task_id: TASK });
    expect(notificationNames(state, task)).toEqual({
      org: 'Acme',
      wg: 'Research',
      task: 'Fix the build',
      subject: null,
      agent: null,
    });
    const unknown = notification('n3', { kind: 'task.idle', org_id: ORG, wg_id: 'other', task_id: TASK });
    expect(notificationNames(state, unknown)).toMatchObject({ wg: null, task: null });
  });

  it('names a device by its newest verified label and nothing without events', () => {
    const store = createStore();
    const accepted = notification('n1', { kind: 'invite.accepted', org_id: ORG, ref: DEVICE });
    expect(notificationNames(store.getState(), accepted).subject).toBeNull();
    putEvents(store, ORG, [
      verified(ORG, { kind: 'device.profile', ref: DEVICE }, { label: 'Old' }),
      verified(ORG, { kind: 'device.profile', ref: DEVICE }, { label: 'Studio' }),
      verified(ORG, { kind: 'device.profile', ref: 'other' }, { label: 'Other' }),
      verified(ORG, { kind: 'device.profile', ref: DEVICE }, { nolabel: true }),
      verified(ORG, { kind: 'device.profile', ref: DEVICE }, { label: 'Forged' }, 'unverifiable'),
      verified(ORG, { kind: 'org.profile' }, { name: 'Acme' }),
    ]);
    expect(notificationNames(store.getState(), accepted).subject).toBe('Studio');
    expect(notificationNames(store.getState(), { ...accepted, ref: null }).subject).toBeNull();
  });
});

describe('notificationNames of org.admitted', () => {
  it('names the admitting admin from the org directory, never a device, and no subject for other kinds', () => {
    const store = createStore();
    putEvents(store, ORG, [
      verified(ORG, { kind: 'member.profile', ref: USER, author: USER }, { display_name: 'Ada' }),
      verified(ORG, { kind: 'device.profile', ref: DEVICE }, { label: 'Studio' }),
    ]);
    const state = store.getState();
    const admitted = (ref: string | null) => notification('n1', { kind: 'org.admitted', org_id: ORG, ref });
    expect(notificationNames(state, admitted(USER)).subject).toBe('Ada');
    expect(notificationNames(state, admitted(DEVICE)).subject).toBeNull();
    expect(notificationNames(state, admitted(null)).subject).toBeNull();
    const other = notification('n2', { kind: 'task.idle', org_id: ORG, ref: USER });
    expect(notificationNames(state, other).subject).toBeNull();
  });
});

describe('notificationNames caching', () => {
  it('returns the same object while events and row are unchanged', () => {
    const store = createStore();
    const row = notification('n1', { org_id: ORG });
    const first = notificationNames(store.getState(), row);
    expect(notificationNames(store.getState(), row)).toBe(first);
    putEvents(store, ORG, [verified(ORG, { kind: 'org.profile' }, { name: 'Acme' })]);
    expect(notificationNames(store.getState(), row)).not.toBe(first);
    expect(notificationNames(store.getState(), row).org).toBe('Acme');
  });
});

describe('account-level notifications of an agent (11-agent-access.md section 8)', () => {
  const AGENT = '0190f000-0000-7000-8000-0000000000a1';
  const change = (ref: string | null) => notification('n1', { kind: 'agent.account_change', org_id: null, ref });

  it('splits the reference into action, acting agent and target', () => {
    expect(agentChange(change(`passkey_removed:${AGENT}:cred`))).toEqual({
      action: 'passkey_removed',
      agent: AGENT,
      target: 'cred',
    });
    expect(agentChange(change(`agent_revoked:${AGENT}:a:b`)).target).toBe('a:b');
    expect(agentChange(change(null))).toEqual({ action: '', agent: '', target: '' });
  });

  it('names every action with the agent, unknown actions generically', () => {
    for (const action of AGENT_ACTIONS) {
      expect(notificationMessage(change(`${action}:${AGENT}:`), NAMES, FALLBACK)).toEqual({
        key: `notifications.text.agent.${action}`,
        params: { agent: 'Claude Code' },
      });
    }
    expect(notificationMessage(change(`odd:${AGENT}:`), NONE, FALLBACK)).toEqual({
      key: 'notifications.text.agent.unknown',
      params: { agent: 'an agent' },
    });
  });

  it('leads to the settings; an org-less row of another kind too', () => {
    expect(notificationTarget(change(`agent_revoked:${AGENT}:`))).toBe('/settings');
    expect(notificationTarget(notification('n2', { org_id: null }))).toBe('/settings');
  });

  it('reads the agent name from the opened labels, and refreshes when the list changes', () => {
    const store = createStore();
    const row = change(`agent_revoked:${AGENT}:`);
    expect(notificationNames(store.getState(), row)).toEqual(NONE);
    setAgents(store, [
      {
        agentId: AGENT,
        state: 'active',
        label: { name: 'Claude Code', host: 'Mac' },
        createdAt: 'x',
        confirmedAt: 'x',
        claimedAt: null,
        lastUsedAt: null,
        revokedAt: null,
        replaces: null,
        remote: true,
      },
    ]);
    expect(notificationNames(store.getState(), row).agent).toBe('Claude Code');
    // a confirmed pairing from another computer has no acting agent: it names the new agent, its target
    expect(notificationNames(store.getState(), change(`agent_paired_remote::${AGENT}`)).agent).toBe('Claude Code');
    expect(notificationNames(store.getState(), notification('n3', { org_id: null })).agent).toBeNull();
  });
});
