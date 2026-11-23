// The calm "being unlocked" line (ADR-058 section 2, `ui/unlock/Unlocking.tsx`): instead of the content of a workgroup
// an admin reads by inheritance before a key holder delivered its key, the dead-end variant once nobody with access is
// left, and the content itself as soon as the key is there.
import { act, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { epochKey } from '../../src/core/log/state';
import { Secret } from '../../src/core/crypto/secret';
import { setQueue } from '../../src/core/store/slices/jobs';
import { addKeys } from '../../src/core/store/slices/orgs';
import { Unlocking } from '../../src/ui/unlock/Unlocking';
import { idOf, T0 } from '../core/log/world';
import { orgWorld } from '../core/org/world';
import { mount } from './org-world';

const settled = { running: null, waiting: 0, last: { name: 'catch-up', ok: true, code: null, at: T0 } };

describe('being unlocked', () => {
  it('shows one calm line instead of the content until the key is there', async () => {
    const one = await idOf('wg one');
    const w = await orgWorld({
      as: 'bob',
      setup: async (world, a) => {
        await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
        // bob becomes admin after the workgroup exists: it reads it by inheritance, its key comes by catch-up
        await world.admitUser(a.ada, a.bob, 'member', {});
        await world.batch(a.ada, [{ type: 'role', subject: a.bob.id, role: 'admin' }], { keys: () => null });
      },
    });
    setQueue(w.store, w.org, settled);
    mount(
      w,
      [
        {
          path: '/',
          element: (
            <Unlocking org={w.org} wg={one}>
              <p data-testid="content">content</p>
            </Unlocking>
          ),
        },
      ],
      '/',
    );
    expect(screen.getByTestId('unlocking')).toHaveTextContent(
      'Being unlocked – the content appears as soon as a member or device of this workgroup has been online.',
    );
    expect(screen.getByTestId('unlocking')).toHaveAttribute('data-status', 'unlocking');
    expect(screen.queryByTestId('content')).toBeNull();
    act(() => addKeys(w.store, w.org, new Map([[epochKey('wg', one, 1), Secret.of(new Uint8Array(32))]])));
    expect(screen.getByTestId('content')).toBeInTheDocument();
    expect(screen.queryByTestId('unlocking')).toBeNull();
  });

  it('says honestly when nobody with access is left, and renders nothing without children', async () => {
    const one = await idOf('wg one');
    const w = await orgWorld({
      as: 'bob',
      setup: async (world, a) => {
        await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
        await world.admitUser(a.ada, a.bob, 'admin', {});
        await world.batch(a.ada, [{ type: 'ungrant', subject: a.ada.id, wg: one }]);
        await world.batch(a.ada, [{ type: 'remove', subject: a.ada.id }]);
      },
    });
    // the accept gave bob the key in the test world; the line is about a user without it
    w.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [w.org]: { ...orgs.byId[w.org]!, keys: new Map() } },
    }));
    setQueue(w.store, w.org, settled);
    mount(w, [{ path: '/', element: <Unlocking org={w.org} wg={one} /> }], '/');
    expect(screen.getByTestId('unlocking')).toHaveAttribute('data-status', 'lost');
    expect(screen.getByTestId('unlocking')).toHaveTextContent(
      'Older content of this workgroup can no longer be opened: nobody who could still share it is left. New content becomes readable for you shortly.',
    );
  });
});
