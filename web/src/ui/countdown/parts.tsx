// Building blocks of the countdown panels (06-web.md section 4.4): the large digits with the progress ring and one
// status step. No spinner anywhere: the ring shows the remaining share and does not move by itself; with
// `prefers-reduced-motion` it changes without a transition (section 12.3).
import type { ReactNode } from 'react';
import type { StepStatus } from '../../core/tasks/countdown';
import { formatEventTime } from '../../core/i18n';
import { useI18n } from '../i18n';

const RADIUS = 44;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** The test-ID part of a step (`vm_ready` → `vm-ready`). */
export const stepId = (state: string): string => state.replace(/_/g, '-');

/** Seconds with one decimal in the active locale (`4.2`, `4,2`). */
export function useSeconds(): (ms: number) => string {
  const { locale } = useI18n();
  const format = new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return (ms: number) => format.format(Math.max(0, ms) / 1000);
}

export function Digits({ value, fractionDigits }: { readonly value: number; readonly fractionDigits: 0 | 1 }) {
  const { locale } = useI18n();
  const text = new Intl.NumberFormat(locale, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
  return (
    <span className="text-5xl font-semibold tabular-nums" data-testid="countdown.digits">
      {text}
    </span>
  );
}

/** The progress ring around the digits: `progress` 1 (full) → 0 (empty). */
export function Ring({ progress, children }: { readonly progress: number; readonly children: ReactNode }) {
  const offset = CIRCUMFERENCE * (1 - Math.min(1, Math.max(0, progress)));
  return (
    <div className="relative flex h-32 w-32 items-center justify-center">
      <svg
        className="absolute inset-0 -rotate-90"
        viewBox="0 0 100 100"
        aria-hidden="true"
        data-testid="countdown.ring"
      >
        <circle cx="50" cy="50" r={RADIUS} fill="none" strokeWidth="8" className="stroke-gray-200" />
        <circle
          cx="50"
          cy="50"
          r={RADIUS}
          fill="none"
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={CIRCUMFERENCE}
          strokeDashoffset={offset}
          className="stroke-blue-600 transition-[stroke-dashoffset] duration-100 motion-reduce:transition-none"
        />
      </svg>
      {children}
    </div>
  );
}

const MARK: Record<StepStatus, string> = { done: '✓', current: '→', failed: '✕', pending: '·' };
const STYLE: Record<StepStatus, string> = {
  done: 'text-gray-900',
  current: 'font-semibold text-blue-800',
  failed: 'font-semibold text-red-800',
  pending: 'text-gray-500',
};

export interface StepRowProps {
  readonly id: string;
  readonly label: string;
  readonly status: StepStatus;
  readonly at?: number | null;
  readonly elapsedMs?: number | null;
  /** Browser time (for the date prefix of a device time that is not today). */
  readonly now?: number;
}

/** One status step: mark, label, device time and elapsed time since task creation. */
export function StepRow({ id, label, status, at = null, elapsedMs = null, now = 0 }: StepRowProps) {
  const { locale, t } = useI18n();
  const seconds = useSeconds();
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <li
      className={`flex flex-wrap items-baseline gap-x-2 ${STYLE[status]}`}
      data-testid={`countdown.step.${id}`}
      data-status={status}
      aria-current={status === 'current' ? 'step' : undefined}
    >
      <span aria-hidden="true" className="w-4 text-center">
        {MARK[status]}
      </span>
      <span className="flex-1">{label}</span>
      {at !== null && (
        <time className="text-xs tabular-nums text-gray-600" dateTime={new Date(at).toISOString()}>
          {formatEventTime(at, { locale, timeZone, now })}
        </time>
      )}
      {elapsedMs !== null && (
        <span className="text-xs tabular-nums text-gray-600">
          {t('countdown.step.elapsed', { seconds: seconds(elapsedMs) })}
        </span>
      )}
    </li>
  );
}
