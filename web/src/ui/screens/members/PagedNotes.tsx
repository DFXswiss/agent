// What a paged hub list shows beside its rows (`usePagedList`): the note that the hub cut its unpaged answer at its cap,
// and "Load more" while no row is shown yet but older pages exist (a page whose rows the verified log filtered out
// entirely; the windowed list offers "Load more" only below a row).
import { useI18n } from '../../i18n';
import { BUTTON } from './ActionDialog';
import type { PagedList } from './usePagedList';

export function PagedNotes(props: {
  readonly list: Pick<PagedList<unknown>, 'truncated' | 'more' | 'paging'>;
  /** The number of rows shown. */
  readonly shown: number;
  /** The catalog key of the truncation note. */
  readonly note: string;
  /** The test id of the windowed rows (`<rows>.load-more` like the windowed list's own button). */
  readonly rows: string;
  readonly testId: string;
}) {
  const { list, shown } = props;
  const { t } = useI18n();
  return (
    <>
      {list.truncated && (
        <p className="rounded bg-gray-100 p-3 text-gray-800" role="note" data-testid={`${props.testId}.truncated`}>
          {t(props.note)}
        </p>
      )}
      {shown === 0 && list.more && (
        <button
          type="button"
          className={`${BUTTON} self-start`}
          disabled={list.paging.loading}
          aria-busy={list.paging.loading}
          onClick={list.paging.loadMore}
          data-testid={`${props.rows}.load-more`}
        >
          {t(list.paging.loading ? 'lists.loading' : 'lists.load-more')}
        </button>
      )}
    </>
  );
}
