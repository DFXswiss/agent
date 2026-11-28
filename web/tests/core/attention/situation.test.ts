// U-WEB-attention (the counters as filters of the task overview): each counter names the group behind its number,
// narrowed as the hub's summary counts it.
import { describe, expect, it } from 'vitest';
import { localMidnight } from '../../../src/core/attention/load';
import { isSituation, SITUATIONS, situationList } from '../../../src/core/attention/situation';
import { formatTime } from '../../../src/core/encoding/time';
import { NO_FILTER } from '../../../src/core/store/slices/attention';
import { T0 } from '../log/world';

describe('situations', () => {
  it('are the four counters in their order; anything else is none', () => {
    expect(SITUATIONS).toEqual(['mine', 'blocked', 'working', 'done']);
    for (const name of SITUATIONS) expect(isSituation(name)).toBe(true);
    for (const other of ['needs_decision', '', null, 1]) expect(isSituation(other)).toBe(false);
  });

  it('name the group behind each counter', () => {
    expect(situationList('mine', 'me', T0)).toEqual({
      category: 'needs_decision',
      filter: { owner: 'me', after: null },
    });
    expect(situationList('blocked', 'me', T0)).toEqual({ category: 'blocked', filter: NO_FILTER });
    expect(situationList('working', 'me', T0)).toEqual({ category: 'working', filter: NO_FILTER });
    expect(situationList('done', 'me', T0)).toEqual({
      category: 'done',
      filter: { owner: null, after: formatTime(localMidnight(T0)) },
    });
  });
});
