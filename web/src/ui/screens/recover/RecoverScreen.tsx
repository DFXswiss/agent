// `/recover` (06-web.md section 3.5, 04-crypto.md section 5.5) for users without any working passkey: recovery code →
// `recovery/begin` (the recovery slot opens with `KEK_rec`) → proof → `recovery/finish` → a new passkey (gesture 1) →
// sign-in restricted to it (gesture 2) → its passkey slot (with PRF) → a **new** recovery code, stored at once
// (`PUT /v1/me/bundles/recovery`, replacing the old code; mandatory, 04-crypto.md section 5.5), shown once and
// confirmed by retyping its last group (no skip; until then the "Recovery code not confirmed" reminder is set, so
// leaving the page here keeps the reminder) → "Remember this browser" if the new passkey has no PRF → start page. `429` shows the waiting time. A cancelled gesture can be
// retried; the proof is single-use, so a new passkey after a cancelled one begins the recovery again with the code.
import { type FormEvent, useState } from 'react';
import { Link, Navigate } from 'react-router-dom';
import { CeremonyCancelled } from '../../../core/account/ceremony';
import { signIn } from '../../../core/account/login';
import { credentialBytes } from '../../../core/account/passkeys';
import {
  beginRecovery,
  finishRecovery,
  type NewRecoveryCode,
  newRecoveryCode,
  type RecoveredPasskey,
  recoveryRefusal,
  type RecoveryStart,
  recoveryClient,
  replaceRecoveryCode,
} from '../../../core/account/recover';
import { useI18n } from '../../i18n';
import { RememberBrowser } from '../../prf-missing/RememberBrowser';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { RecoveryCodeStep } from '../invite/RecoveryCodeStep';

type Step =
  | { readonly step: 'code'; readonly busy: boolean; readonly notice: string | null }
  | {
      readonly step: 'passkey';
      /** null after a spent proof: the next attempt begins again with the code. */
      readonly start: RecoveryStart | null;
      readonly busy: boolean;
      readonly notice: string | null;
    }
  | {
      readonly step: 'unlock';
      readonly start: RecoveryStart;
      readonly passkey: RecoveredPasskey;
      readonly busy: boolean;
      readonly notice: string | null;
    }
  | {
      readonly step: 'new_code';
      readonly start: RecoveryStart;
      readonly prepared: NewRecoveryCode;
      readonly slot: boolean;
      /** The new code replaced the old one at the hub. */
      readonly saved: boolean;
      readonly failed: string | null;
    }
  | { readonly step: 'remember'; readonly start: RecoveryStart }
  | { readonly step: 'done' };

/** The new code, shown once and confirmed by retyping its last group; no "Skip" after a recovery. */
function NewCode({ code, onConfirmed }: { readonly code: string; readonly onConfirmed: () => void }) {
  // `allowSkip` is false, so "Skip" is never rendered; the confirm handler keeps the required prop total
  return <RecoveryCodeStep code={code} allowSkip={false} onConfirmed={onConfirmed} onSkipped={onConfirmed} />;
}

/** The new-code step: saving, the failed save with "Try again", or the stored code to confirm. */
function NewCodeState(props: {
  readonly step: Extract<Step, { step: 'new_code' }>;
  readonly onConfirmed: () => void;
  readonly onRetry: () => void;
}) {
  const { t, error } = useI18n();
  const { step } = props;
  if (step.saved) return <NewCode code={step.prepared.code} onConfirmed={props.onConfirmed} />;
  if (step.failed === null) return <p data-testid="recover.saving">{t('recover.saving')}</p>;
  return (
    <section role="alert" className="flex flex-col gap-3" data-testid="recover.save-failed" data-code={step.failed}>
      <p>{t('recover.save-failed', { reason: error(step.failed).title })}</p>
      <button type="button" className={PRIMARY} onClick={props.onRetry} data-testid="recover.save-retry">
        {t('recover.save-retry')}
      </button>
    </section>
  );
}

const PRIMARY = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';

export function RecoverScreen() {
  const { app, credentials, origin } = useAppEnv();
  const { t, error, locale } = useI18n();
  const [client] = useState(() => recoveryClient(app.fetch));
  const [code, setCode] = useState('');
  const [step, setStep] = useState<Step>({ step: 'code', busy: false, notice: null });

  /** The text of a refused begin: the waiting time for `429`, one text for an unknown code, else the catalog. */
  const beginNotice = (caught: unknown): string => {
    const refusal = recoveryRefusal(caught, locale);
    return 'key' in refusal ? t(refusal.key, refusal.params) : error(refusal.code).title;
  };

  const begin = async (event: FormEvent) => {
    event.preventDefault();
    setStep({ step: 'code', busy: true, notice: null });
    try {
      const start = await beginRecovery(client, code);
      setStep({ step: 'passkey', start, busy: false, notice: null });
    } catch (caught) {
      setStep({ step: 'code', busy: false, notice: beginNotice(caught) });
    }
  };

  const createPasskey = async (current: Extract<Step, { step: 'passkey' }>) => {
    setStep({ ...current, busy: true, notice: null });
    let start = current.start;
    try {
      start ??= await beginRecovery(client, code);
    } catch (caught) {
      setStep({ step: 'code', busy: false, notice: beginNotice(caught) });
      return;
    }
    try {
      const passkey = await finishRecovery(client, credentials, start);
      setStep({ step: 'unlock', start, passkey, busy: false, notice: null });
    } catch (caught) {
      const notice =
        caught instanceof CeremonyCancelled ? t('recover.passkey.cancelled') : error(errorCode(caught)).title;
      // the proof was presented (or the ceremony is gone): a new attempt begins again
      setStep({ ...current, start: null, busy: false, notice });
    }
  };

  const unlock = async (current: Extract<Step, { step: 'unlock' }>) => {
    setStep({ ...current, busy: true, notice: null });
    try {
      const result = await signIn({
        anon: app.anon,
        credentials,
        origin,
        allowCredential: credentialBytes(current.passkey.credentialId),
      });
      if (result.userId !== current.start.userId || result.credentialId !== current.passkey.credentialId) {
        setStep({ ...current, busy: false, notice: t('recover.unlock.other-account') });
        return;
      }
      await app.begin(result);
      await app.unlock(current.start.identity);
      const slot = await app.uploadPasskeySlot(result, current.start.identity);
      app.notePrf(result, slot);
      const prepared = await newRecoveryCode(current.start.userId, current.start.identity);
      await save({ step: 'new_code', start: current.start, prepared, slot, saved: false, failed: null });
    } catch (caught) {
      const notice =
        caught instanceof CeremonyCancelled ? t('recover.unlock.cancelled') : error(errorCode(caught)).title;
      setStep({ ...current, busy: false, notice });
    }
  };

  const save = async (current: Extract<Step, { step: 'new_code' }>) => {
    setStep({ ...current, failed: null });
    try {
      await replaceRecoveryCode(
        (method, target, options) => app.client.request(method, target, options),
        current.prepared,
      );
    } catch (caught) {
      setStep({ ...current, failed: errorCode(caught) });
      return;
    }
    // the old code is gone; the reminder stays until the user retyped the new one
    app.setRecoveryUnconfirmed(true);
    setCode('');
    setStep({ ...current, saved: true, failed: null });
  };

  const confirmed = (current: Extract<Step, { step: 'new_code' }>) => {
    app.setRecoveryUnconfirmed(false);
    setStep(current.slot ? { step: 'done' } : { step: 'remember', start: current.start });
  };

  if (step.step === 'done') return <Navigate to="/" replace />;

  const notice = (text: string | null) =>
    text !== null && (
      <p role="alert" className="text-red-800" data-testid="recover.notice">
        {text}
      </p>
    );

  return (
    <main className="mx-auto flex max-w-xl flex-col gap-4 p-6" data-testid="recover.page" data-step={step.step}>
      <h1 className="text-2xl font-semibold">{t('recover.title')}</h1>
      {step.step === 'code' && (
        <form className="flex flex-col gap-3" onSubmit={(event) => void begin(event)}>
          <p>{t('recover.explain')}</p>
          <label htmlFor="recover-code">{t('recover.code.label')}</label>
          <input
            id="recover-code"
            className="min-h-11 rounded border border-gray-400 px-3 font-mono uppercase"
            autoComplete="off"
            spellCheck={false}
            value={code}
            onChange={(event) => setCode(event.target.value)}
            data-testid="recover.code"
          />
          <button type="submit" className={PRIMARY} disabled={step.busy} data-testid="recover.submit">
            {step.busy ? t('recover.code.working') : t('recover.code.submit')}
          </button>
          {notice(step.notice)}
          <p className="text-sm text-gray-700">{t('recover.lost')}</p>
          <Link className="min-h-11 self-start py-2 underline" to="/login" data-testid="recover.login">
            {t('recover.back')}
          </Link>
        </form>
      )}
      {step.step === 'passkey' && (
        <section className="flex flex-col gap-3" data-testid="recover.passkey-step">
          <h2 className="text-xl font-semibold">{t('recover.passkey.title')}</h2>
          <p>{t('recover.passkey.explain')}</p>
          <button
            type="button"
            className={PRIMARY}
            onClick={() => void createPasskey(step)}
            disabled={step.busy}
            data-testid="recover.create-passkey"
          >
            {step.busy ? t('recover.passkey.working') : t('recover.passkey.submit')}
          </button>
          {notice(step.notice)}
        </section>
      )}
      {step.step === 'unlock' && (
        <section className="flex flex-col gap-3" data-testid="recover.unlock-step">
          <h2 className="text-xl font-semibold">{t('recover.unlock.title')}</h2>
          <p>{t('recover.unlock.explain')}</p>
          <button
            type="button"
            className={PRIMARY}
            onClick={() => void unlock(step)}
            disabled={step.busy}
            data-testid="recover.unlock"
          >
            {step.busy ? t('recover.unlock.working') : t('recover.unlock.submit')}
          </button>
          {notice(step.notice)}
        </section>
      )}
      {step.step === 'new_code' && (
        <>
          <p data-testid="recover.new-code">{t('recover.new-code')}</p>
          <NewCodeState step={step} onConfirmed={() => confirmed(step)} onRetry={() => void save(step)} />
        </>
      )}
      {step.step === 'remember' && (
        <RememberBrowser
          userId={step.start.userId}
          identity={step.start.identity}
          onDone={() => setStep({ step: 'done' })}
        />
      )}
    </main>
  );
}
