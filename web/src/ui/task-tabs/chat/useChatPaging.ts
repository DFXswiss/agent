// A mounted chat (task or session chat, 06-web.md sections 5.2 and 5.5): the view counts as shown for the event reader
// and reads the newest page of its events (`core/chat/actions.ts` `openChat`); "Load more" at the start of the
// timeline reads the next older page (`loadEarlierChat`) – never every page in a loop (ADR-086 section 1).
import { useEffect, useMemo } from 'react';
import { chatList, loadEarlierChat, openChat } from '../../../core/chat/actions';
import type { ListPaging } from '../../../core/lists/window';
import { selectPages } from '../../../core/tasks/pages';
import { useStore } from '../../hooks/useStore';
import { useAppEnv } from '../../shell/context';

export function useChatPaging(org: string, wg: string, task: string, session?: string): ListPaging {
  const { app } = useAppEnv();
  const list = useMemo(() => chatList(org, wg, task, session), [org, wg, task, session]);
  const select = useMemo(() => selectPages(list), [list]);
  const page = useStore(select, app.store);
  useEffect(() => openChat(app.tasks, list), [app, list]);
  return useMemo(
    () => ({
      hasMore: page.status === 'ready' && page.before !== null,
      loading: page.earlier,
      loadMore: () => void loadEarlierChat(app.tasks, list),
    }),
    [app, list, page],
  );
}
