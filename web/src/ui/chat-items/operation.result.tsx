// Chat item `operation.result` (06-web.md section 5.2): a compact row when the operation finished ("Create pull request
// finished: Done"), linking the card's details; the card of the request (`operation.params`) carries the links.
import { Link } from 'react-router-dom';
import type { TimelineItem } from '../../core/chat/timeline';
import { isRecord } from '../../core/registry';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { EventTime } from '../task-tabs/chat/EventTime';
import { operationLink, useTypeLabel } from './operation/Card';
import { hasOperation, Malformed, useOperationItem } from './operation.params';

function Result({ item }: ChatItemProps) {
  const timeline = item as TimelineItem;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const typeLabel = useTypeLabel();
  const found = useOperationItem(timeline);
  const type = useStore(
    (state) => (found === null ? null : (state.operations.rows[found.operation]?.type ?? null)),
    app.store,
  );
  const status = isRecord(timeline.body) && typeof timeline.body.status === 'string' ? timeline.body.status : null;
  if (!hasOperation(timeline) || status === null) return <Malformed />;
  if (found === null) return null;
  return (
    <p className="text-center text-xs text-gray-600" data-testid="chat.operation-result" data-status={status}>
      <Link className="underline" to={operationLink(found.org, found.task, found.operation)}>
        {t('operations.result.row', {
          type: type === null ? t('operations.type.unknown') : typeLabel(type),
          state: t(`operations.state.${status}`),
        })}
      </Link>{' '}
      <EventTime at={timeline.at} />
    </p>
  );
}

export const chatItem: ChatItem = { kind: 'operation.result', Component: Result };
