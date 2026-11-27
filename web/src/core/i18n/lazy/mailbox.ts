// The texts of the area `mailbox` (the view of the settings section "Mailboxes", KOR-74): used only by the lazily
// loaded view, so they are added to the catalogs when it loads, not with the first load (06-web.md section 12.2). The
// section's title and its loading texts stay in the area `mailboxes` of the first load.
import { extendCatalogs } from '..';
import de from './mailbox.de.json';
import en from './mailbox.en.json';

extendCatalogs('mailbox', { de, en });
