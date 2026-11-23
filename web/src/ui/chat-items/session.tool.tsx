// Chat item `session.tool` (06-web.md section 5.2): a collapsed row "Read src/app.py"; expanded: the input and result
// summaries, the error flag and the duration.
import { isRecord } from '../../core/registry';
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useSeconds } from '../countdown/parts';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';

const text = (body: unknown, field: string): string =>
  isRecord(body) && typeof body[field] === 'string' ? body[field] : '';

function Tool({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t } = useI18n();
  const seconds = useSeconds();
  const failed = isRecord(body) && body.is_error === true;
  const duration = isRecord(body) && typeof body.duration_ms === 'number' ? body.duration_ms : null;
  return (
    <details className="rounded border border-gray-200 text-sm" data-testid="chat.tool" data-error={failed}>
      <summary className="flex min-h-11 cursor-pointer flex-wrap items-center gap-2 px-3 py-1">
        <span className="break-all font-mono" data-testid="chat.tool.summary">
          {text(body, 'name')} {text(body, 'input_summary')}
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
      <div className="flex flex-col gap-2 border-t border-gray-200 p-3">
        <h4 className="text-xs font-semibold">{t('task.chat.tool.input')}</h4>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs" data-testid="chat.tool.input">
          {text(body, 'input')}
        </pre>
        <h4 className="text-xs font-semibold">{t('task.chat.tool.result')}</h4>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs" data-testid="chat.tool.result">
          {text(body, 'result_summary')}
        </pre>
      </div>
    </details>
  );
}

export const chatItem: ChatItem = { kind: 'session.tool', Component: Tool };
