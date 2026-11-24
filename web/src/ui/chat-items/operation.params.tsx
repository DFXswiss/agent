// Chat item `operation.params` (06-web.md section 5.2): the operation card at the time the session requested it –
// type, live state, subject, result links once there, duplicates (`operation/Card.tsx`).
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import { locateOperation } from '../../core/operations/view';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { EventTime } from '../task-tabs/chat/EventTime';
import { OperationCard } from './operation/Card';

/** Whether the body of an `operation.*` item names its operation. */
export const hasOperation = (item: TimelineItem): boolean =>
  isRecord(item.body) && typeof item.body.operation_id === 'string';

/** The operation of an `operation.*` item and where its event lives (null when the store lost it). */
export function useOperationItem(item: TimelineItem) {
  const { app } = useAppEnv();
  const operation = isRecord(item.body) && typeof item.body.operation_id === 'string' ? item.body.operation_id : null;
  // a text key keeps the selected value stable between store updates
  const place = useStore((state) => {
    const found = operation === null ? null : locateOperation(state, item.seq, operation);
    return found === null ? '' : `${found.org} ${found.wg} ${found.task}`;
  }, app.store);
  if (place === '') return null;
  const [org, wg, task] = place.split(' ') as [string, string, string];
  return { org, wg, task, operation: operation! };
}

/** A verified body that does not name its operation: a neutral row with the code, never content (06-web.md §1). */
export function Malformed() {
  const { t } = useI18n();
  return (
    <p className="text-sm text-gray-500" data-testid="chat.operation.invalid">
      {t('task.chat.unverifiable', { code: 'params_invalid' })}
    </p>
  );
}

function Params({ item }: ChatItemProps) {
  const found = useOperationItem(item as TimelineItem);
  if (!hasOperation(item as TimelineItem)) return <Malformed />;
  if (found === null) return null;
  return (
    <div className="flex flex-col gap-1" data-testid="chat.operation">
      <EventTime at={(item as TimelineItem).at} />
      <OperationCard org={found.org} wg={found.wg} task={found.task} operation={found.operation} />
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'operation.params', Component: Params };
