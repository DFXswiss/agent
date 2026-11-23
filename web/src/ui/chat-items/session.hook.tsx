// Chat item `session.hook` (06-web.md section 5.2): only `deny` decisions reach the timeline (`core/chat/timeline.ts`)
// and are shown as a muted row "Blocked: <tool> (<reason>)".
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';

function Hook({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t } = useI18n();
  const tool = isRecord(body) && typeof body.tool === 'string' ? body.tool : t('task.chat.hook.tool.unknown');
  const reason = isRecord(body) && typeof body.reason === 'string' ? body.reason : null;
  return (
    <p className="text-sm text-gray-500" data-testid="chat.hook">
      {reason === null ? t('task.chat.hook.blocked', { tool }) : t('task.chat.hook.blocked.reason', { tool, reason })}{' '}
      <EventTime at={at} />
    </p>
  );
}

export const chatItem: ChatItem = { kind: 'session.hook', Component: Hook };
