// Connecting a Claude subscription (06-web.md section 8.2, 05-runner.md sections 11.2 and 11.3, ADR-011): the device
// (and, with a stored mailbox, the mode "automatic"), then the `claude.login` request. Relay: the device answers with
// the authorization link (opened in a new tab), the user pastes the code shown there, which goes to the device as a
// follow-up request; the device stores the subscription token itself (CK-encrypted) and answers with the account's
// e-mail. Automatic: the device's progress is shown; if the automation fails (`login_automation_failed`) the relay
// continues with the link it sends. The token never reaches this page.
import { type FormEvent, useEffect, useState } from 'react';
import { type LoginMode, loginProgress, sendCode, startLogin } from '../../../../core/credentials/login';
import { AUTOMATION_FAILED } from '../../../../core/credentials/login';
import { CODE_RE } from '../../../../core/requests/bodies';
import { type PreparedRequest, requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { BUTTON, DevicePicker, PRIMARY } from './DevicePicker';
import { useRequestView } from './useRequestView';

type Phase =
  | { readonly kind: 'pick' }
  | { readonly kind: 'starting' }
  | { readonly kind: 'request'; readonly prepared: PreparedRequest }
  | { readonly kind: 'error'; readonly code: string };

function Authorize({
  url,
  automationFailed,
  sent,
  onCode,
}: {
  readonly url: string;
  readonly automationFailed: boolean;
  readonly sent: boolean;
  readonly onCode: (code: string) => void;
}) {
  const { t, error } = useI18n();
  const [code, setCode] = useState('');
  const valid = CODE_RE.test(code.trim()) && code.trim().length <= 512;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onCode(code);
  };
  return (
    <form className="flex flex-col gap-3" onSubmit={submit} data-testid="claude.authorize">
      {automationFailed && (
        <p role="alert" data-testid="claude.automation-failed" data-code={AUTOMATION_FAILED}>
          {error(AUTOMATION_FAILED).title}. {error(AUTOMATION_FAILED).action}
        </p>
      )}
      <p>{t('credentials.claude.authorize.intro')}</p>
      <a
        className={`${PRIMARY} flex items-center`}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        data-testid="claude.authorize.open"
      >
        {t('credentials.claude.authorize.open')}
      </a>
      <label htmlFor="claude-code">{t('credentials.claude.code.label')}</label>
      <input
        id="claude-code"
        className="min-h-11 rounded border border-gray-400 px-3 font-mono"
        autoComplete="off"
        spellCheck={false}
        value={code}
        onChange={(event) => setCode(event.target.value)}
        disabled={sent}
        data-testid="claude.code"
      />
      <button type="submit" className={PRIMARY} disabled={!valid || sent} data-testid="claude.code.send">
        {sent ? t('credentials.claude.code.sent') : t('credentials.claude.code.send')}
      </button>
    </form>
  );
}

/** The relay of one `claude.login` request: progress, the authorization link and the code, the outcome (also used by
 * the login of a further subscription, KOR-69). */
export function LoginRequest({
  prepared,
  onDone,
  onRetry,
}: {
  readonly prepared: PreparedRequest;
  readonly onDone: (email: string | null) => void;
  readonly onRetry: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [followUp, setFollowUp] = useState<PreparedRequest | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const original = useRequestView(prepared);
  const follow = useRequestView(followUp);
  const progress = original.view === null ? { step: 'waiting' as const } : loginProgress(original.view, follow.view);
  const doneEmail = progress.step === 'done' ? progress.email : undefined;
  useEffect(() => {
    if (doneEmail !== undefined) onDone(doneEmail);
  }, [doneEmail, onDone]);
  const send = async (code: string) => {
    setSendError(null);
    try {
      setFollowUp(await sendCode(app.tasks, prepared, code));
    } catch (error) {
      setSendError(requestErrorCode(error));
    }
  };
  const readError = follow.readError ?? original.readError;
  return (
    <div className="flex flex-col gap-3">
      {progress.step === 'waiting' && (
        <p aria-live="polite" data-testid="claude.waiting">
          {t('credentials.waiting')}
        </p>
      )}
      {progress.step === 'progress' && (
        <p aria-live="polite" data-testid="claude.progress" data-stage={progress.stage}>
          {t(`credentials.claude.stage.${progress.stage}`)}
        </p>
      )}
      {progress.step === 'authorize' && (
        <Authorize
          url={progress.url}
          automationFailed={progress.automationFailed}
          sent={followUp !== null}
          onCode={(code) => void send(code)}
        />
      )}
      {progress.step === 'done' && (
        <p role="status" data-testid="claude.done">
          {t('credentials.claude.done')}
        </p>
      )}
      {progress.step === 'failed' && (
        <>
          <p role="alert" data-testid="claude.failed" data-code={progress.code}>
            {t('credentials.claude.failed', { reason: reason(progress.code) })}
          </p>
          <button type="button" className={BUTTON} onClick={onRetry} data-testid="claude.retry">
            {t('credentials.retry')}
          </button>
        </>
      )}
      {sendError !== null && (
        <p role="alert" data-testid="claude.code.failed" data-code={sendError}>
          {t('credentials.claude.code.failed', { reason: reason(sendError) })}
        </p>
      )}
      {readError !== null && (
        <p className="text-sm text-red-800" data-testid="claude.read-failed" data-code={readError}>
          {t('credentials.read-failed', { reason: reason(readError) })}
        </p>
      )}
    </div>
  );
}

export function LoginFlow({
  automatic,
  onDone,
  onClose,
}: {
  /** A mailbox is stored: the automatic mode may be chosen. */
  readonly automatic: boolean;
  readonly onDone: (email: string | null) => void;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [phase, setPhase] = useState<Phase>({ kind: 'pick' });
  const [mode, setMode] = useState<LoginMode>(automatic ? 'automatic' : 'manual');
  const start = async (device: string) => {
    setPhase({ kind: 'starting' });
    try {
      setPhase({ kind: 'request', prepared: await startLogin(app.tasks, device, mode) });
    } catch (error) {
      setPhase({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  const retry = () => setPhase({ kind: 'pick' });
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid="claude.flow">
      <h4 className="font-semibold">{t('credentials.claude.flow.title')}</h4>
      {phase.kind === 'pick' && (
        <DevicePicker
          testId="claude"
          startLabel={t('credentials.claude.start')}
          onPick={(device) => void start(device)}
        >
          <fieldset className="flex flex-col gap-1" data-testid="claude.mode">
            <legend>{t('credentials.claude.mode.label')}</legend>
            <label className="flex min-h-11 items-center gap-2">
              <input
                type="radio"
                name="claude-mode"
                checked={mode === 'manual'}
                onChange={() => setMode('manual')}
                data-testid="claude.mode.manual"
              />
              {t('credentials.claude.mode.manual')}
            </label>
            <label className="flex min-h-11 items-center gap-2">
              <input
                type="radio"
                name="claude-mode"
                checked={mode === 'automatic'}
                disabled={!automatic}
                onChange={() => setMode('automatic')}
                data-testid="claude.mode.automatic"
              />
              {automatic ? t('credentials.claude.mode.automatic') : t('credentials.claude.mode.needs-mailbox')}
            </label>
          </fieldset>
        </DevicePicker>
      )}
      {phase.kind === 'starting' && <p data-testid="claude.starting">{t('credentials.starting')}</p>}
      {phase.kind === 'error' && (
        <>
          <p role="alert" data-testid="claude.failed" data-code={phase.code}>
            {t('credentials.claude.failed', { reason: reason(phase.code) })}
          </p>
          <button type="button" className={BUTTON} onClick={retry} data-testid="claude.retry">
            {t('credentials.retry')}
          </button>
        </>
      )}
      {phase.kind === 'request' && <LoginRequest prepared={phase.prepared} onDone={onDone} onRetry={retry} />}
      <button type="button" className={BUTTON} onClick={onClose} data-testid="claude.close">
        {t('credentials.close')}
      </button>
    </section>
  );
}
