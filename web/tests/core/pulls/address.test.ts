// Pull-request addresses and the fixed instruction text (KOR-48, ADR-084 sections 1 and 3).
import { describe, expect, it } from 'vitest';
import { NUMBER_MAX, parsePullAddress, pullKey, pullLabel, pullUrl, samePull } from '../../../src/core/pulls/address';
import { fixConflictsInstruction, INSTRUCTION_VERSION } from '../../../src/core/pulls/instruction';

describe('parsePullAddress', () => {
  it('reads the address of a pull request and the short form', () => {
    expect(parsePullAddress('https://github.com/Example-Org/Example.Repo/pull/17')).toEqual({
      repo: 'Example-Org/Example.Repo',
      number: 17,
    });
    expect(parsePullAddress('  https://www.github.com/a/b/pull/3/files\n')).toEqual({ repo: 'a/b', number: 3 });
    expect(parsePullAddress('https://github.com/a/b/pull/3#discussion_r1')).toEqual({ repo: 'a/b', number: 3 });
    expect(parsePullAddress('https://github.com/a/b/pull/3?w=1')).toEqual({ repo: 'a/b', number: 3 });
    expect(parsePullAddress('owner/name_x#42')).toEqual({ repo: 'owner/name_x', number: 42 });
    expect(parsePullAddress(`a/b#${NUMBER_MAX}`)).toEqual({ repo: 'a/b', number: NUMBER_MAX });
  });

  it('refuses everything else', () => {
    for (const text of [
      '',
      'http://github.com/a/b/pull/1',
      'https://gitlab.com/a/b/pull/1',
      'https://github.com/a/b/issues/1',
      'https://github.com/a/b/pull/0',
      'https://github.com/a/b/pull/12abc',
      'https://github.com/a/b/pull/1 extra',
      'https://github.com.evil/a/b/pull/1',
      'a/b#0',
      'a/b#',
      'a#1',
      'a/b/c#1',
      'a_b/c#1',
      `${'o'.repeat(40)}/b#1`,
      'a/.#1',
      'a/..#1',
      `a/b#${NUMBER_MAX + 1}`,
    ]) {
      expect(parsePullAddress(text)).toBeNull();
    }
    // surrounding white space is no part of the address
    expect(parsePullAddress('a/b#1 ')).toEqual({ repo: 'a/b', number: 1 });
  });

  it('derives the key, the label and the address on GitHub', () => {
    const ref = { repo: 'Example-Org/Repo', number: 5 };
    expect(pullKey(ref)).toBe('example-org/repo#5');
    expect(pullLabel(ref)).toBe('Example-Org/Repo#5');
    expect(pullUrl(ref)).toBe('https://github.com/Example-Org/Repo/pull/5');
    expect(samePull(ref, { repo: 'example-org/REPO', number: 5 })).toBe(true);
    expect(samePull(ref, { repo: 'example-org/repo', number: 6 })).toBe(false);
  });
});

describe('fixConflictsInstruction', () => {
  it('is exactly the text v1 of the brief', () => {
    expect(INSTRUCTION_VERSION).toBe(1);
    expect(fixConflictsInstruction({ repo: 'Example-Org/Repo', number: 17 })).toBe(
      [
        'Platform instruction "fix merge conflicts" (v1) for pull request Example-Org/Repo#17:',
        '1. Call github.merge_base with {"repo": "Example-Org/Repo", "number": 17}.',
        '2. If the result says up_to_date, say so and stop.',
        '3. Resolve every listed conflict in the workspace directory of the repository, keeping the intent of both sides; remove every conflict marker; record a decision for every conflict without markers.',
        '4. If the repository has tests, run them with test.run on the resolved workspace and fix what the merge broke before you commit.',
        "5. Call github.commit with merge: true on the pull request's branch, then github.push of that commit to the same branch.",
        '6. Never merge, close or force-push the pull request.',
        '7. If a resolution needs a decision of a person, ask with human.question and wait for the answer.',
      ].join('\n'),
    );
    expect(fixConflictsInstruction({ repo: 'a/b', number: 1 }).endsWith('\n')).toBe(false);
  });
});
