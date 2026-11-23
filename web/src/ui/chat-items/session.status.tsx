// Chat item `session.status` (06-web.md section 5.2): system rows – session errors (`session.status` events with the
// failure reason from the catalog), the transitions `suspended`/`stopped`/`failed` and the close of the task. A valid
// `waiting` event (KOR-25) is the waiting card (`waiting/WaitingCard.tsx`); a `waiting` body the contract does not allow
// stays a neutral row that shows nothing of it.
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';
import { WaitingCard } from './waiting/WaitingCard';

const PLAIN = new Set(['suspended', 'stopped', 'closed']);

function Status({ item }: ChatItemProps) {
  const { at, body, source, waiting } = item as TimelineItem;
  const { t, error } = useI18n();
  if (waiting !== undefined) return <WaitingCard item={item as TimelineItem} card={waiting} />;
  const state = isRecord(body) && typeof body.state === 'string' ? body.state : '';
  const reason = isRecord(body) && typeof body.reason === 'string' ? body.reason : null;
  let text: string;
  if (state === 'failed') text = t('task.chat.status.failed', { reason: error(reason ?? 'unavailable').title });
  else if (PLAIN.has(state)) text = t(`task.chat.status.${state}`);
  else text = t('task.chat.status.other', { state });
  return (
    <p
      className={`text-center text-xs ${state === 'failed' ? 'text-red-800' : 'text-gray-600'}`}
      data-testid="chat.status"
      data-state={state}
      data-source={source}
    >
      {text} <EventTime at={at} />
    </p>
  );
}

export const chatItem: ChatItem = { kind: 'session.status', Component: Status };
