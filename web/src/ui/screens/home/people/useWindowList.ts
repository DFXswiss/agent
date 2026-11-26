// The window of a long list paged by the hub (HOT-05, ADR-095 section 5; the approach of the situation view's groups,
// `AttentionGroup.tsx`): at most `WINDOW` entries in the DOM. "Show later" (also when the end of the list becomes
// visible) moves the window by `STEP`, loading the hub's next page when the loaded entries run out; "Show earlier"
// moves it back. The window follows its first entry and keeps its place when that entry leaves.
import { type RefObject, useEffect, useRef, useState } from 'react';
import { earlierFrom, groupWindow, type GroupWindow, laterFrom, STEP, windowAt } from '../../../../core/attention/view';

export interface WindowList {
  readonly span: GroupWindow;
  /** The element after the last entry (observed: when it becomes visible, the window moves on). */
  readonly end: RefObject<HTMLLIElement>;
  /** Whether there are later entries (loaded or at the hub). */
  readonly later: boolean;
  readonly more: () => Promise<void>;
  readonly earlier: () => void;
}

/**
 * `ids` in the list's order; `hasMore` whether the hub has a further page; `loadMore` loads it; `current` reads the
 * entries after a load (the store has changed by then).
 */
export function useWindowList(
  ids: readonly string[],
  hasMore: boolean,
  loadMore: () => Promise<unknown>,
  current: () => readonly string[],
): WindowList {
  const [view, setView] = useState<{ start: string | null; from: number }>({ start: null, from: 0 });
  const span = groupWindow(ids, view.start, view.from);
  // state derived during render: the window follows its first entry
  const want = windowAt(ids, span.from);
  if (want.start !== view.start || want.from !== view.from) setView(want);
  const end = useRef<HTMLLIElement>(null);
  const more = async () => {
    if (ids.length - span.to < STEP) await loadMore();
    const next = current();
    setView(windowAt(next, laterFrom(next, span.from)));
  };
  const latest = useRef(more);
  useEffect(() => {
    latest.current = more;
  });
  const later = span.to < ids.length || hasMore;
  useEffect(() => {
    const target = end.current;
    if (!later || target === null || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void latest.current();
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [later]);
  const earlier = () => setView(windowAt(ids, earlierFrom(span.from)));
  return { span, end, later, more, earlier };
}
