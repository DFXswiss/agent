// U-WEB-ui-picker (ADR-086 section 1): the searchable picker of dialog choices – at most `limit` rows rendered, a search
// field only when there are more items, matching case-insensitively anywhere in the item's text, the count of matches
// beyond the limit and a note when nothing matches.
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createI18n } from '../../../src/core/i18n';
import { I18nProvider } from '../../../src/ui/i18n';
import { Picker, PICKER_LIMIT } from '../../../src/ui/lists/Picker';

interface Item {
  readonly id: string;
  readonly name: string;
}

const Row = ({ item }: { readonly item: Item }) => <span data-testid="pick.row">{item.name}</span>;
const make = (count: number): Item[] =>
  Array.from({ length: count }, (_, index) => ({ id: `id${index}`, name: `Name ${index}` }));
const shown = () => screen.queryAllByTestId('pick.row').map((node) => node.textContent);

function mount(items: readonly Item[], limit?: number, locale: 'en' | 'de' = 'en') {
  return render(
    <I18nProvider value={createI18n(locale)}>
      <Picker
        items={items}
        getKey={(item) => item.id}
        text={(item) => item.name}
        Row={Row}
        testId="pick"
        label="Search names"
        {...(limit === undefined ? {} : { limit })}
      />
    </I18nProvider>,
  );
}

describe('Picker', () => {
  it('shows every item without a search when they fit', () => {
    mount(make(3));
    expect(shown()).toEqual(['Name 0', 'Name 1', 'Name 2']);
    expect(screen.queryByTestId('pick.search')).toBeNull();
    expect(screen.queryByTestId('pick.more')).toBeNull();
    expect(screen.queryByTestId('pick.none')).toBeNull();
  });

  it('renders at most the default limit and narrows the list by search', () => {
    mount(make(1234));
    expect(shown()).toHaveLength(PICKER_LIMIT);
    expect(screen.getByTestId('pick.more')).toHaveTextContent('Showing 20 of 1,234 – type to narrow the list');
    const search = screen.getByTestId('pick.search');
    expect(search).toHaveAccessibleName('Search names');
    expect(search).toHaveAttribute('placeholder', 'Search');
    fireEvent.change(search, { target: { value: '  NAME 123' } });
    expect(shown()).toEqual(['Name 123', 'Name 1230', 'Name 1231', 'Name 1232', 'Name 1233']);
    expect(screen.queryByTestId('pick.more')).toBeNull();
    fireEvent.change(search, { target: { value: 'nobody' } });
    expect(shown()).toEqual([]);
    expect(screen.getByTestId('pick.none')).toHaveTextContent('Nothing matches the search');
    fireEvent.change(search, { target: { value: '' } });
    expect(shown()).toHaveLength(PICKER_LIMIT);
  });

  it('takes its own limit and the German texts', () => {
    mount(make(5), 2, 'de');
    expect(shown()).toEqual(['Name 0', 'Name 1']);
    expect(screen.getByTestId('pick.more')).toHaveTextContent('2 von 5 angezeigt – zum Eingrenzen tippen');
    expect(screen.getByTestId('pick.search')).toHaveAttribute('placeholder', 'Suchen');
  });
});
