// The bodies of the device request `github.pull_status` against contracts/requests/ (KOR-48): the request of 1-50
// distinct pull requests, every field of a found and of a missing `pull_state`, `done` and the failures of the kind –
// checked through `requests/bodies.ts`, where the module registers itself.
import { describe, expect, it } from 'vitest';
import { isPullState, PULL_CODES, PULLS_MAX, validPullRequest } from '../../../src/core/pulls/checks';
import { checkResponseBody, validRequestBody } from '../../../src/core/requests/bodies';

describe('github.pull_status', () => {
  const ORG = '0190f000-0000-7000-8000-0000000000e1';
  const WG = '0190f000-0000-7000-8000-0000000000e2';
  const request = { org_id: ORG, wg_id: WG, pulls: [{ repo: 'Org/Repo', number: 1 }] };
  const found = {
    repo: 'Org/Repo',
    number: 1,
    found: true,
    title: 'Fix it',
    state: 'open',
    draft: false,
    merged: false,
    mergeable: null,
    mergeable_state: 'unknown',
    head_ref: 'feature/x',
    head_sha: 'a'.repeat(40),
    head_repo: 'Org/Repo',
    base_ref: 'main',
    url: 'https://github.com/Org/Repo/pull/1',
    updated_at: '2026-11-24T12:00:00Z',
  };

  it('accepts the request bodies of the contract', () => {
    expect(validRequestBody('github.pull_status', request, false)).toBe(true);
    expect(validRequestBody('github.pull_status', { ...request, task_id: ORG }, false)).toBe(true);
    const many = Array.from({ length: PULLS_MAX }, (_, index) => ({ repo: 'a/b', number: index + 1 }));
    expect(validRequestBody('github.pull_status', { ...request, pulls: many }, false)).toBe(true);
    for (const bad of [
      { ...request, pulls: [] },
      { ...request, pulls: [...many, { repo: 'a/b', number: 99 }] },
      {
        ...request,
        pulls: [
          { repo: 'A/B', number: 1 },
          { repo: 'a/b', number: 1 },
        ],
      },
      { ...request, pulls: [{ repo: 'a', number: 1 }] },
      { ...request, pulls: [{ repo: 'a/b', number: 0 }] },
      { ...request, pulls: [{ repo: 'a/b', number: 2 ** 31 }] },
      { ...request, pulls: [{ repo: 'a/b', number: 1, x: 1 }] },
      { ...request, pulls: ['a/b#1'] },
      { ...request, pulls: 'a/b#1' },
      { ...request, org_id: 'org' },
      { ...request, wg_id: 'wg' },
      { ...request, task_id: 'task' },
      { ...request, extra: 1 },
      { org_id: ORG, wg_id: WG },
    ]) {
      expect(validRequestBody('github.pull_status', bad, false)).toBe(false);
    }
    expect(validRequestBody('github.pull_status', request, true)).toBe(false);
    expect(validPullRequest(request, false)).toBe(true);
  });

  it('accepts the states of found and missing pull requests', () => {
    expect(isPullState(found)).toBe(true);
    expect(isPullState({ ...found, mergeable: false, head_repo: null, state: 'closed', merged: true })).toBe(true);
    expect(isPullState({ ...found, head_ref: 'zweig/ä', updated_at: '2026-11-24T12:00:00.123Z' })).toBe(true);
    for (const code of PULL_CODES) expect(isPullState({ repo: 'a/b', number: 2, found: false, code })).toBe(true);
    for (const bad of [
      null,
      { ...found, repo: 'a' },
      { ...found, number: 0 },
      { ...found, found: 'yes' },
      { repo: 'a/b', number: 2, found: false, code: 'other' },
      { repo: 'a/b', number: 2, found: false, code: 'not_found', title: 'x' },
      { ...found, extra: 1 },
      { ...found, title: 5 },
      { ...found, title: 'x'.repeat(1025) },
      { ...found, state: 'merged' },
      { ...found, draft: 'no' },
      { ...found, merged: null },
      { ...found, mergeable: 'unknown' },
      { ...found, mergeable_state: 'Dirty' },
      { ...found, number: 2 ** 31 },
      { ...found, head_ref: 'a b' },
      { ...found, head_ref: 5 },
      { ...found, head_ref: '' },
      { ...found, head_ref: 'x'.repeat(201) },
      { ...found, head_ref: '-x' },
      { ...found, head_ref: 'a\u0001b' },
      { ...found, head_ref: 'a\u007fb' },
      { ...found, url: 'http://127.0.0.1:8080/Org/Repo/pull/1' },
      { ...found, updated_at: '2026-11-24' },
      { ...found, head_sha: 'abc' },
      { ...found, head_repo: 'fork' },
      { ...found, base_ref: '' },
      { ...found, url: 'http://github.com/x' },
      { ...found, updated_at: 5 },
    ]) {
      expect(isPullState(bad)).toBe(false);
    }
  });

  it('accepts done with the states and the failures of the kind', () => {
    expect(checkResponseBody('github.pull_status', { step: 'done', pulls: [found] })).not.toBeNull();
    expect(checkResponseBody('github.pull_status', { step: 'failed', code: 'github_not_connected' })).not.toBeNull();
    expect(checkResponseBody('github.pull_status', { step: 'failed', code: 'forbidden' })).not.toBeNull();
    for (const bad of [
      { step: 'done', pulls: [] },
      { step: 'done', pulls: Array.from({ length: PULLS_MAX + 1 }, () => found) },
      { step: 'done', pulls: [{ ...found, title: 1 }] },
      { step: 'done', pulls: 'x' },
      { step: 'done', pulls: [found], extra: 1 },
      { step: 'progress', stage: 'authorized' },
    ]) {
      expect(checkResponseBody('github.pull_status', bad)).toBeNull();
    }
  });
});
