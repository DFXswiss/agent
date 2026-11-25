// The texts of the area `subscriptions` (the card "My subscriptions" of the home page and its page, KOR-68): used only
// by lazily loaded parts, so they are added to the catalogs when the first of them loads (06-web.md section 12.2).
// Import this module for its effect from every part that shows a `subscriptions.*` text.
import { extendCatalogs } from '..';
import de from './subscriptions.de.json';
import en from './subscriptions.en.json';

extendCatalogs('subscriptions', { de, en });
