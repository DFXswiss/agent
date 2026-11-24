// A hub list read page by page (ADR-086 section 2, `core/org/pages.ts`): the first page, the next one on "Load more",
// and – whenever `version` changes (a change, a revocation, the presence poll) – the range loaded so far read again
// from the newest page. A new `read` (another org or workgroup) starts over with one page and shows nothing of the
// previous list. Answers can arrive out of order (a slow poll after a change): only the newest request's answer is
// shown, and a page loaded by "Load more" is dropped when a re-read started meanwhile.
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ListPaging } from '../../../core/lists/window';
import { actionErrorCode } from '../../../core/org/batch';
import { appendRows, type ListPage, readPages } from '../../../core/org/pages';

type Read<T> = (before: string | null) => Promise<ListPage<T>>;

export interface PagedList<T> {
  /** The loaded rows; null before the first answer. */
  readonly rows: T[] | null;
  /** The error code of the last failed read. */
  readonly code: string | null;
  /** Older pages exist. */
  readonly more: boolean;
  /** The hub cut its (unpaged) answer at its cap. */
  readonly truncated: boolean;
  readonly paging: ListPaging;
}

/** What the last answers of the list `read` hold. */
interface Loaded<T> {
  readonly read: Read<T>;
  readonly rows: T[] | null;
  readonly next: string | null;
  readonly truncated: boolean;
  readonly code: string | null;
}

/** `read` must be stable per list; `id` is a module-level row key; nothing is read while `active` is false. */
export function usePagedList<T>(read: Read<T>, id: (row: T) => string, version: unknown, active = true): PagedList<T> {
  const [loaded, setLoaded] = useState<Loaded<T> | null>(null);
  const [loading, setLoading] = useState(false);
  // the loaded range in pages, and the list it belongs to
  const range = useRef({ read, pages: 1 });
  const latest = useRef(0);
  useEffect(() => {
    if (!active) return;
    if (range.current.read !== read) range.current = { read, pages: 1 };
    latest.current += 1;
    const request = latest.current;
    readPages(read, range.current.pages, id).then(
      (found) => {
        if (request !== latest.current) return;
        range.current.pages = found.pages;
        setLoaded({ read, rows: found.rows, next: found.nextBefore, truncated: found.truncated, code: null });
      },
      (failure: unknown) => {
        if (request !== latest.current) return;
        const code = actionErrorCode(failure);
        // a failed re-read keeps the rows shown so far
        setLoaded((before) =>
          before?.read === read ? { ...before, code } : { read, rows: null, next: null, truncated: false, code },
        );
      },
    );
  }, [read, id, version, active]);
  const current = loaded?.read === read ? loaded : null;
  const next = current?.next ?? null;
  // offered only while `next` is set (the windowed list's button, `PagedNotes`)
  const loadMore = useCallback(() => {
    const request = latest.current;
    setLoading(true);
    read(next!)
      .then(
        (page) => {
          if (request !== latest.current) return;
          range.current.pages += 1;
          setLoaded((before) => ({
            ...before!,
            rows: appendRows(before!.rows!, page.rows, id),
            next: page.nextBefore,
            truncated: before!.truncated || page.truncated,
            code: null,
          }));
        },
        (failure: unknown) => {
          if (request === latest.current) setLoaded((before) => ({ ...before!, code: actionErrorCode(failure) }));
        },
      )
      .finally(() => setLoading(false));
  }, [read, id, next]);
  return {
    rows: current?.rows ?? null,
    code: current?.code ?? null,
    more: next !== null,
    truncated: current?.truncated ?? false,
    paging: { hasMore: next !== null, loading, loadMore },
  };
}
