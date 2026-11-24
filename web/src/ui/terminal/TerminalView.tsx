// The read-only terminal of a session (06-web.md sections 5.5 and 12.1, ADR-016), loaded lazily with the session
// detail. The session is watched while the view is open (`core/terminal/watch.ts`: live `terminal` frames, the history
// pulled page by page, chunks strictly in `scope_seq` order). Two modes:
//
// - **Follow** (default): every newly applied chunk is written to the visible screen (live tail);
// - **History**: a slider over the recording range. Moving it replays the output from the start up to the chosen
//   offset into the hidden second screen and then swaps the screens; playback continues from there at 1×, 4× or 16×.
//   Back to Follow, the hidden screen gets the whole output and is swapped in the same way.
//
// There is no input path: the screens have input disabled and no handlers (`xterm.ts`), paste is cancelled; selection
// and copy work. The label "Read-only view – prompts are entered in the chat" is always visible. The screens keep the
// recorded geometry and scroll horizontally on narrow screens; the font follows the container width.
import { type ChangeEvent, useEffect, useRef, useState } from 'react';
import { formatEventTime } from '../../core/i18n';
import { absoluteAt, type Chunk, geometry, outputOf, outputUntil, recordingRange } from '../../core/terminal/chunks';
import { Player, type Speed, SPEEDS } from '../../core/terminal/replay';
import { TerminalWatch } from '../../core/terminal/watch';
import { useNow } from '../countdown/useNow';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { clearScreen, fitFont, openScreen, type Screen } from './xterm';

export interface TerminalViewProps {
  readonly org: string;
  readonly wg: string;
  readonly session: string;
}

type Mode = 'follow' | 'history';

const NO_CHUNKS: readonly Chunk[] = [];

interface Screens {
  readonly a: Screen;
  readonly b: Screen;
  readonly hosts: { readonly a: HTMLElement; readonly b: HTMLElement };
  front: 'a' | 'b';
  /** Chunks written to the front screen in Follow mode. */
  written: number;
  cols: number;
  rows: number;
  /** Fits the font of both screens to the container. */
  fit: () => void;
  /** A replay into the hidden screen is running; the newest further request waits in `queued`. */
  swapping: boolean;
  queued: { readonly make: () => string; readonly done: () => void } | null;
}

const backOf = (screens: Screens): 'a' | 'b' => (screens.front === 'a' ? 'b' : 'a');

/**
 * Writes `data` to screen `which`; the host's `data-chars` counts the characters written since the last clear (an
 * observable fact for the tests, the content itself stays in the canvas).
 */
function write(screens: Screens, which: 'a' | 'b', data: string, done?: () => void): void {
  const host = screens.hosts[which];
  host.dataset.chars = String(Number(host.dataset.chars) + data.length);
  screens[which].terminal.write(data, done);
}

function clear(screens: Screens, which: 'a' | 'b'): void {
  screens.hosts[which].dataset.chars = '0';
  clearScreen(screens[which]);
}

/** "m:ss.s" of a recording offset. */
/** The history position as absolute local time `HH:MM:SS.mmm` (06-web.md section 12.4), if the chunks tell it. */
export function PositionTime({ chunks, position }: { readonly chunks: readonly Chunk[]; readonly position: number }) {
  const { locale } = useI18n();
  const now = useNow(false);
  const at = absoluteAt(chunks, position);
  if (at === null) return null;
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <time
      className="text-xs tabular-nums text-gray-600"
      dateTime={new Date(at).toISOString()}
      data-testid="terminal.position.time"
    >
      {formatEventTime(at, { locale, timeZone, now })}
    </time>
  );
}

export function offsetLabel(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  const seconds = ((ms % 60_000) / 1000).toFixed(1).padStart(4, '0');
  return `${minutes}:${seconds}`;
}

export default function TerminalView({ org, wg, session }: TerminalViewProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const entry = useStore((state) => state.terminal.sessions[session] ?? null, app.store);
  const chunks: readonly Chunk[] = entry === null ? NO_CHUNKS : entry.chunks;
  const [mode, setMode] = useState<Mode>('follow');
  const [frontId, setFrontId] = useState<'a' | 'b'>('a');
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(1);
  const hostA = useRef<HTMLDivElement>(null);
  const hostB = useRef<HTMLDivElement>(null);
  const screens = useRef<Screens | null>(null);
  const latest = useRef<readonly Chunk[]>(chunks);
  const player = useRef<Player | null>(null);

  useEffect(() => {
    const watch = new TerminalWatch(app.terminal, { org, wg, session });
    watch.start();
    return () => watch.stop();
  }, [app, org, wg, session]);

  useEffect(() => {
    const { cols, rows } = geometry(latest.current);
    const hosts = { a: hostA.current!, b: hostB.current! };
    hosts.a.dataset.chars = '0';
    hosts.b.dataset.chars = '0';
    const mounted: Screens = {
      a: openScreen(hosts.a, cols, rows),
      b: openScreen(hosts.b, cols, rows),
      hosts,
      front: 'a',
      written: 0,
      cols,
      rows,
      fit: () => {
        const size = fitFont(mounted[mounted.front]);
        mounted.a.terminal.options.fontSize = size;
        mounted.b.terminal.options.fontSize = size;
      },
      swapping: false,
      queued: null,
    };
    screens.current = mounted;
    const fit = mounted.fit;
    fit();
    window.addEventListener('resize', fit);
    player.current = new Player({
      chunks: () => latest.current,
      onOutput: (data) => write(mounted, mounted.front, data),
      onPosition: (at, running) => {
        setPosition(at);
        setPlaying(running);
      },
      now: () => performance.now(),
    });
    return () => {
      window.removeEventListener('resize', fit);
      player.current!.pause();
      mounted.a.terminal.dispose();
      mounted.b.terminal.dispose();
      screens.current = null;
    };
  }, []);

  // Follow: the recorded geometry, then every chunk not written yet, in order
  useEffect(() => {
    latest.current = chunks;
    const mounted = screens.current!;
    const { cols, rows } = geometry(chunks);
    if (cols !== mounted.cols || rows !== mounted.rows) {
      mounted.cols = cols;
      mounted.rows = rows;
      mounted.a.terminal.resize(cols, rows);
      mounted.b.terminal.resize(cols, rows);
      mounted.fit();
    }
    if (mode !== 'follow' || chunks.length <= mounted.written) return;
    write(mounted, mounted.front, outputOf(chunks.slice(mounted.written)));
    mounted.written = chunks.length;
  }, [chunks, mode]);

  /**
   * Writes `make()` into the hidden screen, then shows it. Replays never overlap (a reset does not cancel writes that
   * are still queued in xterm): a request during a running replay waits, and only the newest waiting one runs.
   */
  const swapIn = (make: () => string, done: () => void) => {
    const mounted = screens.current!;
    if (mounted.swapping) {
      mounted.queued = { make, done };
      return;
    }
    mounted.swapping = true;
    const run = (data: string, finish: () => void) => {
      const hidden = backOf(mounted);
      clear(mounted, hidden);
      write(mounted, hidden, data, () => {
        const next = mounted.queued;
        if (next !== null) {
          mounted.queued = null;
          run(next.make(), next.done);
          return;
        }
        mounted.swapping = false;
        mounted.front = hidden;
        setFrontId(hidden);
        finish();
      });
    };
    run(make(), done);
  };

  const seek = (ms: number) => {
    player.current!.pause();
    swapIn(
      () => outputUntil(latest.current, ms),
      () => player.current!.seek(ms),
    );
  };

  const toHistory = () => {
    if (mode === 'history') return;
    setMode('history');
    // the visible screen already shows everything: the playback starts at the end of the recording
    player.current!.seek(recordingRange(latest.current)?.endMs ?? 0);
  };

  const toFollow = () => {
    if (mode === 'follow') return;
    player.current!.pause();
    let all = latest.current;
    swapIn(
      () => {
        all = latest.current;
        return outputOf(all);
      },
      () => {
        screens.current!.written = all.length;
        setMode('follow');
      },
    );
  };

  const range = recordingRange(chunks);
  const bad = entry?.bad ?? [];
  return (
    <section className="flex flex-col gap-2" data-testid="terminal" data-mode={mode} data-chunks={chunks.length}>
      <p className="rounded bg-amber-50 px-3 py-2 text-sm font-medium text-amber-900" data-testid="terminal.label">
        {t('terminal.label')}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label={t('terminal.mode.label')} className="flex gap-1">
          {(['follow', 'history'] as const).map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={mode === item}
              className={`min-h-11 min-w-11 rounded border px-3 ${mode === item ? 'border-blue-700 bg-blue-50' : 'border-gray-300'}`}
              onClick={item === 'follow' ? toFollow : toHistory}
              data-testid={`terminal.mode.${item}`}
            >
              {t(`terminal.mode.${item}`)}
            </button>
          ))}
        </div>
        {mode === 'history' && range !== null && (
          <div className="flex flex-1 flex-wrap items-center gap-2" data-testid="terminal.history">
            <button
              type="button"
              className="min-h-11 min-w-11 rounded border border-gray-300 px-3"
              onClick={() => (playing ? player.current!.pause() : player.current!.play())}
              data-testid="terminal.play"
              data-playing={playing}
            >
              {playing ? t('terminal.pause') : t('terminal.play')}
            </button>
            <div role="group" aria-label={t('terminal.speed.label')} className="flex gap-1">
              {SPEEDS.map((item) => (
                <button
                  key={item}
                  type="button"
                  aria-pressed={speed === item}
                  className={`min-h-11 min-w-11 rounded border px-2 ${speed === item ? 'border-blue-700 bg-blue-50' : 'border-gray-300'}`}
                  onClick={() => {
                    setSpeed(item);
                    player.current!.setSpeed(item);
                  }}
                  data-testid={`terminal.speed.${item}`}
                >
                  {t('terminal.speed', { speed: item })}
                </button>
              ))}
            </div>
            <input
              type="range"
              className="min-h-11 min-w-40 flex-1"
              min={range.startMs}
              max={range.endMs}
              step={100}
              value={Math.min(range.endMs, Math.max(range.startMs, position))}
              aria-label={t('terminal.slider')}
              onChange={(event: ChangeEvent<HTMLInputElement>) => seek(Number(event.target.value))}
              data-testid="terminal.slider"
            />
            <span className="text-xs tabular-nums text-gray-600" data-testid="terminal.position">
              {t('terminal.position', { position: offsetLabel(position), end: offsetLabel(range.endMs) })}
            </span>
            <PositionTime chunks={chunks} position={position} />
          </div>
        )}
      </div>
      {entry?.status === 'loading' && (
        <p className="text-sm text-gray-600" data-testid="terminal.history-loading">
          {t('terminal.loading')}
        </p>
      )}
      {entry?.status === 'error' && (
        <p className="text-sm text-red-800" role="alert" data-testid="terminal.error" data-code={entry.code}>
          {t('terminal.failed', { reason: error(entry.code!).title })}
        </p>
      )}
      {entry?.status === 'ready' && chunks.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="terminal.empty">
          {t('terminal.empty')}
        </p>
      )}
      {bad.length > 0 && (
        <p className="text-sm text-gray-600" data-testid="terminal.unverifiable" data-count={bad.length}>
          {t('terminal.unverifiable', { count: bad.length, code: bad[0]!.code })}
        </p>
      )}
      <div
        className="touch-pan-x touch-pan-y touch-pinch-zoom overflow-x-auto rounded bg-black p-1"
        role="region"
        aria-label={t('terminal.viewport')}
        onPasteCapture={(event) => event.preventDefault()}
        data-testid="terminal.viewport"
      >
        <div className="relative">
          <div
            ref={hostA}
            className={frontId === 'a' ? '' : 'invisible absolute inset-0'}
            data-testid="terminal.screen.a"
          />
          <div
            ref={hostB}
            className={frontId === 'b' ? '' : 'invisible absolute inset-0'}
            data-testid="terminal.screen.b"
          />
        </div>
      </div>
    </section>
  );
}
