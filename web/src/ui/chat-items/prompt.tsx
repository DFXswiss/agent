// Chat item `prompt` (06-web.md section 5.2): a right-aligned bubble with the author's name (the verified author of the
// event, named from the org's verified `member.profile`; a person of DFX support as "Support <Name>", ADR-096), plain
// text with its bare URLs as links (`LinkedText`, never Markdown), `CLIP_CHARS` characters at a time (HOT-05). A prompt the queue of the task names as withdrawn is marked
// "Withdrawn", "Being withdrawn" or "Could not be withdrawn" (it had already reached the AI; ADR-083 section 3).
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import { selectWithdrawal } from '../../core/store/slices/queue';
import { selectDirectory } from '../../core/tasks/directory';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { EventTime } from '../task-tabs/chat/EventTime';
import { LinkedText } from '../task-tabs/chat/LinkedText';
import { useClipped } from '../lists/Clipped';
import { knownPersonName } from '../names/names';
import { useAuthorOverride, useSupportLabel } from '../names/support';

function Prompt({ item }: ChatItemProps) {
  const { at, author, body, eventId } = item as TimelineItem;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const org = useStore((state) => state.session.activeOrg ?? '', app.store);
  const directory = useStore(selectDirectory(org), app.store);
  const me = useStore((state) => state.me.userId, app.store);
  const withdrawal = useStore(selectWithdrawal(eventId!), app.store);
  // the support area names authors from `support.info`; a member sees a support person as "Support <Name>" (ADR-096)
  const override = useAuthorOverride(author);
  const support = useSupportLabel(org, author);
  const name =
    author === me
      ? t('task.chat.author.you')
      : (override ?? knownPersonName(t, directory, author!) ?? support ?? t('task.chat.author.unknown'));
  const { shown, more } = useClipped(isRecord(body) && typeof body.text === 'string' ? body.text : '', {
    controls: 'chat',
  });
  return (
    <div
      className="ml-auto flex max-w-[85%] flex-col items-end gap-1"
      data-testid="chat.prompt"
      data-withdrawal={withdrawal ?? undefined}
    >
      <span className="text-xs text-gray-600">
        <span data-testid="chat.prompt.author">{name}</span> <EventTime at={at} />
      </span>
      <p className="whitespace-pre-wrap break-words rounded-lg bg-blue-50 px-3 py-2" data-testid="chat.prompt.text">
        <LinkedText text={shown} />
      </p>
      {more}
      {withdrawal !== null && (
        <span className="text-xs text-gray-600" data-testid="chat.prompt.withdrawal">
          {t(`queue.prompt.${withdrawal}`)}
        </span>
      )}
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'prompt', Component: Prompt };
