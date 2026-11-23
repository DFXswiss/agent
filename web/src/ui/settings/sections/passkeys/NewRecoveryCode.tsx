// "Generate a new recovery code" in `/settings` (06-web.md section 3.2; KOR-01, AUD-01 B-06): the browser draws a new
// code and seals the unlocked identity with it, shows it once and requires the confirmation of the onboarding
// (re-typing the last group, no skip); only then one passkey gesture proves the user and the hub replaces the recovery
// slot (`rotateRecoveryCode`). Until the hub accepted it the old code stays valid; afterwards only the new one works.
// A cancelled gesture keeps the shown code for another try.
import { useState } from 'react';
import { CeremonyCancelled } from '../../../../core/account/ceremony';
import {
  type NewRecoveryCode as Prepared,
  newRecoveryCode,
  rotateRecoveryCode,
} from '../../../../core/account/recover';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { RecoveryCodeStep } from '../../../screens/invite/RecoveryCodeStep';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';

type State =
  | { readonly step: 'idle'; readonly done: boolean }
  | { readonly step: 'show'; readonly prepared: Prepared; readonly notice: string | null; readonly busy: boolean };

const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4 disabled:opacity-60';

export function NewRecoveryCode() {
  const { app, credentials } = useAppEnv();
  const { t, error } = useI18n();
  const identity = useStore((state) => state.me.identity, app.store);
  const [state, setState] = useState<State>({ step: 'idle', done: false });

  const cancel = () => setState({ step: 'idle', done: false });

  const start = async () => {
    setState({ step: 'show', prepared: await newRecoveryCode(app.userId!, identity!), notice: null, busy: false });
  };

  const replace = async (current: Extract<State, { step: 'show' }>) => {
    setState({ ...current, notice: null, busy: true });
    try {
      const request = (method: string, target: string, options?: { body: unknown }) =>
        app.client.request(method, target, options);
      await rotateRecoveryCode(request, credentials, current.prepared);
      app.setRecoveryUnconfirmed(false);
      setState({ step: 'idle', done: true });
    } catch (caught) {
      const text =
        caught instanceof CeremonyCancelled ? t('passkeys.recovery-new.cancelled') : error(errorCode(caught)).title;
      setState({ ...current, notice: text, busy: false });
    }
  };

  return (
    <div className="flex flex-col gap-3" data-testid="passkeys.recovery-new" data-step={state.step}>
      <h3 className="text-lg font-semibold">{t('passkeys.recovery-new.title')}</h3>
      {state.step === 'idle' ? (
        <>
          <p>{t('passkeys.recovery-new.explain')}</p>
          <button
            type="button"
            className={BUTTON}
            disabled={identity === null}
            onClick={() => void start()}
            data-testid="passkeys.recovery-new.start"
          >
            {t('passkeys.recovery-new.start')}
          </button>
          {state.done && (
            <p role="status" data-testid="passkeys.recovery-new.notice">
              {t('passkeys.recovery-new.done')}
            </p>
          )}
        </>
      ) : (
        <>
          <p>{t('passkeys.recovery-new.confirm-first')}</p>
          {state.busy ? (
            <p role="status" data-testid="passkeys.recovery-new.working">
              {t('passkeys.recovery-new.working')}
            </p>
          ) : (
            <RecoveryCodeStep
              code={state.prepared.code}
              allowSkip={false}
              onConfirmed={() => void replace(state)}
              onSkipped={cancel}
            />
          )}
          {state.notice !== null && (
            <p role="alert" data-testid="passkeys.recovery-new.notice">
              {state.notice}
            </p>
          )}
          <button
            type="button"
            className={BUTTON}
            disabled={state.busy}
            onClick={cancel}
            data-testid="passkeys.recovery-new.cancel"
          >
            {t('passkeys.recovery-new.cancel')}
          </button>
        </>
      )}
    </div>
  );
}
