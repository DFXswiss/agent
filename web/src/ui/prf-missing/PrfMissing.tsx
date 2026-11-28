// `prf_missing` guidance (06-web.md section 3.3, 04-crypto.md section 5.4), shown by the sign-in paths (`/login`, the
// existing-account step of org invitations) when the passkey returned no usable PRF output and this browser has no
// device key for the user: (1) the explanation; (2a) the recovery code unlocks this once – the slot comes from the
// signed `GET /v1/me/bundles/recovery`, nothing is rotated and the code stays valid – optionally with (2b) "Remember
// this browser" (a non-extractable device key in IndexedDB encrypts the identity for the next sign-in here); (2c) the
// hint to add a PRF-capable passkey from another signed-in device. A PRF-capable passkey whose slot is missing gets
// its slot uploaded once the identity is confirmed.
import { type FormEvent, useState } from 'react';
import { type SignIn, unlockWithRecoveryCode } from '../../core/account/login';
import type { Identity } from '../../core/crypto/keys';
import type { DeviceKeys } from '../../core/persist/device-key';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { errorCode } from '../shell/errors';
import { deviceKeys, rememberBrowser, uploadMissingSlot } from './device';

export interface PrfMissingProps {
  readonly signIn: SignIn;
  /** Unlocks the app with the opened identity (`AppSession.unlock`: checks it against the registered keys). */
  readonly unlock: (identity: Identity) => Promise<void>;
  /** Called once unlocked (and remembered, if chosen). */
  readonly onUnlocked: () => void;
  /** "Sign out" (leave the guidance); not offered without. */
  readonly onCancel?: () => void;
  readonly keys?: () => Promise<DeviceKeys>;
}

const PRIMARY = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';

export function PrfMissing({ signIn, unlock, onUnlocked, onCancel, keys = deviceKeys }: PrfMissingProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const [code, setCode] = useState('');
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    let identity: Identity;
    try {
      identity = await unlockWithRecoveryCode(signIn.userId, code, await app.recoveryBundle());
      await unlock(identity);
    } catch (caught) {
      setBusy(false);
      setNotice(error(errorCode(caught)).title);
      return;
    }
    // a PRF-capable passkey without slot: upload it now that the identity is confirmed
    await uploadMissingSlot(app, signIn, identity);
    // remembered, this browser also stays signed in (rule E-40: the same device key)
    if (remember && (await rememberBrowser(signIn.userId, identity, keys))) app.noteRemembered(true);
    onUnlocked();
  };

  return (
    <form className="flex flex-col gap-3" onSubmit={(event) => void submit(event)} data-testid="prf-missing.page">
      <p role="alert" className="font-semibold" data-testid="prf-missing.explain">
        {t('passkeys.prf-missing.explain')}
      </p>
      <p>{t('passkeys.prf-missing.options')}</p>
      <h2 className="text-lg font-semibold">{t('passkeys.prf-missing.recovery.title')}</h2>
      <label htmlFor="prf-missing-code">{t('passkeys.prf-missing.recovery.label')}</label>
      <input
        id="prf-missing-code"
        className="min-h-11 rounded border border-gray-400 px-3 font-mono uppercase"
        autoComplete="off"
        spellCheck={false}
        value={code}
        onChange={(event) => setCode(event.target.value)}
        data-testid="prf-missing.recovery.input"
      />
      <p className="text-sm text-gray-700">{t('passkeys.prf-missing.recovery.hint')}</p>
      <label className="flex min-h-11 items-center gap-2">
        <input
          type="checkbox"
          className="h-5 w-5"
          checked={remember}
          onChange={(event) => setRemember(event.target.checked)}
          data-testid="prf-missing.remember"
        />
        <span>{t('passkeys.prf-missing.remember')}</span>
      </label>
      <p className="text-sm text-gray-700">{t('passkeys.prf-missing.remember.hint')}</p>
      <button type="submit" className={PRIMARY} disabled={busy} data-testid="prf-missing.recovery.submit">
        {busy ? t('passkeys.prf-missing.recovery.working') : t('passkeys.prf-missing.recovery.submit')}
      </button>
      {notice !== null && (
        <p role="alert" className="text-red-800" data-testid="prf-missing.error">
          {notice}
        </p>
      )}
      <h2 className="text-lg font-semibold">{t('passkeys.prf-missing.other.title')}</h2>
      <p data-testid="prf-missing.other-device">{t('passkeys.prf-missing.other')}</p>
      {onCancel !== undefined && (
        <button
          type="button"
          className="min-h-11 self-start underline"
          onClick={onCancel}
          disabled={busy}
          data-testid="prf-missing.cancel"
        >
          {t('passkeys.prf-missing.cancel')}
        </button>
      )}
    </form>
  );
}
