// The countdown while a send is still running (06-web.md section 4.3): the task does not exist yet, so the digits
// count down from ten seconds since the send started, and the first step is "Assigning execution device" when the
// workgroup gets its device automatically. The task page continues with the real countdown.
import { preparing, digits } from '../../core/tasks/countdown';
import { useI18n } from '../i18n';
import { Digits, Ring, StepRow } from './parts';
import { useNow } from './useNow';

export interface PreparingPanelProps {
  /** Browser time at which the send started. */
  readonly startedAt: number;
  /** The automatic device assignment is running. */
  readonly assigning: boolean;
}

export function PreparingPanel({ startedAt, assigning }: PreparingPanelProps) {
  const { t } = useI18n();
  const now = useNow(true);
  const { remainingMs, progress } = preparing(startedAt, now);
  const shown = digits(remainingMs);
  return (
    <section
      className="flex flex-col gap-4 rounded border border-gray-200 p-4"
      aria-label={t('countdown.label')}
      data-testid="countdown.panel"
      data-phase="preparing"
    >
      <Ring progress={progress}>
        <Digits value={shown.value} fractionDigits={shown.fractionDigits} />
      </Ring>
      <ol className="flex flex-col gap-1" data-testid="countdown.steps">
        {assigning && <StepRow id="assigning" label={t('countdown.step.assigning')} status="current" />}
        <StepRow id="requested" label={t('countdown.step.requested')} status={assigning ? 'pending' : 'current'} />
      </ol>
    </section>
  );
}
