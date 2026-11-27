// The texts of the area `login-check` (the check for humans of a Claude login in the device's sandbox and the reason
// why the automation gave up, KOR-73, ADR-100 section 4): used only by the lazily loaded login dialogs, so they are
// added to the catalogs when the first of them loads, not with the first load (06-web.md section 12.2). Import this
// module for its effect from every part that shows a `login-check.*` text.
import { extendCatalogs } from '..';
import de from './login-check.de.json';
import en from './login-check.en.json';

extendCatalogs('login-check', { de, en });
