// The texts of the area `subscription-login` (adding, logging in and managing Claude subscriptions, KOR-69, KOR-73):
// used only by lazily loaded parts – the settings section "Claude subscriptions" and the page "My subscriptions" – so
// they are added to the catalogs when the first of them loads, not with the first load (06-web.md section 12.2).
// Import this module for its effect from every part that shows a `subscription-login.*` text.
import { extendCatalogs } from '..';
import de from './subscription-login.de.json';
import en from './subscription-login.en.json';

extendCatalogs('subscription-login', { de, en });
