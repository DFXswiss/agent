// The one building block for every list of the web client (ADR-086 section 1, 06-web.md section 12.4): renders only
// the window of `core/lists/window.ts` as an `<ol>`/`<ul>` of memoized rows.
//
// - Rows are `React.memo` with the list's own comparison (`same`), so new items render only the new rows; rows off
//   screen skip layout and paint (`content-visibility: auto` with an intrinsic size).
// - Both ends show what is not rendered as a button with its count ("4 812 more entries – show"), "Jump to the newest"
//   and – when the window reaches the loaded end and the server has more – "Load more" (server paging). Real buttons
//   of at least 44 × 44 px.
// - Live inflow never takes focus away: when follow mode would drop the row that has focus, the window becomes
//   anchored (`keepRow`). When the focused row leaves the list itself (its item was deleted), focus moves to the
//   nearest row still shown (the next one, else the previous one), else to the first control of the list, and the
//   list forgets the row (no anchor on a row that is gone).
// - After a move, focus goes to the first newly shown row; the reading position is kept by correcting the scroll
//   offset when rows are added or dropped above it (no reliance on `overflow-anchor`, which iOS Safari lacks).
// - Rows carry `aria-setsize`/`aria-posinset` of the whole list; the shown range is announced in a polite live region
//   after a user action only, never on live inflow.
import {
  Component,
  type ComponentType,
  memo,
  type ReactNode,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { formatNumber } from '../../core/i18n';
import type { ListId } from '../../core/lists/ids';
import {
  initialWindow,
  keepRow,
  type NewestAt,
  sameWindow,
  settleWindow,
  showNewer,
  showNewest,
  showOlder,
  windowConfig,
  type WindowState,
  windowRange,
} from '../../core/lists/window';
import { LiveRegion } from '../a11y/LiveRegion';
import { useI18n } from '../i18n';

export interface RowProps<T> {
  readonly item: T;
}

/** Extra attributes of a row's `<li>`: classes and `data-*` test hooks. */
export type RowAttributes = Readonly<{ className?: string } & Record<`data-${string}`, string | number | boolean>>;

/** Catalog keys of the texts (with `{count}`), defaults in `lists.*`. */
export interface ListLabels {
  readonly older?: string;
  readonly newer?: string;
  readonly newest?: string;
}

export type ListControl = 'older' | 'newer' | 'newest' | 'loadMore' | 'status';

/** Estimated row heights for `contain-intrinsic-size` (static classes: Tailwind compiles them from the source). */
const HEIGHT = {
  sm: '[contain-intrinsic-size:auto_3rem]',
  md: '[contain-intrinsic-size:auto_4rem]',
  lg: '[contain-intrinsic-size:auto_8rem]',
} as const;

const ROW =
  '[content-visibility:auto] focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700 scroll-mt-4';
const BUTTON = 'min-h-11 min-w-11 py-2 text-left text-sm text-blue-800 underline disabled:text-gray-500';

export interface WindowedListProps<T> {
  readonly listId: ListId;
  readonly items: readonly T[];
  readonly getKey: (item: T) => string;
  /** The row content (memoized here with `same`; a stable component, defined at module level). */
  readonly Row: ComponentType<RowProps<T>>;
  /** Whether two builds of an item render the same (default: the same object). */
  readonly same?: (a: T, b: T) => boolean;
  /** At which display end new items appear: `end` for chat and logs, `start` for newest-first lists. */
  readonly newest: NewestAt;
  /** Rows shown at first. */
  readonly size?: number;
  /** Rows added by a move. */
  readonly page: number;
  /** The most rows ever rendered. */
  readonly maxRows: number;
  /** Test id of the list; the controls are `<testId>.older` and so on unless `testIds` names them. */
  readonly testId: string;
  readonly testIds?: Partial<Record<ListControl, string>>;
  readonly labels?: ListLabels;
  readonly as?: 'ol' | 'ul';
  readonly className?: string;
  readonly rowClassName?: string;
  readonly rowAttributes?: (item: T) => RowAttributes;
  readonly rowHeight?: keyof typeof HEIGHT;
  /** Server paging: more items exist beyond the oldest loaded one. */
  readonly hasMore?: boolean;
  readonly loading?: boolean;
  readonly onLoadMore?: () => void;
  /** Size of the whole list when the server says it (else the loaded items, or unknown while `hasMore`). */
  readonly total?: number;
  /** Shown instead of the list when there are no items. */
  readonly empty?: ReactNode;
}

interface Snapshot {
  readonly row: Element;
  readonly top: number;
}

/** The element that scrolls `node`: the nearest scrollable ancestor, else the page. */
export function scrollParent(node: Element): Element | Window {
  for (let parent = node.parentElement; parent !== null; parent = parent.parentElement) {
    const { overflowY } = getComputedStyle(parent);
    if ((overflowY === 'auto' || overflowY === 'scroll') && parent.scrollHeight > parent.clientHeight) return parent;
  }
  return window;
}

interface KeeperProps {
  readonly list: RefObject<HTMLElement | null>;
  /** Keys of the rows shown after this update. */
  readonly shown: readonly string[];
  readonly children: ReactNode;
}

/**
 * Keeps the reading position when rows are added or dropped above: before the DOM changes it notes the screen
 * position of the first row that stays, after the change it scrolls by the distance that row moved.
 */
class ScrollKeeper extends Component<KeeperProps> {
  override getSnapshotBeforeUpdate(previous: KeeperProps): Snapshot | null {
    if (previous.shown[0] === this.props.shown[0]) return null;
    const stays = new Set(this.props.shown);
    for (const row of this.props.list.current!.children) {
      if (stays.has((row as HTMLElement).dataset.key!)) return { row, top: row.getBoundingClientRect().top };
    }
    return null;
  }

  override componentDidUpdate(_props: KeeperProps, _state: unknown, snapshot: Snapshot | null): void {
    if (snapshot === null) return;
    const delta = snapshot.row.getBoundingClientRect().top - snapshot.top;
    if (delta !== 0) scrollParent(snapshot.row).scrollBy(0, delta);
  }

  override render(): ReactNode {
    return this.props.children;
  }
}

/** Key of the row of `list` that contains `node` (an element inside the list). */
function rowKey(list: Element, node: Element): string {
  let row = node;
  while (row.parentElement !== list) row = row.parentElement!;
  return (row as HTMLElement).dataset.key!;
}

const rowElement = (list: HTMLElement, key: string): HTMLElement | undefined =>
  [...list.children].find((row) => (row as HTMLElement).dataset.key === key) as HTMLElement | undefined;

export function WindowedList<T>(props: WindowedListProps<T>) {
  const { items, getKey, Row, same, newest, page, maxRows, testId, labels = {}, hasMore = false } = props;
  const { t, locale } = useI18n();
  const config = useMemo(
    () => windowConfig({ size: props.size ?? page * 2, page, maxRows, newest }),
    [props.size, page, maxRows, newest],
  );
  const keys = useMemo(() => items.map(getKey), [items, getKey]);
  const MemoRow = useMemo(
    () => memo(Row, same === undefined ? undefined : (a, b) => same(a.item, b.item)),
    [Row, same],
  );
  const [state, setState] = useState<WindowState>(() => initialWindow(config));
  const [message, setMessage] = useState('');
  const list = useRef<HTMLOListElement & HTMLUListElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const focus = useRef<string | null>(null);
  const jump = useRef(false);
  /** The keys when "Load more" was used, until its page arrived. */
  const pending = useRef<ReadonlySet<string> | null>(null);
  // the row that holds focus (from focus events of the list)
  const [focused, setFocused] = useState<string | null>(null);
  /** The rows shown by the last commit (the neighbours of a removed focused row). */
  const lastShown = useRef<readonly string[]>([]);
  /** The removed focused row focus was already moved away from (once per row). */
  const moved = useRef<string | null>(null);
  // a focused row whose item left the list holds nothing any more (the effect below moves focus on)
  const holding = focused !== null && keys.includes(focused) ? focused : null;
  // the anchor follows the rows actually shown (state derived during render, no effect); a follow window keeps the
  // focused row
  const settled = keepRow(keys, state, settleWindow(keys, state, config), config, holding);
  if (!sameWindow(settled, state)) setState(settled);
  const range = windowRange(keys, settled, config);
  const n = keys.length;
  const id = (control: ListControl, fallback: string) => props.testIds?.[control] ?? `${testId}.${fallback}`;
  const count = (value: number) => formatNumber(value, locale);

  const announce = (next: WindowState) => {
    const shown = windowRange(keys, next, config);
    const total = props.total ?? n;
    setMessage(
      t(hasMore && props.total === undefined ? 'lists.range.loaded' : 'lists.range', {
        from: count(shown.from + 1),
        to: count(shown.to),
        total: count(total),
      }),
    );
  };

  /**
   * Moves the window and focuses the first row (display order) the move shows that was not shown before; when the move
   * shows no such row (a loaded page that fit into the window), the first shown row that is not in `known`. A loaded
   * page may insert its rows anywhere in the list (an order by time, not by the server's paging key), so the focus
   * target is always taken from the rows the new window shows – never an index outside it (KOR-35).
   */
  const move = (next: WindowState, known: ReadonlySet<string>) => {
    const shown = windowRange(keys, next, config);
    const target = keys.slice(shown.from, shown.to);
    const was = new Set(keys.slice(range.from, range.to));
    focus.current = target.find((key) => !was.has(key)) ?? target.find((key) => !known.has(key)) ?? null;
    // a user move may drop the focused row (it moves focus itself); only live inflow keeps it
    setFocused(null);
    setState(next);
    announce(next);
  };

  const toNewest = () => {
    const next = showNewest(config);
    jump.current = true;
    focus.current = null;
    setFocused(null);
    setState(next);
    announce(next);
  };

  const loadMore = () => {
    pending.current = new Set(keys);
    props.onLoadMore!();
  };

  // after a user move: focus the first newly shown row, or the newest row after a jump
  useLayoutEffect(() => {
    if (jump.current) {
      jump.current = false;
      const row = (newest === 'end' ? list.current!.lastElementChild : list.current!.firstElementChild) as HTMLElement;
      row.scrollIntoView({ block: newest });
      row.focus({ preventScroll: true });
      return;
    }
    if (focus.current === null) return;
    // live inflow rendered with the move may have dropped the row again (a follow window): focus then stays
    rowElement(list.current!, focus.current)?.focus({ preventScroll: true });
    focus.current = null;
  });

  // the focused row left the list: focus goes to the nearest row still shown, else to a control (module comment)
  useLayoutEffect(() => {
    const previous = lastShown.current;
    lastShown.current = keys.slice(range.from, range.to);
    if (focused === null || holding !== null || moved.current === focused) return;
    const key = focused;
    moved.current = key;
    if (document.activeElement !== document.body) return; // focus went elsewhere meanwhile
    const index = previous.indexOf(key);
    for (let distance = 1; distance < previous.length; distance += 1) {
      for (const near of [previous[index + distance], previous[index - distance]]) {
        const row = near === undefined || list.current === null ? undefined : rowElement(list.current, near);
        if (row !== undefined) return row.focus({ preventScroll: true });
      }
    }
    root.current?.querySelector<HTMLElement>('button:not([disabled])')?.focus({ preventScroll: true });
  });

  // a page loaded by "Load more" is shown as soon as it arrived (its rows may lie anywhere in the list)
  useEffect(() => {
    if (pending.current === null || props.loading === true) return;
    const known = pending.current;
    pending.current = null;
    if (keys.some((key) => !known.has(key))) move(showOlder(keys, settled, config), known);
    // `move` and `settled` belong to this render; the effect runs when the loaded items arrive
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys, props.loading, hasMore]);

  if (n === 0) return <>{props.empty ?? null}</>;

  const olderButton = range.older > 0 && (
    <button
      type="button"
      className={BUTTON}
      onClick={() => move(showOlder(keys, settled, config), new Set(keys))}
      data-testid={id('older', 'older')}
    >
      {t(labels.older ?? 'lists.older', { count: count(range.older) })}
    </button>
  );
  const newerButton = range.newer > 0 && (
    <button
      type="button"
      className={BUTTON}
      onClick={() => move(showNewer(keys, settled, config), new Set(keys))}
      data-testid={id('newer', 'newer')}
    >
      {t(labels.newer ?? 'lists.newer', { count: count(range.newer) })}
    </button>
  );
  const newestButton = (
    <button type="button" className={BUTTON} onClick={toNewest} data-testid={id('newest', 'newest')}>
      {t(labels.newest ?? 'lists.newest')}
    </button>
  );
  const loadButton = range.older === 0 && hasMore && props.onLoadMore !== undefined && (
    <button
      type="button"
      className={BUTTON}
      onClick={loadMore}
      disabled={props.loading === true}
      aria-busy={props.loading === true}
      data-testid={id('loadMore', 'load-more')}
    >
      {t(props.loading === true ? 'lists.loading' : 'lists.load-more')}
    </button>
  );
  // "Jump to the newest" sits at the newest end while newer rows are hidden, else at the oldest end
  const newerBar = range.newer > 0 && (
    <div className="flex flex-wrap gap-x-4">
      {newerButton}
      {newestButton}
    </div>
  );
  const olderBar = (range.older > 0 || loadButton !== false) && (
    <div className="flex flex-wrap gap-x-4">
      {olderButton}
      {loadButton}
      {range.newer === 0 && range.older > 0 && newestButton}
    </div>
  );
  const setsize = props.total ?? (hasMore ? -1 : n);
  const List = props.as ?? 'ul';
  const shownKeys = keys.slice(range.from, range.to);
  return (
    <div ref={root} className="flex flex-col gap-3" data-list={props.listId}>
      {newest === 'end' ? olderBar : newerBar}
      <ScrollKeeper list={list} shown={shownKeys}>
        <List
          ref={list}
          className={props.className}
          data-testid={testId}
          data-total={n}
          data-shown={shownKeys.length}
          data-mode={settled.mode}
          onFocus={(event) => setFocused(rowKey(event.currentTarget, event.target))}
          onBlur={() => setFocused(null)}
        >
          {items.slice(range.from, range.to).map((item, index) => {
            const key = shownKeys[index]!;
            const { className: extra = '', ...data } = props.rowAttributes?.(item) ?? {};
            return (
              <li
                key={key}
                data-key={key}
                tabIndex={-1}
                aria-setsize={setsize}
                aria-posinset={range.from + index + 1}
                className={`${ROW} ${HEIGHT[props.rowHeight ?? 'md']} ${props.rowClassName ?? ''} ${extra}`}
                {...data}
              >
                <MemoRow item={item} />
              </li>
            );
          })}
        </List>
      </ScrollKeeper>
      {newest === 'end' ? newerBar : olderBar}
      <LiveRegion text={message} testId={id('status', 'status')} />
    </div>
  );
}
