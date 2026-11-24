// The log of a test run or a work VM job (ADR-078 section 3): a button opens the log panel below it. The panel shows
// one page (one chunk of at most 256 KiB of output) at a time – never the whole log in the DOM – with "Start",
// "Previous part", "Next part" and "Jump to end", the note when only the last part was kept, and "Download", which
// reads every chunk and saves a `.log` file. Reading, checking and decrypting is `core/logs`.
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { type LogSource, locateEvent } from '../../core/logs/reader';
import { LogViewer } from '../../core/logs/viewer';
import type { LogRef } from '../../core/chat/shape';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

const BUTTON = 'min-h-11 rounded border border-gray-400 px-3 text-sm disabled:opacity-50';
const MIB = 1024 * 1024;

/** Offers `blob` as a file download. */
export function saveBlob(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function LogPanel({ source, onClose }: { readonly source: LogSource; readonly onClose: () => void }) {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const viewer = useMemo(() => new LogViewer(app.tasks, source), [app, source]);
  useEffect(() => {
    void viewer.open();
    return () => viewer.close();
  }, [viewer]);
  const state = useSyncExternalStore(viewer.subscribe, viewer.snapshot);
  const mib = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  const last = state.chunks - 1;
  const download = () =>
    void viewer.download().then((parts) => {
      if (parts !== null) saveBlob(`${source.ref}.log`, new Blob(parts as BlobPart[], { type: 'text/plain' }));
    });
  return (
    <section
      className="flex flex-col gap-2 rounded border border-gray-300 p-2"
      aria-label={t('logs.title')}
      data-testid="log.viewer"
      data-status={state.status}
      data-index={state.index}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{t('logs.title')}</span>
        <button type="button" className={BUTTON} onClick={onClose} data-testid="log.close">
          {t('logs.close')}
        </button>
      </div>
      {state.status === 'loading' && (
        <p className="text-sm text-gray-600" data-testid="log.loading">
          {t('logs.loading')}
        </p>
      )}
      {state.code !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="log.failed" data-code={state.code}>
          {t('logs.failed', { reason: error(state.code).title })}
        </p>
      )}
      {state.status === 'ready' && (
        <>
          {state.truncated && (
            <p className="text-sm text-gray-700" data-testid="log.truncated">
              {t('logs.truncated', { kept: mib.format(state.stored / MIB), total: mib.format(state.bytes / MIB) })}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={BUTTON}
              disabled={state.index === 0}
              onClick={() => void viewer.go(0)}
              data-testid="log.first"
            >
              {t('logs.first')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={state.index === 0}
              onClick={() => void viewer.go(state.index - 1)}
              data-testid="log.previous"
            >
              {t('logs.previous')}
            </button>
            <span className="text-sm" data-testid="log.part">
              {t('logs.part', { index: state.index + 1, count: state.chunks })}
            </span>
            <button
              type="button"
              className={BUTTON}
              disabled={state.index === last}
              onClick={() => void viewer.go(state.index + 1)}
              data-testid="log.next"
            >
              {t('logs.next')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={state.index === last}
              onClick={() => void viewer.go(last)}
              data-testid="log.last"
            >
              {t('logs.last')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={state.downloading}
              onClick={download}
              data-testid="log.download"
            >
              {state.downloading ? t('logs.downloading') : t('logs.download')}
            </button>
          </div>
          {state.downloadCode !== null && (
            <p role="alert" className="text-sm text-red-800" data-testid="log.download-failed">
              {t('logs.download-failed', { reason: error(state.downloadCode).title })}
            </p>
          )}
          <pre
            className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-50 p-2 font-mono text-xs"
            data-testid="log.text"
            aria-busy={state.text === null}
          >
            {state.text ?? t('logs.loading')}
          </pre>
        </>
      )}
    </section>
  );
}

/** A button that opens the log of `source` below it (and closes it again). */
export function OpenLog({ source, label }: { readonly source: LogSource; readonly label: string }) {
  // the source is fixed while the panel is open (a new object per render would restart the viewer)
  const [shown, setShown] = useState<LogSource | null>(null);
  return (
    <div className="flex flex-col gap-1">
      <button
        type="button"
        className="min-h-11 self-start text-sm text-blue-800 underline"
        aria-expanded={shown !== null}
        onClick={() => setShown(shown === null ? source : null)}
        data-testid="log.open"
      >
        {label}
      </button>
      {shown !== null && <LogPanel source={shown} onClose={() => setShown(null)} />}
    </div>
  );
}

/**
 * The log button of a chat item: the workgroup and task come from the verified event at `seq` with this `body`;
 * nothing while the store does not hold it.
 */
export function EventLog({
  seq,
  body,
  runId,
  log,
  label,
}: {
  readonly seq: number;
  readonly body: unknown;
  readonly runId: string;
  readonly log: LogRef;
  readonly label: string;
}) {
  const { app } = useAppEnv();
  // a text key keeps the selected value stable between store updates
  const place = useStore((state) => {
    const found = locateEvent(state, seq, body);
    return found === null ? '' : `${found.wg} ${found.task}`;
  }, app.store);
  if (place === '') return null;
  const [wg, task] = place.split(' ') as [string, string];
  return <OpenLog source={{ wg, task, ref: runId, log }} label={label} />;
}
