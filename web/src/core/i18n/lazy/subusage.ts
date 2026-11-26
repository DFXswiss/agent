// The texts of the area `subusage` (the overview "Who used how much" of the page "My subscriptions", ADR-100): used
// only by that lazily loaded part, so they are added to the catalogs when it loads (06-web.md section 12.2). Import
// this module for its effect from every part that shows a `subusage.*` text.
import { extendCatalogs } from '..';
import de from './subusage.de.json';
import en from './subusage.en.json';

extendCatalogs('subusage', { de, en });
