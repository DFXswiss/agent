// U-WEB-ui-lists (ADR-063 section 1): the shared windowed list – only the window is rendered, the hidden counts at
// both ends, moves by a page with focus on the first new row and a kept reading position, "Jump to the newest",
// server paging with "Load more", memoized rows, ARIA set positions and the live region after user actions only.
import { act, fireEvent, render, screen } from '@testing-library/react';
import { memo } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../../src/core/i18n';
import { I18nProvider } from '../../../src/ui/i18n';
import { scrollParent, WindowedList, type WindowedListProps } from '../../../src/ui/lists/WindowedList';

interface Item {
  readonly id: string;
  readonly text: string;
}

const renders: string[] = [];

function Row({ item }: { readonly item: Item }) {
  renders.push(item.id);
  return <span data-testid="row.text">{item.text}</span>;
}

const key = (item: Item) => item.id;
const sameText = (a: Item, b: Item) => a.text === b.text;
const make = (from: number, count: number): Item[] =>
  Array.from({ length: count }, (_, index) => ({ id: `k${from + index}`, text: `t${from + index}` }));
/** `count` items newest first: the newest has the highest number. */
const newestFirst = (count: number, from = 0) => make(from, count).reverse();
const texts = () => screen.queryAllByTestId('row.text').map((node) => node.textContent);
const rows = () => screen.getByTestId('list').querySelectorAll('li');

type Props = Partial<WindowedListProps<Item>> & { readonly items: readonly Item[] };

function List(props: Props) {
  return (
    <WindowedList<Item>
      listId="notifications"
      getKey={key}
      Row={Row}
      newest="start"
      size={4}
      page={2}
      maxRows={6}
      testId="list"
      {...props}
    />
  );
}

function mount(props: Props, locale: 'en' | 'de' = 'en') {
  const view = render(
    <I18nProvider value={createI18n(locale)}>
      <List {...props} />
    </I18nProvider>,
  );
  return {
    ...view,
    update: (next: Props) =>
      view.rerender(
        <I18nProvider value={createI18n(locale)}>
          <List {...next} />
        </I18nProvider>,
      ),
  };
}

afterEach(() => {
  renders.length = 0;
});

describe('a newest-first list', () => {
  it('renders the newest rows, shows older and newer rows by a page and jumps to the newest', () => {
    const items = newestFirst(20);
    const view = mount({ items });
    expect(texts()).toEqual(['t19', 't18', 't17', 't16']);
    const list = screen.getByTestId('list');
    expect(list.tagName).toBe('UL');
    expect(list).toHaveAttribute('data-total', '20');
    expect(list).toHaveAttribute('data-shown', '4');
    expect(list).toHaveAttribute('data-mode', 'follow');
    expect(list.parentElement).toHaveAttribute('data-list', 'notifications');
    expect(rows()[0]).toHaveAttribute('aria-setsize', '20');
    expect(rows()[1]).toHaveAttribute('aria-posinset', '2');
    expect(rows()[0]!.className).toContain('[contain-intrinsic-size:auto_4rem]');
    expect(screen.queryByTestId('list.newer')).toBeNull();
    expect(screen.getByTestId('list.status')).toHaveTextContent('');
    // the hidden older rows at the end
    expect(screen.getByTestId('list.older')).toHaveTextContent('16 more entries – show');
    fireEvent.click(screen.getByTestId('list.older'));
    expect(texts()).toEqual(['t19', 't18', 't17', 't16', 't15', 't14']);
    expect(document.activeElement).toBe(rows()[4]);
    expect(screen.getByTestId('list.status')).toHaveTextContent('Showing entries 1–6 of 20');
    // past maxRows the newest rows drop: newer rows are hidden at the start
    fireEvent.click(screen.getByTestId('list.older'));
    expect(texts()).toEqual(['t17', 't16', 't15', 't14', 't13', 't12']);
    expect(document.activeElement).toBe(rows()[4]);
    expect(list).toHaveAttribute('data-mode', 'anchored');
    expect(screen.getByTestId('list.newer')).toHaveTextContent('2 newer entries – show');
    expect(rows()[0]).toHaveAttribute('aria-posinset', '3');
    // new items do not move an anchored window, and the live region stays as it was
    view.update({ items: newestFirst(22) });
    expect(texts()).toEqual(['t17', 't16', 't15', 't14', 't13', 't12']);
    expect(screen.getByTestId('list.newer')).toHaveTextContent('4 newer entries – show');
    expect(screen.getByTestId('list.status')).toHaveTextContent('Showing entries 3–8 of 20');
    // newer rows by a page: focus on the first new row
    fireEvent.click(screen.getByTestId('list.newer'));
    expect(texts()).toEqual(['t19', 't18', 't17', 't16', 't15', 't14']);
    expect(document.activeElement).toBe(rows()[0]);
    // jump to the newest: the start row is scrolled to and focused
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    fireEvent.click(screen.getByTestId('list.newest'));
    expect(scroll).toHaveBeenCalledWith({ block: 'start' });
    expect(texts()).toEqual(['t21', 't20', 't19', 't18']);
    expect(document.activeElement).toBe(rows()[0]);
    expect(screen.getByTestId('list.status')).toHaveTextContent('Showing entries 1–4 of 22');
  });

  it('offers "Jump to the newest" at the oldest end while no newer rows are hidden', () => {
    mount({ items: newestFirst(10), as: 'ol', rowHeight: 'lg', rowClassName: 'extra' });
    expect(screen.getByTestId('list').tagName).toBe('OL');
    expect(rows()[0]!.className).toContain('[contain-intrinsic-size:auto_8rem] extra');
    fireEvent.click(screen.getByTestId('list.older'));
    const newest = screen.getByTestId('list.newest');
    expect(newest.parentElement).toBe(screen.getByTestId('list.older').parentElement);
  });

  it('shows the German texts with formatted counts', () => {
    mount({ items: newestFirst(1234) }, 'de');
    expect(screen.getByTestId('list.older')).toHaveTextContent('1.230 weitere Einträge – anzeigen');
  });

  it('shows the empty content or nothing', () => {
    const view = mount({ items: [], empty: <p data-testid="list.empty">none</p> });
    expect(screen.getByTestId('list.empty')).toBeInTheDocument();
    view.update({ items: [] });
    expect(view.container).toBeEmptyDOMElement();
  });
});

describe('an ascending list', () => {
  it('renders the newest rows at the end with custom labels, test ids and row attributes', () => {
    mount({
      items: make(0, 10),
      newest: 'end',
      rowHeight: 'sm',
      labels: { older: 'chat-list.earlier', newest: 'chat-list.latest' },
      testIds: { older: 'chat.earlier', newest: 'chat.latest' },
      rowAttributes: (item) => ({ className: 'own', 'data-testid': 'chat.item', 'data-seq': item.id }),
    });
    expect(texts()).toEqual(['t6', 't7', 't8', 't9']);
    expect(rows()[0]).toHaveAttribute('data-testid', 'chat.item');
    expect(rows()[0]).toHaveAttribute('data-seq', 'k6');
    expect(rows()[0]!.className).toContain('[contain-intrinsic-size:auto_3rem]');
    expect(rows()[0]!.className).toContain('own');
    expect(screen.getByTestId('chat.earlier')).toHaveTextContent('Show earlier items (6 more)');
    expect(screen.getByTestId('chat.latest')).toHaveTextContent('Jump to the latest item');
    fireEvent.click(screen.getByTestId('chat.earlier'));
    expect(texts()).toEqual(['t4', 't5', 't6', 't7', 't8', 't9']);
    expect(document.activeElement).toBe(rows()[0]);
    fireEvent.click(screen.getByTestId('chat.earlier'));
    expect(texts()).toEqual(['t2', 't3', 't4', 't5', 't6', 't7']);
    // newer rows hidden at the end
    expect(screen.getByTestId('list.newer')).toHaveTextContent('2 newer entries – show');
    fireEvent.click(screen.getByTestId('list.newer'));
    expect(texts()).toEqual(['t4', 't5', 't6', 't7', 't8', 't9']);
    expect(document.activeElement).toBe(rows()[4]);
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    fireEvent.click(screen.getByTestId('chat.earlier'));
    fireEvent.click(screen.getByTestId('chat.latest'));
    expect(scroll).toHaveBeenCalledWith({ block: 'end' });
    expect(document.activeElement).toBe(rows()[3]);
  });
});

describe('memoized rows', () => {
  it('renders only new rows; the list comparison decides for rebuilt items', () => {
    const view = mount({ items: newestFirst(3), same: sameText });
    expect(renders).toEqual(['k2', 'k1', 'k0']);
    renders.length = 0;
    // rebuilt objects with the same text: nothing renders again
    view.update({ items: [{ id: 'k3', text: 't3' }, ...newestFirst(3).map((item) => ({ ...item }))], same: sameText });
    expect(renders).toEqual(['k3']);
    renders.length = 0;
    view.update({ items: [{ id: 'k3', text: 'changed' }, ...newestFirst(3)], same: sameText });
    expect(renders).toEqual(['k3']);
    renders.length = 0;
    // without a comparison: by object identity
    const same = newestFirst(2);
    view.update({ items: same });
    renders.length = 0;
    view.update({ items: [...same] });
    expect(renders).toEqual([]);
  });

  it('keeps a stable row component', () => {
    const Stable = memo(Row);
    mount({ items: newestFirst(2), Row: Stable });
    expect(texts()).toEqual(['t1', 't0']);
  });
});

describe('server paging', () => {
  it('loads more at the loaded end and shows the new page', () => {
    const loadMore = vi.fn();
    const items = newestFirst(3, 10);
    const view = mount({ items, hasMore: true, onLoadMore: loadMore });
    expect(rows()[0]).toHaveAttribute('aria-setsize', '-1');
    const button = screen.getByTestId('list.load-more');
    expect(button).toHaveTextContent('Load more');
    fireEvent.click(button);
    expect(loadMore).toHaveBeenCalledTimes(1);
    view.update({ items, hasMore: true, loading: true, onLoadMore: loadMore });
    expect(screen.getByTestId('list.load-more')).toBeDisabled();
    expect(screen.getByTestId('list.load-more')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('list.load-more')).toHaveTextContent('Loading…');
    // the page arrived: the window shows it and focuses its first row
    const more = [...items, ...newestFirst(3, 7)];
    view.update({ items: more, hasMore: true, loading: false, onLoadMore: loadMore });
    expect(texts()).toEqual(['t12', 't11', 't10', 't9', 't8', 't7']);
    // t9 came in with the window's own size, t8 is the first row the move showed
    expect(document.activeElement).toBe(rows()[4]);
    expect(screen.getByTestId('list.status')).toHaveTextContent('Showing entries 1–6 of 6 loaded');
    // an empty page: nothing moves
    fireEvent.click(screen.getByTestId('list.load-more'));
    view.update({ items: more, hasMore: false, loading: false, onLoadMore: loadMore });
    expect(texts()).toEqual(['t12', 't11', 't10', 't9', 't8', 't7']);
    expect(screen.queryByTestId('list.load-more')).toBeNull();
  });

  it('uses the total the server reports', () => {
    // the default size is two pages
    render(
      <WindowedList<Item>
        listId="audit"
        items={newestFirst(10)}
        getKey={key}
        Row={Row}
        newest="start"
        page={2}
        maxRows={6}
        testId="list"
        hasMore
        total={500}
        rowAttributes={() => ({ 'data-x': 1 })}
      />,
    );
    expect(texts()).toEqual(['t9', 't8', 't7', 't6']);
    expect(rows()[0]).toHaveAttribute('aria-setsize', '500');
    expect(rows()[0]).toHaveAttribute('data-x', '1');
    // no loader: no "Load more"
    expect(screen.queryByTestId('list.load-more')).toBeNull();
    fireEvent.click(screen.getByTestId('list.older'));
    expect(screen.getByTestId('list.status')).toHaveTextContent('Showing entries 1–6 of 500');
  });
});

describe('reading position', () => {
  it('scrolls by the distance the first staying row moved when rows are added above', () => {
    const original = HTMLElement.prototype.getBoundingClientRect;
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
      const top = this.parentElement === null ? 0 : [...this.parentElement.children].indexOf(this) * 10;
      return { top } as DOMRect;
    };
    const scrollBy = vi.fn();
    window.scrollBy = scrollBy;
    try {
      mount({ items: make(0, 10), newest: 'end' });
      fireEvent.click(screen.getByTestId('list.older'));
      // t6 moved from position 0 to position 2
      expect(scrollBy).toHaveBeenCalledWith(0, 20);
      fireEvent.click(screen.getByTestId('list.older'));
      // t4 moved from 0 to 2 while t8 and t9 dropped below
      expect(scrollBy).toHaveBeenLastCalledWith(0, 20);
      scrollBy.mockClear();
      fireEvent.click(screen.getByTestId('list.newer'));
      // rows dropped above: t4 moved up from 2 to 0
      expect(scrollBy).toHaveBeenCalledWith(0, -20);
      scrollBy.mockClear();
      // a jump far away: no row stays, nothing to correct
      fireEvent.click(screen.getByTestId('list.older'));
      fireEvent.click(screen.getByTestId('list.older'));
      scrollBy.mockClear();
      Element.prototype.scrollIntoView = vi.fn();
      fireEvent.click(screen.getByTestId('list.newest'));
      expect(scrollBy).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.getBoundingClientRect = original;
    }
  });

  it('scrolls the nearest scrollable ancestor', () => {
    const outer = document.createElement('div');
    const flat = document.createElement('div');
    const box = document.createElement('div');
    const inner = document.createElement('div');
    const node = document.createElement('ul');
    outer.append(box);
    box.append(flat);
    flat.append(inner);
    inner.append(node);
    document.body.append(outer);
    // overflow without anything to scroll, and plain blocks, are skipped
    inner.style.overflowY = 'auto';
    box.style.overflowY = 'scroll';
    Object.defineProperty(box, 'scrollHeight', { value: 500 });
    Object.defineProperty(box, 'clientHeight', { value: 100 });
    expect(scrollParent(node)).toBe(box);
    box.style.overflowY = 'auto';
    expect(scrollParent(node)).toBe(box);
    box.style.overflowY = 'visible';
    expect(scrollParent(node)).toBe(window);
    outer.remove();
  });
});

describe('act around state derived in render', () => {
  it('settles the anchor when the list shrinks', () => {
    const view = mount({ items: newestFirst(10) });
    act(() => view.update({ items: newestFirst(2) }));
    expect(texts()).toEqual(['t1', 't0']);
  });
});
