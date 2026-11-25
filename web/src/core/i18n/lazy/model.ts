// The texts of the area `model` (model access, fallback, quotas, model switches, the card `quota_choice`): used only
// by lazily loaded parts – the settings sections, the task page and the situation view – so they are added to the
// catalogs when the first of them loads, not with the first load (06-web.md section 12.2). Import this module for its
// effect from every part that shows a `model.*` text.
import { extendCatalogs } from '..';
import de from './model.de.json';
import en from './model.en.json';

extendCatalogs('model', { de, en });
