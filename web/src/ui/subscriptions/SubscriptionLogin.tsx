// The login of a further Claude subscription on a platform device (KOR-69, ADR-090 section 2): the device is chosen by
// itself (an online one that announced `subscriptions`, `subscriptionDevices`), the `claude.login` request names the
// subscription (and, for a new one, its label and note); then the relay of the credentials section runs
// (`LoginRequest`: open the link, paste the code, done). The token is created on the device and never reaches this page.
// KOR-73 (ADR-100): with a device that runs logins side by side the record may be written first (own mailbox, use), the
// login carries the subscription as its slot and may register a new account; a login that needs this is refused for
// an older device (`no_device_available`).
import { useCallback, useEffect, useRef, useState } from 'react';
import { type LoginInput, startSubscriptionLogin, subscriptionDevices } from '../../core/credentials/subscriptions';
import { type PreparedRequest, requestErrorCode } from '../../core/requests/client';
import { useI18n } from '../i18n';
import { useReason } from '../settings/sections/github/reason';
import { BUTTON } from '../settings/sections/credentials/DevicePicker';
import { LoginRequest } from '../settings/sections/credentials/LoginFlow';
import { useAppEnv } from '../shell/context';
import '../../core/i18n/lazy/subscription-login';

type Phase =
  | { readonly kind: 'starting' }
  | { readonly kind: 'none' }
  | { readonly kind: 'request'; readonly prepared: PreparedRequest }
  | { readonly kind: 'error'; readonly code: string };

export function SubscriptionLogin({
  input,
  onDone,
  onClose,
}: {
  readonly input: LoginInput;
  readonly onDone: () => void;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [phase, setPhase] = useState<Phase>({ kind: 'starting' });
  // the input of the first render: a re-render of the caller (a new object) never starts a second login
  const first = useRef(input);
  useEffect(() => {
    if (phase.kind !== 'starting') return;
    let closed = false;
    subscriptionDevices(app.tasks)
      .then(async (found) => {
        if (closed) return;
        if (found.preselected === null) return setPhase({ kind: 'none' });
        const prepared = await startSubscriptionLogin(app.tasks, found.preselected, first.current, found.parallel);
        if (!closed) setPhase({ kind: 'request', prepared });
      })
      .catch((error: unknown) => {
        if (!closed) setPhase({ kind: 'error', code: requestErrorCode(error) });
      });
    // "Close" or a new attempt: a late answer changes nothing that is shown and starts nothing more
    return () => {
      closed = true;
    };
  }, [app, phase]);
  const done = useCallback(() => onDone(), [onDone]);
  const retry = () => setPhase({ kind: 'starting' });
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid="sublogin.flow">
      {phase.kind === 'starting' && (
        <p aria-live="polite" data-testid="sublogin.starting">
          {t('subscription-login.starting')}
        </p>
      )}
      {phase.kind === 'none' && (
        <p role="alert" data-testid="sublogin.no-device">
          {t('subscription-login.no-device')}
        </p>
      )}
      {phase.kind === 'error' && (
        <>
          <p role="alert" data-testid="sublogin.failed" data-code={phase.code}>
            {t('subscription-login.failed', { reason: reason(phase.code) })}
          </p>
          <button type="button" className={BUTTON} onClick={retry} data-testid="sublogin.retry">
            {t('subscription-login.retry')}
          </button>
        </>
      )}
      {phase.kind === 'request' && <LoginRequest prepared={phase.prepared} onDone={done} onRetry={retry} />}
      <button type="button" className={BUTTON} onClick={onClose} data-testid="sublogin.close">
        {t('subscription-login.close')}
      </button>
    </section>
  );
}
