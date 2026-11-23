// Board locations: keys, scopes, the level of a header, membership of a header in a board, read and write rights on
// the three levels (org: member reads, admin writes; workgroup and task: wg read / wg write)
// and the board routes.
import { describe, expect, it } from 'vitest';
import {
  boardKey,
  boardPath,
  boardScope,
  canReadBoard,
  canWriteBoard,
  levelOfHeader,
  onBoard,
  orgBoard,
  taskBoard,
  wgBoard,
} from '../../../src/core/whiteboards/location';
import { boardWorld } from './world';

const ORG = '0190f000-0000-7000-8000-00000000000a';
const WG = '0190f000-0000-7000-8000-00000000000b';
const TASK = '0190f000-0000-7000-8000-00000000000c';

describe('locations', () => {
  it('builds keys, scopes and paths', () => {
    expect(boardKey(orgBoard(ORG))).toBe(`org ${ORG}  `);
    expect(boardKey(taskBoard(ORG, WG, TASK))).toBe(`task ${ORG} ${WG} ${TASK}`);
    expect(boardScope(orgBoard(ORG))).toEqual({ scope: 'org', scopeId: ORG });
    expect(boardScope(wgBoard(ORG, WG))).toEqual({ scope: 'wg', scopeId: WG });
    expect(boardPath(orgBoard(ORG))).toBe(`/o/${ORG}/whiteboard`);
    expect(boardPath(wgBoard(ORG, WG))).toBe(`/o/${ORG}/w/${WG}/whiteboard`);
    expect(boardPath(taskBoard(ORG, WG, TASK))).toBe(`/o/${ORG}/t/${TASK}?tab=whiteboard`);
  });

  it('derives the level from a header and places headers on boards', () => {
    expect(levelOfHeader({ scope: 'org', task: '' })).toBe('org');
    expect(levelOfHeader({ scope: 'wg', task: '' })).toBe('workgroup');
    expect(levelOfHeader({ scope: 'wg', task: TASK })).toBe('task');
    const org = { org: ORG, scope: 'org', scope_id: ORG, task: '' };
    const wg = { org: ORG, scope: 'wg', scope_id: WG, task: '' };
    const task = { org: ORG, scope: 'wg', scope_id: WG, task: TASK };
    expect(onBoard(org, orgBoard(ORG))).toBe(true);
    expect(onBoard({ ...org, org: WG }, orgBoard(ORG))).toBe(false);
    expect(onBoard(wg, orgBoard(ORG))).toBe(false);
    expect(onBoard(wg, wgBoard(ORG, WG))).toBe(true);
    expect(onBoard(task, wgBoard(ORG, WG))).toBe(false);
    expect(onBoard(task, taskBoard(ORG, WG, TASK))).toBe(true);
    expect(onBoard({ ...task, task: WG }, taskBoard(ORG, WG, TASK))).toBe(false);
    expect(onBoard({ ...task, scope_id: ORG }, taskBoard(ORG, WG, TASK))).toBe(false);
  });
});

describe('rights', () => {
  it('lets the admin write every board and a reader only read', async () => {
    const w = await boardWorld();
    const state = w.world.state;
    for (const location of [orgBoard(w.org), wgBoard(w.org, w.research), taskBoard(w.org, w.research, TASK)]) {
      expect(canReadBoard(state, w.ada.id, location)).toBe(true);
      expect(canWriteBoard(state, w.ada.id, location)).toBe(true);
    }
    expect(canReadBoard(state, w.bob.id, orgBoard(w.org))).toBe(true);
    expect(canWriteBoard(state, w.bob.id, orgBoard(w.org))).toBe(false);
    expect(canReadBoard(state, w.bob.id, wgBoard(w.org, w.research))).toBe(true);
    expect(canWriteBoard(state, w.bob.id, wgBoard(w.org, w.research))).toBe(false);
    expect(canWriteBoard(state, w.bob.id, taskBoard(w.org, w.wg, TASK))).toBe(true);
  });

  it('refuses without a state, a user, for another org and for strangers', async () => {
    const w = await boardWorld();
    const state = w.world.state;
    expect(canReadBoard(null, w.ada.id, orgBoard(w.org))).toBe(false);
    expect(canWriteBoard(state, null, orgBoard(w.org))).toBe(false);
    expect(canReadBoard(state, w.ada.id, orgBoard(ORG))).toBe(false);
    expect(canWriteBoard(state, w.ada.id, orgBoard(ORG))).toBe(false);
    expect(canReadBoard(state, w.devices[0]!.id, wgBoard(w.org, w.wg))).toBe(false);
  });
});
