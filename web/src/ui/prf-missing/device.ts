// The device keys of this browser profile for the screens (`core/persist/device-key.ts`): one `DeviceKeys` per
// IndexedDB factory, opened on first use. `unlockFromDevice` is the "Remember this browser" unlock of a sign-in
// without usable PRF output (06-web.md section 3.3 option b): null when this browser has no usable entry for the user;
// an entry that does not open is forgotten (it can never unlock again).
import { type SignIn, unlockWithPrf } from '../../core/account/login';
import { removePasskey } from '../../core/account/passkeys';
import { abandonRegistration } from '../../core/account/register';
import type { AppSession } from '../../core/account/session';
import type { Identity } from '../../core/crypto/keys';
import { DeviceKeys } from '../../core/persist/device-key';

const opened = new WeakMap<IDBFactory, Promise<DeviceKeys>>();

/** The device keys on `factory` (default: the page's IndexedDB; refused where there is none). */
export function deviceKeys(factory: IDBFactory | undefined = globalThis.indexedDB): Promise<DeviceKeys> {
  if (factory === undefined) return Promise.reject(new Error('no IndexedDB'));
  let keys = opened.get(factory);
  if (keys === undefined) {
    keys = DeviceKeys.open(factory);
    opened.set(factory, keys);
  }
  return keys;
}

/** The identity of `userId` from this browser's device key, or null. */
export async function unlockFromDevice(userId: string, keys: () => Promise<DeviceKeys> = deviceKeys) {
  let store: DeviceKeys;
  try {
    store = await keys();
  } catch {
    return null;
  }
  try {
    return await store.unlock(userId);
  } catch {
    await store.forget(userId).catch(() => undefined);
    return null;
  }
}

/**
 * The unlock of a sign-in in the order of 06-web.md section 3.3: the passkey slot with the PRF output (a slot that does
 * not open, `decrypt_failed`, counts as no PRF output; the `prf_missing` event is recorded), else this browser's device
 * key; null when neither unlocks (the caller shows the guidance).
 */
export async function unlockSignIn(app: AppSession, signIn: SignIn): Promise<Identity | null> {
  const identity = await unlockWithPrf(signIn).catch(() => null);
  app.notePrf(signIn, identity !== null);
  return identity ?? (await unlockFromDevice(signIn.userId));
}

/**
 * After an unlock without PRF output (device key, recovery code) of a PRF-capable passkey whose slot is missing: its
 * slot, so the next sign-in needs only the passkey. Best effort – a refusal leaves the unlock as it is.
 */
export async function uploadMissingSlot(app: AppSession, signIn: SignIn, identity: Identity): Promise<void> {
  if (signIn.prf === null || signIn.bundleCt !== null) return;
  await app.uploadPasskeySlot(signIn, identity).catch(() => false);
}

/** "Remember this browser" for `userId`; false if the browser refused (the caller says so). */
export async function rememberBrowser(
  userId: string,
  identity: Identity,
  keys: () => Promise<DeviceKeys> = deviceKeys,
): Promise<boolean> {
  try {
    await (await keys()).remember(userId, identity);
    return true;
  } catch {
    return false;
  }
}

/**
 * A registration whose new passkey gave no PRF output (KOR-22): the registration is withdrawn at the hub
 * (`abandonRegistration`) and this browser signs out; true if the invitation is free again at once.
 */
export async function withdrawRegistration(app: AppSession): Promise<boolean> {
  const released = await abandonRegistration((method, target, options) => app.client.request(method, target, options));
  await app.signOut();
  return released;
}

/**
 * Removes a passkey that was not accepted (no PRF output; KOR-22); the hub ends its sessions as well. False if the hub
 * refused or could not be reached – the caller says so (the passkey then stays on the account).
 */
export async function rejectPasskey(app: AppSession, credentialId: string): Promise<boolean> {
  const request = (method: string, target: string, options?: { body: unknown }) =>
    app.client.request(method, target, options);
  return removePasskey(request, credentialId).then(
    () => true,
    () => false,
  );
}
