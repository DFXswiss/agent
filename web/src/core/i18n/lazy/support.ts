// The texts of the area `support` (the release section and dialog of the workgroup settings and the support area,
// ADR-096): used only by lazily loaded parts, so they are added to the catalogs when the first of them loads, not with
// the first load (06-web.md section 12.2). Import this module for its effect from every part that shows a `support.*`
// text; the navigation label of the first load is the area `supportnav`.
import { extendCatalogs } from '..';
import de from './support.de.json';
import en from './support.en.json';

extendCatalogs('support', { de, en });
