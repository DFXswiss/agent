// The texts of the area `browser` (the platform browser's chat card and Browser tab, KOR-44): used only by the lazily
// loaded browser views, so they are added to the catalogs when the first of them loads, not with the first load
// (06-web.md section 12.2). Import this module for its effect from every part that shows a `browser.*` text.
import { extendCatalogs } from '..';
import de from './browser.de.json';
import en from './browser.en.json';

extendCatalogs('browser', { de, en });
