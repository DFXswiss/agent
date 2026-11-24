// Chat item `file-group` (06-web.md section 5.2, HOT-05): a run of file changes of one session as one row – "483 files
// changed" with the time span. "Show files" opens the list of paths, a `WindowedList` (ADR-086 section 1) of
// `FILES_PAGE` files at a time ("Show more files", at most `FILES_MAX` rendered); each file's patch opens on its own.
// Nothing of the list is in the DOM while the group is closed.
import { useId, useState } from 'react';
import type { FileGroupBody } from '../../core/chat/files';
import type { TimelineItem } from '../../core/chat/timeline';
import { formatNumber } from '../../core/i18n';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';
import { WindowedList } from '../lists/WindowedList';
import { FileChange } from './file/FileChange';

export const FILES_PAGE = 50;
export const FILES_MAX = 200;

const fileKey = (file: TimelineItem): string => file.key;

function File({ item }: { readonly item: TimelineItem }) {
  return <FileChange item={item} />;
}

const LINK = 'min-h-11 self-start py-2 text-left text-blue-800 underline';

function FileGroup({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { files, last } = body as FileGroupBody;
  const { t, locale } = useI18n();
  const id = useId();
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="chat.files" data-count={files.length}>
      <p className="flex flex-wrap items-center gap-2">
        <span data-testid="chat.files.summary">
          {t('chat-list.files.summary', { count: formatNumber(files.length, locale) })}
        </span>
        <EventTime at={at} />
        <span aria-hidden="true">–</span>
        <EventTime at={last} />
      </p>
      <button
        type="button"
        className={LINK}
        aria-expanded={open}
        {...(open && { 'aria-controls': id })}
        onClick={() => setOpen(!open)}
        data-testid="chat.files.toggle"
      >
        {t(open ? 'chat-list.files.hide' : 'chat-list.files.show')}
      </button>
      {open && (
        <div id={id}>
          {/* in the order of the run: the first files at the start, "Show more files" at the end */}
          <WindowedList
            listId="chat.files"
            items={files}
            getKey={fileKey}
            Row={File}
            newest="start"
            size={FILES_PAGE}
            page={FILES_PAGE}
            maxRows={FILES_MAX}
            className="flex flex-col gap-2 border-l-2 border-gray-200 pl-3"
            testId="chat.files.list"
            testIds={{ older: 'chat.files.more', newer: 'chat.files.previous', newest: 'chat.files.first' }}
            labels={{
              older: 'chat-list.files.more',
              newer: 'chat-list.files.previous',
              newest: 'chat-list.files.first',
            }}
          />
        </div>
      )}
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'file-group', Component: FileGroup };
