// Chat item `session.file` (06-web.md section 5.2): "Changed `path`" (or "Deleted") with the patch expandable in
// monospace, without syntax highlighting. Runs of file changes are one `file-group` item (`file-group.tsx`).
import type { TimelineItem } from '../../core/chat/timeline';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { FileChange } from './file/FileChange';

function SingleFile({ item }: ChatItemProps) {
  return <FileChange item={item as TimelineItem} />;
}

export const chatItem: ChatItem = { kind: 'session.file', Component: SingleFile };
