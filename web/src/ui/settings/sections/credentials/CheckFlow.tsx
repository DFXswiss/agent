// "Check key" / "Check mailbox" (06-web.md section 8.2): the stored credential is checked by an execution device
// (`credential.check` for the API key or the subscription, `mailbox.check` for the mailbox); the answer is only
// "valid" or "not valid" – nothing of the credential is shown.
import { useState } from 'react';
import { type CheckKind, checkProgress, startCheck } from '../../../../core/credentials/login';
import { type PreparedRequest, requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { BUTTON, DevicePicker } from './DevicePicker';
import { useRequestView } from './useRequestView';

type Phase =
  | { readonly kind: 'pick' }
  | { readonly kind: 'starting' }
  | { readonly kind: 'request'; readonly prepared: PreparedRequest }
  | { readonly kind: 'error'; readonly code: string };

function Result({ prepared, testId }: { readonly prepared: PreparedRequest; readonly testId: string }) {
  const { t } = useI18n();
  const reason = useReason();
  const { view, readError } = useRequestView(prepared);
  const progress = view === null ? { step: 'waiting' as const } : checkProgress(view);
  if (progress.step === 'waiting')
    return (
      <p aria-live="polite" data-testid={`${testId}.waiting`} data-read-error={readError ?? ''}>
        {readError === null ? t('credentials.waiting') : t('credentials.read-failed', { reason: reason(readError) })}
      </p>
    );
  if (progress.step === 'failed')
    return (
      <p role="alert" data-testid={`${testId}.failed`} data-code={progress.code}>
        {t('credentials.check.failed', { reason: reason(progress.code) })}
      </p>
    );
  return (
    <p role="status" data-testid={`${testId}.result`} data-valid={progress.valid}>
      {progress.valid ? t('credentials.check.valid') : t('credentials.check.invalid')}
    </p>
  );
}

export function CheckFlow({
  kind,
  testId,
  onClose,
}: {
  readonly kind: CheckKind;
  readonly testId: string;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [phase, setPhase] = useState<Phase>({ kind: 'pick' });
  const start = async (device: string) => {
    setPhase({ kind: 'starting' });
    try {
      setPhase({ kind: 'request', prepared: await startCheck(app.tasks, device, kind) });
    } catch (error) {
      setPhase({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid={testId}>
      {phase.kind === 'pick' && (
        <DevicePicker
          testId={testId}
          startLabel={t('credentials.check.start')}
          onPick={(device) => void start(device)}
        />
      )}
      {phase.kind === 'starting' && <p data-testid={`${testId}.starting`}>{t('credentials.starting')}</p>}
      {phase.kind === 'error' && (
        <p role="alert" data-testid={`${testId}.failed`} data-code={phase.code}>
          {t('credentials.check.failed', { reason: reason(phase.code) })}
        </p>
      )}
      {phase.kind === 'request' && <Result prepared={phase.prepared} testId={testId} />}
      <button type="button" className={BUTTON} onClick={onClose} data-testid={`${testId}.close`}>
        {t('credentials.close')}
      </button>
    </section>
  );
}
