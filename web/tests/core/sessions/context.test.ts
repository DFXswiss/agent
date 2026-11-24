// U-WEB-core-session-context (KOR-33, ADR-059 sections 5 and 7): the resume outcome of a session row, tolerant of
// hubs that do not send `context_restore` and `context_reason`.
import { describe, expect, it } from 'vitest';
import { CONTEXT_REASONS, contextResume } from '../../../src/core/sessions/context';
import type { SessionEntry } from '../../../src/core/store/slices/tasks';

const entry = (fields: Record<string, unknown>): SessionEntry =>
  ({ row: { session_id: 's', ...fields }, transitions: [], estimatedAt: null }) as unknown as SessionEntry;

describe('contextResume', () => {
  it('reports nothing without a row or without the fields (older hub)', () => {
    expect(contextResume(null)).toBeNull();
    expect(contextResume(undefined)).toBeNull();
    expect(contextResume(entry({}))).toBeNull();
    expect(contextResume(entry({ context_restore: null, context_reason: null }))).toBeNull();
    expect(contextResume(entry({ context_restore: 'other' }))).toBeNull();
  });

  it('reports a restored context and what came back of the workspace', () => {
    const restored = (reason: unknown) => contextResume(entry({ context_restore: 'restored', context_reason: reason }));
    expect(restored(null)).toEqual({ kind: 'restored', workspace: 'full' });
    expect(restored(undefined)).toEqual({ kind: 'restored', workspace: 'full' });
    expect(restored('workspace_partial')).toEqual({ kind: 'restored', workspace: 'partial' });
    expect(restored('workspace_omitted')).toEqual({ kind: 'restored', workspace: 'omitted' });
    // a reason a restore does not carry, a later code or a malformed value: the conversation is still complete
    for (const reason of ['context_missing', 'workspace_later', 'toString', 7]) {
      expect(restored(reason)).toEqual({ kind: 'restored', workspace: 'full' });
    }
  });

  it.each(CONTEXT_REASONS)('reports a fallback because of %s', (reason) => {
    expect(contextResume(entry({ context_restore: 'fallback', context_reason: reason }))).toEqual({
      kind: 'fallback',
      reason,
    });
  });

  it('reports an unknown, missing or malformed reason as unknown', () => {
    for (const reason of ['something_new', null, undefined, 3, 'toString']) {
      expect(contextResume(entry({ context_restore: 'fallback', context_reason: reason }))).toEqual({
        kind: 'fallback',
        reason: 'unknown',
      });
    }
  });
});
