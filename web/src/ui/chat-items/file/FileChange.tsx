// One file change (06-web.md section 5.2): "Changed `path`" (or "Deleted") with its time and the patch behind a
// button (`FilePatch`); the chat item `session.file` and each file of a `file-group`.
import { isRecord } from '../../../core/registry';
import type { TimelineItem } from '../../../core/chat/timeline';
import { useI18n } from '../../i18n';
import { EventTime } from '../../task-tabs/chat/EventTime';
import { FilePatch } from './FilePatch';

const OPS = new Set(['write', 'edit', 'delete']);

export function FileChange({ item }: { readonly item: TimelineItem }) {
  const { at, body } = item;
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
      {patch !== null && <FilePatch patch={patch} />}
    </div>
  );
}
