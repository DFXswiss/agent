// "Check" a mailbox on a device (KOR-74, ADR-099 section 9): `mailbox.check` with the mailbox's body, opened and sealed
// to the chosen device in the core (`startMailboxCheck`); the answer is only "valid" or "not valid".
import { useState } from 'react';
import { checkProgress } from '../../../../core/credentials/login';
import { startMailboxCheck } from '../../../../core/credentials/mailboxes';
import { type PreparedRequest, requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { BUTTON, DevicePicker } from '../credentials/DevicePicker';
import { useRequestView } from '../credentials/useRequestView';
import { useReason } from '../github/reason';

const ID = 'mbcheck';

type Phase =
  | { readonly kind: 'pick' }
  | { readonly kind: 'starting' }
  | { readonly kind: 'request'; readonly prepared: PreparedRequest }
  | { readonly kind: 'error'; readonly code: string };

function Result({ prepared }: { readonly prepared: PreparedRequest }) {
  const { t } = useI18n();
  const reason = useReason();
  const { view, readError } = useRequestView(prepared);
  const progress = view === null ? { step: 'waiting' as const } : checkProgress(view);
  if (progress.step === 'waiting')
    return (
      <p aria-live="polite" data-testid={`${ID}.waiting`} data-read-error={readError ?? ''}>
        {readError === null
          ? t('mailbox.check.waiting')
          : t('mailbox.check.read-failed', { reason: reason(readError) })}
      </p>
    );
  if (progress.step === 'failed')
    return (
      <p role="alert" data-testid={`${ID}.failed`} data-code={progress.code}>
        {t('mailbox.check.failed', { reason: reason(progress.code) })}
      </p>
    );
  return (
    <p role="status" data-testid={`${ID}.result`} data-valid={progress.valid}>
      {progress.valid ? t('mailbox.check.valid') : t('mailbox.check.invalid')}
    </p>
  );
}

export function MailboxCheck({ id, onClose }: { readonly id: string; readonly onClose: () => void }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [phase, setPhase] = useState<Phase>({ kind: 'pick' });
  const start = async (device: string) => {
    setPhase({ kind: 'starting' });
    try {
      setPhase({ kind: 'request', prepared: await startMailboxCheck(app.tasks, device, id) });
    } catch (error) {
      setPhase({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid={ID}>
      {phase.kind === 'pick' && (
        <DevicePicker testId={ID} startLabel={t('mailbox.check.start')} onPick={(device) => void start(device)} />
      )}
      {phase.kind === 'starting' && <p data-testid={`${ID}.starting`}>{t('mailbox.check.starting')}</p>}
      {phase.kind === 'error' && (
        <p role="alert" data-testid={`${ID}.failed`} data-code={phase.code}>
          {t('mailbox.check.failed', { reason: reason(phase.code) })}
        </p>
      )}
      {phase.kind === 'request' && <Result prepared={phase.prepared} />}
      <button type="button" className={BUTTON} onClick={onClose} data-testid={`${ID}.close`}>
        {t('mailbox.close')}
      </button>
    </section>
  );
}
