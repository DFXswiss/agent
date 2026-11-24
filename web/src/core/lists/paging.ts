// Server paging of a windowed list (ADR-063 section 1): what a list's loader tells the view. A view loads at most what
// it shows plus one page ahead; "Load more" (or the window reaching the loaded end) asks for the next page.
export interface ListPaging {
  /** The server has items beyond the oldest loaded one. */
  readonly hasMore: boolean;
  /** A page is being loaded. */
  readonly loading: boolean;
  /** Loads the next older page. */
  readonly loadMore: () => void;
}
