// Chat item `session.output` (06-web.md section 5.2): one assistant message as Markdown (section 2.1 rules; loaded
// lazily, plain text until then and when the chunk cannot be loaded); a cut message says so. A very long message
// shows `CLIP_CHARS` characters at a time, the rest behind "Show more" (HOT-05); the Markdown is built only once the
// row comes near the viewport (ADR-086 section 1), before that the message's first line is shown.
import { lazy, Suspense } from 'react';
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { ChunkBoundary } from '../lazy/ChunkBoundary';
import { EventTime } from '../task-tabs/chat/EventTime';
import { useClipped } from '../lists/Clipped';
import { Near } from '../lists/near';

const Markdown = lazy(() => import('../task-tabs/chat/Markdown'));

function Output({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t } = useI18n();
  const { shown: text, more } = useClipped(isRecord(body) && typeof body.text === 'string' ? body.text : '', {
    controls: 'chat',
  });
  return (
    <div className="flex max-w-full flex-col gap-1" data-testid="chat.output">
      <EventTime at={at} />
      <Near text={text}>
        <ChunkBoundary fallback={<p className="whitespace-pre-wrap break-words">{text}</p>}>
          <Suspense fallback={<p className="whitespace-pre-wrap break-words">{text}</p>}>
            <Markdown text={text} />
          </Suspense>
        </ChunkBoundary>
      </Near>
      {more}
      {isRecord(body) && body.truncated === true && (
        <p className="text-xs text-gray-500" data-testid="chat.output.truncated">
          {t('task.chat.output.truncated')}
        </p>
      )}
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'session.output', Component: Output };
