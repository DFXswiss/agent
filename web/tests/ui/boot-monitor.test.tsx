// U-WEB-ui-boot-monitor: the long-task early warning is a lazy chunk; a chunk that cannot be loaded leaves the page
// without it – the start goes on and nothing is thrown or left unhandled.
import { waitFor } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { boot } from '../../src/ui/app/boot';
import { FakeAuthenticator } from '../core/account/authenticator';
import { CONFIG, ORIGIN } from '../core/account/hub';

const chunk = vi.hoisted(() => ({ requested: 0 }));
vi.mock('../../src/core/monitoring/longtasks', () => {
  chunk.requested += 1;
  throw new Error('chunk load failed');
});

describe('boot without the long-task chunk', () => {
  it('starts the page when the monitor chunk cannot be loaded', async () => {
    const unhandled = vi.fn();
    process.on('unhandledRejection', unhandled);
    const container = document.createElement('div');
    window.history.replaceState(null, '', '/login');
    const result = await boot({
      container,
      fetch: async () => Response.json(CONFIG),
      rootKey: CONFIG.platform_root_key,
      origin: ORIGIN,
      languages: ['en'],
      credentials: new FakeAuthenticator(),
      indexedDB: new IDBFactory(),
      storage: null,
      missing: async () => [],
    });
    expect(result.ok).toBe(true);
    await waitFor(() => expect(container.textContent).toContain('Sign in with passkey'));
    expect(chunk.requested).toBeGreaterThan(0);
    await new Promise((resolve) => setTimeout(resolve, 0));
    process.off('unhandledRejection', unhandled);
    expect(unhandled).not.toHaveBeenCalled();
  });
});
