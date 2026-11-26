// KOR-52 (ADR-095 section 1): the automatic name of a main agent against every case of contracts/tasks/agent-name.json
// (code points, not UTF-16 units), the check of a typed name, and the handles.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  agentPath,
  AUTO_CODE_POINTS,
  AUTO_EMPTY,
  AUTO_WORDS,
  automaticName,
  checkName,
  mainHandle,
  NAME_MAX,
  subagentHandle,
  taskNumber,
} from '../../../src/core/agents/name';
import { WEB_DIR } from '../../helpers';

interface Vectors {
  readonly max_words: number;
  readonly max_code_points: number;
  readonly empty: string;
  readonly cases: readonly { readonly name: string; readonly title: string; readonly agent_name: string }[];
}

const vectors = JSON.parse(readFileSync(join(WEB_DIR, '../contracts/tasks/agent-name.json'), 'utf8')) as Vectors;

describe('automatic name', () => {
  it('uses the limits of the contract', () => {
    expect([AUTO_WORDS, AUTO_CODE_POINTS, AUTO_EMPTY]).toEqual([
      vectors.max_words,
      vectors.max_code_points,
      vectors.empty,
    ]);
    expect(vectors.cases.length).toBeGreaterThanOrEqual(12);
  });

  it.each(vectors.cases)('$name', ({ title, agent_name }) => {
    expect(automaticName(title)).toBe(agent_name);
  });

  it('counts code points: a cut never splits a surrogate pair', () => {
    const name = automaticName('😀'.repeat(45));
    expect([...name]).toHaveLength(AUTO_CODE_POINTS);
    expect(name).toBe('😀'.repeat(40));
  });
});

describe('typed name', () => {
  it('is trimmed and must be 1 to 80 code points on one line', () => {
    expect(checkName('  Login doctor \t')).toBe('Login doctor');
    expect(checkName('a'.repeat(NAME_MAX))).toBe('a'.repeat(80));
    expect(checkName('😀'.repeat(80))).toBe('😀'.repeat(80));
    expect(checkName('a'.repeat(81))).toBeNull();
    expect(checkName(' 　 ')).toBeNull();
    expect(checkName('')).toBeNull();
    for (const separator of ['\n', '\r', '\u2028', '\u0085']) expect(checkName(`one${separator}two`)).toBeNull();
  });
});

describe('handles', () => {
  it('formats main agents and subagents', () => {
    expect(mainHandle(42)).toBe('#42');
    expect(subagentHandle(42, '1.2')).toBe('#42.1.2');
  });

  it('takes only well-formed numbers and paths from the hub', () => {
    expect(taskNumber({ number: 7 })).toBe(7);
    expect(taskNumber({})).toBeNull();
    expect(taskNumber({ number: 0 })).toBeNull();
    expect(taskNumber({ number: 1.5 })).toBeNull();
    expect(taskNumber({ number: '7' })).toBeNull();
    expect(agentPath({ agent_path: '1.12.3' })).toBe('1.12.3');
    expect(agentPath({ agent_path: null })).toBeNull();
    expect(agentPath({})).toBeNull();
    for (const bad of ['', '0', '1.', '.1', '1.0', '01', 'a', 7]) expect(agentPath({ agent_path: bad })).toBeNull();
  });
});
