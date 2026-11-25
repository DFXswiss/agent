// The chat card of `browser.state` (06-web.md section 5.4b, ADR-085 section 7), a lazy chunk of the chat item
// `chat-items/browser.state.tsx` (the start of the app stays within its budget): one card per browser session (the timeline
// keeps its latest state, `core/chat/workvm.ts`): the state – starting with the countdown to the 10 s target and
// "Delayed" after it, open, started again or closed with its reason –, whether a browser tool opened it, the current
// page, the number of actions, when its VM was ready and "Watch live", which opens the session's Browser tab. A body
// that does not follow the contract is a neutral row with a code, never content.
import './texts';
import { Link, useParams } from 'react-router-dom';
import { type BrowserState, parseBrowserState } from '../../core/browser/shape';
import { browserStart } from '../../core/browser/sessions';
import { START_TARGET_MS } from '../../core/chat/workvm';
import type { TimelineItem } from '../../core/chat/timeline';
import { parseTime } from '../../core/encoding/time';
import type { ChatItemProps } from '../registries/chat-items';
import { Digits, Ring, useSeconds } from '../countdown/parts';
import { useNow } from '../countdown/useNow';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';
import { Invalid } from '../chat-items/workvm.run';

/** "Browser open", "Browser closed (idle)", … */
export function useBrowserStateTitle(): (state: BrowserState) => string {
  const { t } = useI18n();
  return (state) => {
    if (state.state === 'starting' || state.state === 'open') return t(`browser.state.${state.state}`);
    if (state.reason === null) return t(`browser.state.${state.state}.plain`);
    return t(`browser.state.${state.state}`, { reason: t(`browser.reason.${state.reason}`) });
  };
}

/** The countdown of a starting browser session (ticks while shown). */
function Starting({ state, at }: { readonly state: BrowserState; readonly at: number }) {
  const { t } = useI18n();
  const seconds = useSeconds();
  const now = useNow(true);
  const start = browserStart(state, at, now);
  return (
    <div className="flex flex-wrap items-center gap-4" data-testid="chat.browser.countdown">
      <Ring progress={start.remainingMs / START_TARGET_MS}>
        <Digits value={Math.ceil(start.remainingMs / 100) / 10} fractionDigits={1} />
      </Ring>
      <p className="text-sm" data-testid="chat.browser.countdown.text" data-overdue={start.overdue}>
        {t(start.overdue ? 'browser.card.overdue' : 'browser.card.starting', { seconds: seconds(start.elapsedMs) })}
      </p>
    </div>
  );
}

export default function StateCard({ item }: ChatItemProps) {
  const { at, body, sessionId } = item as TimelineItem;
  const { t } = useI18n();
  const seconds = useSeconds();
  const title = useBrowserStateTitle();
  const { org } = useParams();
  const state = parseBrowserState(body);
  if (state === null) return <Invalid />;
  const linked = sessionId !== null && org !== undefined;
  const closed = state.state === 'closed';
  return (
    <div
      className="flex flex-col gap-2 rounded border border-gray-200 p-3 text-sm"
      data-testid="chat.browser.state"
      data-state={state.state}
      data-browser={state.browser_id}
    >
      <p className="flex flex-wrap items-center gap-x-2">
        <span className="font-medium" data-testid="chat.browser.state.title">
          {title(state)}
        </span>
        {state.implicit && (
          <span className="text-xs text-gray-600" data-testid="chat.browser.state.implicit">
            {t('browser.card.implicit')}
          </span>
        )}
        <EventTime at={at} />
      </p>
      {state.state === 'starting' && <Starting state={state} at={at} />}
      {state.url !== null && state.url !== '' && (
        <p className="break-all" data-testid="chat.browser.state.url">
          <span className="text-gray-600">{t('browser.card.page')}: </span>
          {state.url}
        </p>
      )}
      <p className="flex flex-wrap gap-x-3 text-gray-700">
        <span data-testid="chat.browser.state.actions">{t('browser.card.actions', { count: state.actions })}</span>
        {state.ready_ms !== null && (
          <span data-testid="chat.browser.state.ready">
            {t('browser.card.ready', { seconds: seconds(state.ready_ms) })}
          </span>
        )}
        {state.opened_at !== null && (
          <span className="flex items-baseline gap-1" data-testid="chat.browser.state.opened">
            {t('browser.card.opened')} <EventTime at={parseTime(state.opened_at)} />
          </span>
        )}
      </p>
      {linked && (
        <Link
          className="min-h-11 self-start py-2 text-blue-800 underline"
          to={`/o/${encodeURIComponent(org)}/s/${encodeURIComponent(sessionId)}?tab=browser`}
          data-testid={closed ? 'browser.record.open' : 'browser.watch'}
        >
          {t(closed ? 'browser.record.open' : 'browser.watch')}
        </Link>
      )}
    </div>
  );
}
