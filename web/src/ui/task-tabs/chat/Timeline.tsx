// The chat timeline (06-web.md section 5.2): one row per item of `core/chat/timeline.ts`, rendered by the chat-item
// registry (`ui/chat-items/<kind>.tsx`); kinds without a renderer are not shown (operation cards follow in WEB-09).
// Unverifiable events are a neutral row with the reader's code, never content. Subagent items are indented and
// labelled with their place in the session tree ("Subagent 1.2").
import type { TimelineItem } from '../../../core/chat/timeline';
import { useI18n } from '../../i18n';
import { chatItems } from '../../registries/chat-items';
import { EventTime } from './EventTime';

const RENDERERS = chatItems();

function Row({ item }: { readonly item: TimelineItem }) {
  const { t } = useI18n();
  if (item.kind === 'unverifiable')
    return (
      <li
        className="rounded border border-dashed border-gray-300 px-3 py-2 text-sm text-gray-500"
        data-testid="chat.item"
        data-kind="unverifiable"
        data-code={item.code!}
      >
        <EventTime at={item.at} /> {t('task.chat.unverifiable', { code: item.code! })}
      </li>
    );
  const renderer = RENDERERS.get(item.kind);
  if (renderer === undefined) return null;
  const subagent = item.subagent;
  return (
    <li
      className={subagent === null ? '' : 'ml-4 border-l-2 border-gray-200 pl-3 sm:ml-8'}
      data-testid="chat.item"
      data-kind={item.kind}
      data-seq={item.seq}
    >
      {subagent !== null && (
        <span className="text-xs font-medium text-gray-600" data-testid="chat.item.subagent">
          {t('task.chat.subagent', { path: subagent.path.join('.') })}
        </span>
      )}
      <renderer.Component item={item} />
    </li>
  );
}

export function Timeline({ items, empty }: { readonly items: readonly TimelineItem[]; readonly empty: string }) {
  if (items.length === 0)
    return (
      <p className="text-sm text-gray-600" data-testid="chat.empty">
        {empty}
      </p>
    );
  return (
    <ol className="flex flex-col gap-3" data-testid="chat.timeline">
      {items.map((item) => (
        <Row key={item.key} item={item} />
      ))}
    </ol>
  );
}
