// "Optimize processes" (ADR-097 sections 6 and 7): the button of the situation view (scope `org`) and of the workgroup
// view (scope `wg`), rendered by the screens only for a person who may write a workgroup (resp. this one). It opens a
// dialog: the workgroup the task lives in (the writable workgroups; default as the screen passes it), the period
// (presets of `core/analysis/`, default 7 days), who will read the report, and "Start". The task is created through
// the ordinary send path with the fixed title and prompt in the UI language and `analysis`; then the task page opens,
// as after the composer. A failed start keeps the prepared request while workgroup and period stay the same and the
// failure may pass (`RETRYABLE` of the composer: same IDs, idempotent); `peer_outdated` says that a device of the
// organization must be updated first.
import { useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { DEFAULT_DAYS, PERIODS, prepareAnalysis, type Scope } from '../../core/analysis/analysis';
import { type PreparedTask, sendErrorCode, sendTask } from '../../core/tasks/send';
import { RETRYABLE } from '../composer/Composer';
import { useI18n } from '../i18n';
import { Sheet } from '../responsive/Sheet';
import { useAppEnv } from '../shell/context';
import '../../core/i18n/lazy/analysis';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';
const SELECT = 'min-h-11 rounded border border-gray-300 px-2';

export interface AnalysisButtonProps {
  readonly org: string;
  readonly scope: Scope;
  /** The writable workgroups (the choice of the dialog), the default one included. */
  readonly workgroups: readonly string[];
  /** The preselected workgroup. */
  readonly initial: string;
  /** The display name of a workgroup. */
  readonly name: (wg: string) => string;
}

interface Kept {
  readonly wg: string;
  readonly days: number;
  readonly task: PreparedTask;
}

function AnalysisDialog({ org, scope, workgroups, initial, name, onClose }: AnalysisButtonProps & { onClose(): void }) {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const navigate = useNavigate();
  const wgId = useId();
  const periodId = useId();
  const [wg, setWg] = useState(initial);
  const [days, setDays] = useState<number>(DEFAULT_DAYS);
  const [sending, setSending] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const kept = useRef<Kept | null>(null);
  const start = async () => {
    setSending(true);
    setFailed(null);
    try {
      const known = kept.current;
      const task =
        known !== null && known.wg === wg && known.days === days
          ? known.task
          : await prepareAnalysis(app.tasks, { org, wg, name: name(wg), scope, days, language: locale });
      kept.current = { wg, days, task };
      const sent = await sendTask(app.tasks, task);
      navigate(`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(sent.task.task_id)}`);
    } catch (failure) {
      const code = sendErrorCode(failure);
      if (!RETRYABLE.has(code)) kept.current = null;
      setFailed(code);
      setSending(false);
    }
  };
  return (
    <Sheet title={t('analysis.title')} testId="analysis.dialog" busy={sending} onClose={onClose}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void start();
        }}
      >
        <p className="text-sm text-gray-700">{t('analysis.intro')}</p>
        <label htmlFor={wgId} className="text-sm font-medium">
          {t('analysis.wg')}
        </label>
        <select
          id={wgId}
          className={SELECT}
          value={wg}
          onChange={(event) => setWg(event.target.value)}
          disabled={sending}
          data-testid="analysis.wg"
        >
          {workgroups.map((id) => (
            <option key={id} value={id}>
              {name(id)}
            </option>
          ))}
        </select>
        <label htmlFor={periodId} className="text-sm font-medium">
          {t('analysis.period')}
        </label>
        <select
          id={periodId}
          className={SELECT}
          value={days}
          onChange={(event) => setDays(Number(event.target.value))}
          disabled={sending}
          data-testid="analysis.period"
        >
          {PERIODS.map((item) => (
            <option key={item} value={item}>
              {t(`analysis.period.${item}`)}
            </option>
          ))}
        </select>
        <p className="text-sm" data-testid="analysis.visibility" data-scope={scope}>
          {t(`analysis.visibility.${scope}`, { name: name(wg) })}
        </p>
        {failed !== null && (
          <div
            role="alert"
            className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-900"
            data-testid="analysis.error"
            data-code={failed}
          >
            {failed === 'peer_outdated' ? (
              <p>{t('analysis.error.peer_outdated')}</p>
            ) : (
              <>
                <p className="font-semibold">{error(failed).title}</p>
                <p>{error(failed).body}</p>
              </>
            )}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="submit"
            className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
            disabled={sending}
            data-testid="analysis.start"
          >
            {sending ? t('analysis.starting') : t('analysis.start')}
          </button>
          <button type="button" className={BUTTON} onClick={onClose} disabled={sending} data-testid="analysis.cancel">
            {t('analysis.cancel')}
          </button>
        </div>
      </form>
    </Sheet>
  );
}

export function AnalysisButton(props: AnalysisButtonProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={BUTTON}
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        data-testid="analysis.open"
        data-scope={props.scope}
      >
        {t('analysis.button')}
      </button>
      {open && <AnalysisDialog {...props} onClose={() => setOpen(false)} />}
    </>
  );
}
