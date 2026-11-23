// A signed-in user with an identity and a scriptable client for the agent flows (`core/agents`).
import { vi } from 'vitest';
import { Identity } from '../../../src/core/crypto/keys';
import { createStore } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';

export const USER = '01a1ee56-5e00-77be-8cf3-1f27bb0e92a2';
export const AGENT = '01a1ee56-5e00-7242-9e24-f87629ad2977';
export const OTHER = '01a1ee56-5e00-79bc-ad68-1fafbfc5d917';

export type Handler = (method: string, target: string, body: unknown) => unknown;

export async function agentWorld(handler: Handler = () => ({})) {
  const store = createStore();
  const identity = await Identity.generate();
  setSession(store, USER, { keyId: 'session.x', expiresAt: Date.now() + 3_600_000 });
  setIdentity(store, identity);
  const request = vi.fn(async (method: string, target: string, options: { body?: unknown } = {}) =>
    handler(method, target, options.body),
  );
  return { store, identity, client: { request }, context: { store, client: { request } } };
}
