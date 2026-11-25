// The Browser tab of the session detail (06-web.md section 5.4b, ADR-085 sections 6 and 7). While it is open the
// session's platform browser is watched (`core/browser/watch.ts`):
//
// - the **picture**: the live frame of the session's newest open browser session, labelled "Live" (with its age once
//   it is older than 30 s), else the newest recorded picture, labelled with its time; scaled to the width (phones
//   included). Watch only: the picture has no input handler, nothing reaches the page;
// - the **bandwidth choice** (Economical, Standard, Sharp), kept in this browser and sent as `browser_kbps`;
// - the **action record**, newest first, in a window of `RECORD_ROWS` rows (never the whole record in the page):
//   time, tool and action, target, typed text (as stored: passwords are masked by the runner), page, title, result or
//   error, notice and the picture after the action – read and decoded only for the rows in the window;
// - the note that the work VM's own browser is neither recorded nor live (E-09).
//
// Pictures are shown from object URLs of the decrypted JPEG bytes, revoked as soon as a row leaves the window or the
// picture changes. The record's position text is announced (`aria-live`) only after the user moved the window.
import { type ChangeEvent, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { type Bandwidth, BANDWIDTH_IDS, bandwidthOf, kbpsOf } from '../../core/browser/bandwidth';
import { frameAge, showsAge } from '../../core/browser/live';
import type { ActionRecord, RecordEntry } from '../../core/browser/record';
import { newestOpen, selectBrowserStates } from '../../core/browser/sessions';
import { BrowserWatch } from '../../core/browser/watch';
import { formatRuntime } from '../../core/chat/workvm';
import { formatEventTime, localTimeZone } from '../../core/i18n';
import { useSeconds } from '../countdown/parts';
import { useNow } from '../countdown/useNow';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

/** Rows of the record in the page at a time. */
export const RECORD_ROWS = 20;

export interface BrowserTabProps {
  readonly org: string;
  readonly wg: string;
  readonly task: string;
  readonly session: string;
}

/** An object URL of JPEG bytes, revoked when the bytes change or the component goes. */
export function useObjectUrl(bytes: Uint8Array | null): string | null {
  const url = useMemo(
    () =>
      bytes === null ? null : URL.createObjectURL(new Blob([bytes as Uint8Array<ArrayBuffer>], { type: 'image/jpeg' })),
    [bytes],
  );
  useEffect(() => (url === null ? undefined : () => URL.revokeObjectURL(url)), [url]);
  return url;
}

/** The picture of a record entry (read on demand); undefined while it is being read. */
function usePicture(record: ActionRecord, entry: RecordEntry | null): Uint8Array | null | undefined {
  const [picture, setPicture] = useState<{ seq: number; bytes: Uint8Array | null } | null>(null);
  const seq = entry?.action?.image != null ? entry.scopeSeq : null;
  useEffect(() => {
    if (seq === null) return;
    let current = true;
    void record.picture(seq).then((bytes) => {
      if (current) setPicture({ seq, bytes });
    });
    return () => {
      current = false;
    };
  }, [record, seq]);
  if (seq === null) return null;
  return picture?.seq === seq ? picture.bytes : undefined;
}

function useTime(): (at: number) => string {
  const { locale } = useI18n();
  const now = useNow(false);
  return (at) => formatEventTime(at, { locale, timeZone: localTimeZone(), now });
}

/** The live picture of the newest open browser session, else the newest recorded one. */
function Picture({ watch, current }: { readonly watch: BrowserWatch; readonly current: string | null }) {
  const { t, error } = useI18n();
  const time = useTime();
  const view = useSyncExternalStore(watch.subscribe, () => watch.view);
  const frame = view.live.frame !== null && view.live.frame.browser === current ? view.live.frame : null;
  const now = useNow(frame !== null, 1000);
  const entries = view.record.entries;
  const recorded = useMemo(() => {
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      if (entries[index]!.action?.image != null) return entries[index]!;
    }
    return null;
  }, [entries]);
  const recordedBytes = usePicture(watch.record, frame === null ? recorded : null);
  const url = useObjectUrl(frame?.jpeg ?? recordedBytes ?? null);
  const age = frame === null ? 0 : frameAge(frame.ts, now);
  const action = recorded?.action ?? null;
  const shown =
    frame !== null
      ? { source: 'live', seq: frame.seq, browser: frame.browser, alt: t('browser.live.alt') }
      : action !== null && recordedBytes != null
        ? {
            source: 'record',
            seq: action.seq,
            browser: action.browser_id,
            alt: t('browser.recorded.alt', { action: action.action }),
          }
        : null;
  return (
    <figure className="flex flex-col gap-1" data-testid="browser.picture">
      <figcaption className="flex flex-wrap items-center gap-2 text-sm">
        {frame !== null ? (
          <>
            <span className="rounded bg-red-700 px-2 py-0.5 font-semibold text-white" data-testid="browser.live.badge">
              {t('browser.live.badge')}
            </span>
            {showsAge(age) && (
              <span className="text-amber-800" data-testid="browser.live.age">
                {t('browser.live.age', { age: formatRuntime(age) })}
              </span>
            )}
          </>
        ) : (
          recorded !== null && (
            <span className="text-gray-700" data-testid="browser.live.badge">
              {t('browser.recorded.badge', { time: time(recorded.at) })}
            </span>
          )
        )}
      </figcaption>
      {url !== null && shown !== null ? (
        <img
          className="h-auto w-full max-w-full rounded border border-gray-300"
          src={url}
          alt={shown.alt}
          draggable={false}
          data-testid="browser.live"
          data-source={shown.source}
          data-seq={shown.seq}
          data-browser={shown.browser}
        />
      ) : (
        <p className="text-sm text-gray-600" data-testid="browser.picture.none">
          {t(recorded !== null && recordedBytes === undefined ? 'browser.picture.loading' : 'browser.picture.none')}
        </p>
      )}
      {view.live.error !== null && (
        <p className="text-xs text-gray-600" data-testid="browser.live.failed">
          {t('browser.live.failed', { reason: error(view.live.error).title })}
        </p>
      )}
    </figure>
  );
}

/** The picture of one record row (read and decoded while the row is in the window). */
function RowPicture({ record, entry }: { readonly record: ActionRecord; readonly entry: RecordEntry }) {
  const { t } = useI18n();
  const bytes = usePicture(record, entry);
  const url = useObjectUrl(bytes ?? null);
  const action = entry.action!;
  if (action.image === null) {
    return (
      <span className="text-xs text-gray-500" data-testid="browser.record.no-picture">
        {t(
          action.image_omitted === null
            ? 'browser.record.no-picture'
            : `browser.record.omitted.${action.image_omitted}`,
        )}
      </span>
    );
  }
  if (url === null) {
    return (
      <span className="text-xs text-gray-500" data-testid="browser.record.picture.loading">
        {t('browser.picture.loading')}
      </span>
    );
  }
  return (
    <img
      className="h-auto w-full max-w-md rounded border border-gray-200"
      src={url}
      alt={t('browser.recorded.alt', { action: action.action })}
      width={action.image.width}
      height={action.image.height}
      loading="lazy"
      draggable={false}
      data-testid="browser.record.picture"
    />
  );
}

function Field({ label, value, testId }: { readonly label: string; readonly value: string; readonly testId: string }) {
  return (
    <div className="flex min-w-0 gap-1">
      <dt className="shrink-0 text-gray-600">{label}:</dt>
      <dd className="min-w-0 break-all" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

function Row({ record, entry }: { readonly record: ActionRecord; readonly entry: RecordEntry }) {
  const { t } = useI18n();
  const time = useTime();
  const seconds = useSeconds();
  const action = entry.action;
  if (action === null) {
    return (
      <li className="text-sm text-gray-500" data-testid="browser.record.row" data-seq={entry.scopeSeq}>
        {t('browser.record.unreadable', { code: entry.code! })}
      </li>
    );
  }
  return (
    <li
      className="flex flex-col gap-1 border-b border-gray-200 py-2 text-sm"
      data-testid="browser.record.row"
      data-seq={action.seq}
      data-tool={action.tool}
      data-ok={action.ok}
    >
      <p className="flex flex-wrap items-baseline gap-x-2">
        <time className="text-xs tabular-nums text-gray-600" dateTime={action.at}>
          {time(entry.at)}
        </time>
        <span className="font-mono font-medium" data-testid="browser.record.action">
          {action.tool} · {action.action}
        </span>
        <span className={action.ok ? 'text-green-800' : 'text-red-800'} data-testid="browser.record.result">
          {action.ok
            ? t('browser.record.ok')
            : action.error === null
              ? t('browser.record.error.plain')
              : t('browser.record.error', { error: action.error })}
        </span>
        <span className="text-xs text-gray-600">
          {t('browser.record.duration', { seconds: seconds(action.duration_ms) })}
        </span>
      </p>
      <dl className="flex flex-col gap-0.5">
        {action.target !== null && (
          <Field label={t('browser.record.target')} value={action.target} testId="browser.record.target" />
        )}
        {action.text !== null && (
          <Field label={t('browser.record.text')} value={action.text} testId="browser.record.text" />
        )}
        <Field label={t('browser.record.url')} value={action.url} testId="browser.record.url" />
        {action.title !== '' && (
          <Field label={t('browser.record.title')} value={action.title} testId="browser.record.title" />
        )}
        {action.notice !== null && (
          <Field label={t('browser.record.notice')} value={action.notice} testId="browser.record.notice" />
        )}
      </dl>
      <RowPicture record={record} entry={entry} />
    </li>
  );
}

/** The window of the record, newest first. */
function Record({ watch }: { readonly watch: BrowserWatch }) {
  const { t, error } = useI18n();
  const view = useSyncExternalStore(watch.subscribe, () => watch.view);
  const [offset, setOffset] = useState(0);
  const [moved, setMoved] = useState(false);
  const { entries, loading, error: failed } = view.record;
  const total = entries.length;
  const start = Math.min(offset, Math.max(0, total - 1));
  const rows: RecordEntry[] = [];
  for (let index = start; index < Math.min(total, start + RECORD_ROWS); index += 1) {
    rows.push(entries[total - 1 - index]!);
  }
  const move = (to: number) => {
    setOffset(Math.max(0, to));
    setMoved(true);
  };
  return (
    <section
      className="flex flex-col gap-2"
      aria-labelledby="browser-record-title"
      data-testid="browser.record.section"
    >
      <h2 id="browser-record-title" className="text-base font-semibold">
        {t('browser.record.heading')}
      </h2>
      {failed !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="browser.record.failed">
          {t('browser.record.failed', { reason: error(failed).title })}
        </p>
      )}
      {loading && (
        <p className="text-sm text-gray-600" data-testid="browser.record.loading">
          {t('browser.record.loading', { count: total })}
        </p>
      )}
      {total === 0 && !loading && (
        <p className="text-sm text-gray-600" data-testid="browser.record.empty">
          {t('browser.record.empty')}
        </p>
      )}
      {total > 0 && (
        <p
          className="text-sm text-gray-700"
          data-testid="browser.record.range"
          aria-live={moved ? 'polite' : undefined}
        >
          {t('browser.record.range', { from: start + 1, to: start + rows.length, total })}
        </p>
      )}
      <ol className="flex flex-col" data-testid="browser.record">
        {rows.map((entry) => (
          <Row key={entry.scopeSeq} record={watch.record} entry={entry} />
        ))}
      </ol>
      {total > RECORD_ROWS && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-3 disabled:opacity-50"
            disabled={start === 0}
            onClick={() => move(0)}
            data-testid="browser.record.newest"
          >
            {t('browser.record.newest')}
          </button>
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-3 disabled:opacity-50"
            disabled={start === 0}
            onClick={() => move(start - RECORD_ROWS)}
            data-testid="browser.record.newer"
          >
            {t('browser.record.newer')}
          </button>
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-3 disabled:opacity-50"
            disabled={start + RECORD_ROWS >= total}
            onClick={() => move(start + RECORD_ROWS)}
            data-testid="browser.record.older"
          >
            {t('browser.record.older')}
          </button>
        </div>
      )}
    </section>
  );
}

/** The watch the effect started (an external store, so the effect does not set React state). */
class Running {
  #watch: BrowserWatch | null = null;
  readonly #listeners = new Set<() => void>();

  get = (): BrowserWatch | null => this.#watch;

  set(watch: BrowserWatch | null): void {
    this.#watch = watch;
    for (const listener of [...this.#listeners]) listener();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };
}

export function BrowserTab({ org, wg, task, session }: BrowserTabProps) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [bandwidth, setBandwidth] = useState<Bandwidth>(() => bandwidthOf(app.prefs.browserBandwidth()));
  const states = useStore(
    useMemo(() => selectBrowserStates(wg, task, session), [wg, task, session]),
    app.store,
  );
  const current = newestOpen(states);
  const [running] = useState(() => new Running());
  const watch = useSyncExternalStore(running.subscribe, running.get);
  useEffect(() => {
    // the stored choice is the current one: `choose` changes the running watch, it does not start a new one
    const kbps = kbpsOf(bandwidthOf(app.prefs.browserBandwidth()));
    const started = new BrowserWatch(app.browser, { org, wg, task, session }, kbps);
    started.start();
    running.set(started);
    return () => {
      running.set(null);
      started.stop();
    };
  }, [app, running, org, wg, task, session]);
  const refused = useSyncExternalStore(
    (listener) => watch?.subscribe(listener) ?? (() => undefined),
    () => watch?.view.refused ?? false,
  );
  const choose = (event: ChangeEvent<HTMLSelectElement>) => {
    const next = bandwidthOf(event.target.value);
    setBandwidth(next);
    app.prefs.setBrowserBandwidth(next);
    watch?.setKbps(kbpsOf(next));
  };
  return (
    <section className="flex flex-col gap-4" aria-labelledby="browser-tab-title" data-testid="browser.tab">
      <h2 id="browser-tab-title" className="sr-only">
        {t('browser.tab.title')}
      </h2>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-gray-700" data-testid="browser.watch-only">
          {t('browser.tab.watch-only')}
        </p>
        <label className="flex items-center gap-2 text-sm">
          {t('browser.kbps.label')}
          <select
            className="min-h-11 rounded border border-gray-300 px-2"
            value={bandwidth}
            onChange={choose}
            data-testid="browser.kbps"
          >
            {BANDWIDTH_IDS.map((id) => (
              <option key={id} value={id}>
                {t(`browser.kbps.${id}`)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {refused && (
        <p role="alert" className="text-sm text-red-800" data-testid="browser.refused">
          {t('browser.tab.refused')}
        </p>
      )}
      {states.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="browser.none">
          {t('browser.tab.none')}
        </p>
      )}
      {watch !== null && <Picture watch={watch} current={current} />}
      {watch !== null && <Record watch={watch} />}
      <p className="text-xs text-gray-600" data-testid="browser.note.workvm">
        {t('browser.note.workvm')}
      </p>
    </section>
  );
}
