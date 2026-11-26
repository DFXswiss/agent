// The texts of the area `mainagent` (names and handles of main agents, the situation view by person, the agent actors
// of the audit, ADR-095): used only by lazily loaded parts – the situation view, the task lists, the task page and the
// audit – so they are added to the catalogs when the first of them loads, not with the first load (06-web.md
// section 12.2). Import this module for its effect from every part that shows a `mainagent.*` text.
import { extendCatalogs } from '..';
import de from './mainagent.de.json';
import en from './mainagent.en.json';

extendCatalogs('mainagent', { de, en });
