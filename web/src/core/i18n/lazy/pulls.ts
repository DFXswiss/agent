// The texts of the area `pulls` (the pull-request view, the task's tab, the line in the situation view, KOR-48): used
// only by lazily loaded parts, so they are added to the catalogs when the first of them loads, not with the first load
// (06-web.md section 12.2). Import this module for its effect from every part that shows a `pulls.*` text; the
// navigation label of the first load is the area `pullnav`.
import { extendCatalogs } from '..';
import de from './pulls.de.json';
import en from './pulls.en.json';

extendCatalogs('pulls', { de, en });
