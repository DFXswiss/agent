// Chat item `session.tool` (06-web.md section 5.2): a collapsed row "Read src/app.py"; expanded: the input and result
// summaries, the error flag and the duration. The expanded part is rendered only while open and each summary at most
// `CLIP_CHARS` characters at a time (HOT-05: tool calls with large inputs or outputs).
import { useState } from 'react';
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useSeconds } from '../countdown/parts';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';
import { Clipped } from './file/Clipped';

const text = (body: unknown, field: string): string =>
  isRecord(body) && typeof body[field] === 'string' ? body[field] : '';

/** The summary line shows at most `LINE_CHARS` characters of each part (the expanded view has the full input). */
export const LINE_CHARS = 300;
const line = (value: string): string => (value.length > LINE_CHARS ? `${value.slice(0, LINE_CHARS)}…` : value);

function Tool({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t } = useI18n();
  const seconds = useSeconds();
  const [open, setOpen] = useState(false);
  const failed = isRecord(body) && body.is_error === true;
  const duration = isRecord(body) && typeof body.duration_ms === 'number' ? body.duration_ms : null;
  return (
    <details
      className="rounded border border-gray-200 text-sm"
      data-testid="chat.tool"
      data-error={failed}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex min-h-11 cursor-pointer flex-wrap items-center gap-2 px-3 py-1">
        <span className="break-all font-mono" data-testid="chat.tool.summary">
          {line(text(body, 'name'))} {line(text(body, 'input_summary'))}
        </span>
        {failed && (
          <span className="rounded bg-red-100 px-1 text-xs text-red-900" data-testid="chat.tool.error">
            {t('task.chat.tool.error')}
          </span>
        )}
        {duration !== null && (
          <span className="text-xs text-gray-500">{t('task.chat.tool.duration', { seconds: seconds(duration) })}</span>
        )}
        <EventTime at={at} />
      </summary>
      {open && (
        <div className="flex flex-col gap-2 border-t border-gray-200 p-3">
          <h4 className="text-xs font-semibold">{t('task.chat.tool.input')}</h4>
          <Clipped
            text={text(body, 'input')}
            className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs"
            testId="chat.tool.input"
          />
          <h4 className="text-xs font-semibold">{t('task.chat.tool.result')}</h4>
          <Clipped
            text={text(body, 'result_summary')}
            className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs"
            testId="chat.tool.result"
          />
        </div>
      )}
    </details>
  );
}

export const chatItem: ChatItem = { kind: 'session.tool', Component: Tool };
