// `/login` (06-web.md section 3.4, 04-crypto.md section 5.4): one button. The gesture certifies a new
// non-extractable session key and returns the PRF output that opens the passkey slot; then `GET /v1/me`, log and wrap
// verification of every org, and the route to `next` (or the start page). Without a usable PRF output (`prf_missing`,
// or `decrypt_failed` of the slot) this browser's device key unlocks if the user chose "Remember this browser";
// otherwise the `prf_missing` guidance of WEB-14 (`ui/prf-missing`: recovery code once, no rotation; remember this
// browser; a PRF passkey from another device). After an unlock without PRF of a PRF-capable passkey whose slot is
// missing, the slot is uploaded so the next sign-in needs only the passkey. "Lost your passkeys?" leads to `/recover`.
// "Stay signed in" (rule E-40, on unless switched off; the choice is kept for this browser in `Prefs`) lets the unlock
// keep the identity with this browser's device key, so a reload, a new tab or a restart opens without the passkey.
import { useState } from 'react';
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { CeremonyCancelled } from '../../../core/account/ceremony';
import { type SignIn, signIn } from '../../../core/account/login';
import type { Identity } from '../../../core/crypto/keys';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { unlockSignIn, uploadMissingSlot } from '../../prf-missing/device';
import { PrfMissing } from '../../prf-missing/PrfMissing';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { safeNext } from '../../shell/guard';

type Status =
  | { readonly step: 'idle'; readonly notice: string | null }
  | { readonly step: 'working' }
  | { readonly step: 'recovery'; readonly signIn: SignIn };

const BUTTON = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';

export function LoginScreen() {
  const { app, credentials, origin } = useAppEnv();
  const { t, error } = useI18n();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const target = safeNext(params.get('next')) ?? '/';
  const unlocked = useStore((state) => state.session.phase === 'unlocked', app.store);
  const [status, setStatus] = useState<Status>({ step: 'idle', notice: null });
  const [stay, setStay] = useState(() => app.prefs.staySignedIn());
  if (unlocked && status.step === 'idle') return <Navigate to={target} replace />;

  const chooseStay = (on: boolean) => {
    app.prefs.setStaySignedIn(on);
    setStay(on);
  };

  const finish = async (result: SignIn, identity: Identity) => {
    await app.unlock(identity);
    // unlocked by the device key: a PRF-capable passkey without slot gets its slot now
    await uploadMissingSlot(app, result, identity);
    navigate(target, { replace: true });
  };

  const start = async () => {
    setStatus({ step: 'working' });
    let result: SignIn;
    try {
      result = await signIn({ anon: app.anon, credentials, origin });
      await app.begin(result);
    } catch (caught) {
      const notice = caught instanceof CeremonyCancelled ? t('login.cancelled') : error(errorCode(caught)).title;
      setStatus({ step: 'idle', notice });
      return;
    }
    const unlocking = await unlockSignIn(app, result);
    if (unlocking === null) {
      setStatus({ step: 'recovery', signIn: result });
      return;
    }
    try {
      await finish(result, unlocking);
    } catch (caught) {
      const code = errorCode(caught);
      // an identity that is not the registered one: the §3.3 options (recovery code) instead of a dead end
      if (code === 'decrypt_failed') setStatus({ step: 'recovery', signIn: result });
      else setStatus({ step: 'idle', notice: error(code).title });
    }
  };

  const signOut = async () => {
    await app.signOut();
    setStatus({ step: 'idle', notice: null });
  };

  return (
    <main className="mx-auto flex max-w-md flex-col gap-4 p-6" data-testid="login.page">
      <h1 className="text-2xl font-semibold">{t('login.title')}</h1>
      {status.step !== 'recovery' ? (
        <>
          <p>{t('login.explain')}</p>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void start()}
            disabled={status.step === 'working'}
            data-testid="login.submit"
          >
            {status.step === 'working' ? t('login.working') : t('login.submit')}
          </button>
          <label className="flex min-h-11 items-center gap-2">
            <input
              type="checkbox"
              className="h-5 w-5"
              checked={stay}
              onChange={(event) => chooseStay(event.target.checked)}
              disabled={status.step === 'working'}
              data-testid="login.stay"
            />
            <span>{t('login.stay')}</span>
          </label>
          <p className="text-sm text-gray-700" data-testid="login.stay.hint">
            {t('login.stay.hint')}
          </p>
          {status.step === 'idle' && status.notice !== null && (
            <p role="alert" data-testid="login.error">
              {status.notice}
            </p>
          )}
          <Link className="min-h-11 self-start py-2 underline" to="/recover" data-testid="login.recover">
            {t('recover.login-link')}
          </Link>
        </>
      ) : (
        <PrfMissing
          signIn={status.signIn}
          unlock={(identity) => app.unlock(identity)}
          onUnlocked={() => navigate(target, { replace: true })}
          onCancel={() => void signOut()}
        />
      )}
    </main>
  );
}
