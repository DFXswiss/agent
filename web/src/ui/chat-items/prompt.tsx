// Chat item `prompt` (06-web.md section 5.2): a right-aligned bubble with the author's name (the verified author of the
// event, named from the org's verified `member.profile`), plain text, `CLIP_CHARS` characters at a time (HOT-05).
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import { selectDirectory } from '../../core/tasks/directory';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { EventTime } from '../task-tabs/chat/EventTime';
import { useClipped } from './file/Clipped';

function Prompt({ item }: ChatItemProps) {
  const { at, author, body } = item as TimelineItem;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const org = useStore((state) => state.session.activeOrg ?? '', app.store);
  const members = useStore(selectDirectory(org), app.store).members;
  const me = useStore((state) => state.me.userId, app.store);
  const name = author === me ? t('task.chat.author.you') : (members.get(author!) ?? t('task.chat.author.unknown'));
  const { shown, more } = useClipped(isRecord(body) && typeof body.text === 'string' ? body.text : '');
  return (
    <div className="ml-auto flex max-w-[85%] flex-col items-end gap-1" data-testid="chat.prompt">
      <span className="text-xs text-gray-600">
        <span data-testid="chat.prompt.author">{name}</span> <EventTime at={at} />
      </span>
      <p className="whitespace-pre-wrap break-words rounded-lg bg-blue-50 px-3 py-2" data-testid="chat.prompt.text">
        {shown}
      </p>
      {more}
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'prompt', Component: Prompt };
