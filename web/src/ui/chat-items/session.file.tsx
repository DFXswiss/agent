// Chat item `session.file` (06-web.md section 5.2): "Changed `path`" (or "Deleted") with the patch expandable in
// monospace, without syntax highlighting.
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';

const OPS = new Set(['write', 'edit', 'delete']);

function FileChange({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t } = useI18n();
  const path = isRecord(body) && typeof body.path === 'string' ? body.path : '';
  const op = isRecord(body) && typeof body.op === 'string' && OPS.has(body.op) ? body.op : 'edit';
  const patch = isRecord(body) && typeof body.patch === 'string' ? body.patch : null;
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="chat.file" data-op={op}>
      <p className="flex flex-wrap items-center gap-2">
        <span>{t(`task.chat.file.${op}`)}</span>
        <code className="break-all rounded bg-gray-100 px-1" data-testid="chat.file.path">
          {path}
        </code>
        <EventTime at={at} />
      </p>
      {patch !== null && (
        <details>
          <summary className="min-h-11 cursor-pointer py-2 text-blue-800 underline">
            {t('task.chat.file.patch')}
          </summary>
          <pre
            className="max-h-96 overflow-auto rounded bg-gray-50 p-2 font-mono text-xs"
            data-testid="chat.file.patch"
          >
            {patch}
          </pre>
        </details>
      )}
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'session.file', Component: FileChange };
