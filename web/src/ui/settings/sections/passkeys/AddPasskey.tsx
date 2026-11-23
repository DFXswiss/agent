// "Add passkey" (06-web.md section 3.6, 04-crypto.md section 5.6) with the unlocked identity in memory: signed
// `register/begin {add:true}`, `create()` on this device or on another one (the platform's hybrid/QR and security-key
// UI), `register/finish`; then one confirmation with the new passkey (`get()` restricted to it, a sign-in that
// certifies a new session key and yields its PRF output) whose slot is uploaded, so the new passkey unlocks the keys
// on its own. The previous session is ended and this browser continues with the new one. Without PRF output the
// passkey still signs in; unlocking then needs the recovery code or this browser's device key.
import { useState } from 'react';
import { CeremonyCancelled } from '../../../../core/account/ceremony';
import { signIn } from '../../../../core/account/login';
import { credentialBytes, type PasskeyPlace, registerPasskey } from '../../../../core/account/passkeys';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';

type State =
  | { readonly step: 'idle'; readonly notice: string | null; readonly busy: boolean }
  | { readonly step: 'confirm'; readonly credentialId: string; readonly notice: string | null; readonly busy: boolean };

const BUTTON = 'min-h-11 rounded border border-gray-400 px-4 disabled:opacity-60';

export function AddPasskey() {
  const { app, credentials, origin } = useAppEnv();
  const { t, error } = useI18n();
  const identity = useStore((state) => state.me.identity, app.store);
  const [state, setState] = useState<State>({ step: 'idle', notice: null, busy: false });
  const failure = (caught: unknown) => {
    if (caught instanceof CeremonyCancelled) return t('passkeys.add.cancelled');
    // the authenticator already holds a passkey of this account (`excludeCredentials`)
    if (caught instanceof Error && caught.name === 'InvalidStateError') return t('passkeys.add.exists');
    return error(errorCode(caught)).title;
  };

  const add = async (place: PasskeyPlace) => {
    setState({ step: 'idle', notice: null, busy: true });
    try {
      const request = (method: string, target: string, options?: { body: unknown }) =>
        app.client.request(method, target, options);
      const credentialId = await registerPasskey(request, credentials, app.userId!, place);
      setState({ step: 'confirm', credentialId, notice: null, busy: false });
    } catch (caught) {
      setState({ step: 'idle', notice: failure(caught), busy: false });
    }
  };

  const confirm = async (current: Extract<State, { step: 'confirm' }>) => {
    setState({ ...current, notice: null, busy: true });
    try {
      const result = await signIn({
        anon: app.anon,
        credentials,
        origin,
        allowCredential: credentialBytes(current.credentialId),
      });
      if (result.userId !== app.userId || result.credentialId !== current.credentialId) {
        setState({ ...current, notice: t('passkeys.add.other'), busy: false });
        return;
      }
      // this browser continues with the session of the new passkey; the previous one ends
      await app.client.request('POST', '/v1/session/end', { body: {} }).catch(() => null);
      await app.begin(result);
      const slot = await app.uploadPasskeySlot(result, identity!);
      await app.loadMe();
      setState({ step: 'idle', notice: slot ? t('passkeys.add.done') : t('passkeys.add.done-no-prf'), busy: false });
    } catch (caught) {
      setState({ ...current, notice: failure(caught), busy: false });
    }
  };

  return (
    <div className="flex flex-col gap-3" data-testid="passkeys.add" data-step={state.step}>
      <h3 className="text-lg font-semibold">{t('passkeys.add.title')}</h3>
      {state.step === 'idle' ? (
        <>
          <p>{t('passkeys.add.explain')}</p>
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className={BUTTON}
              onClick={() => void add('this-device')}
              disabled={state.busy}
              data-testid="passkeys.add.this-device"
            >
              {t('passkeys.add.this-device')}
            </button>
            <button
              type="button"
              className={BUTTON}
              onClick={() => void add('other-device')}
              disabled={state.busy}
              data-testid="passkeys.add.other-device"
            >
              {t('passkeys.add.other-device')}
            </button>
          </div>
        </>
      ) : (
        <>
          <p>{t('passkeys.add.confirm.explain')}</p>
          <button
            type="button"
            className="min-h-11 self-start rounded bg-gray-900 px-4 text-white disabled:opacity-60"
            onClick={() => void confirm(state)}
            disabled={state.busy}
            data-testid="passkeys.add.confirm"
          >
            {t('passkeys.add.confirm')}
          </button>
        </>
      )}
      {state.notice !== null && (
        <p role="status" data-testid="passkeys.add.notice">
          {state.notice}
        </p>
      )}
    </div>
  );
}
