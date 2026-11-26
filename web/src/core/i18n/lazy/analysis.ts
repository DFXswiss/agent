// The texts of the area `analysis` (process analysis, ADR-097: the button and its dialog, the badge, the locked
// profile, the orchestrator reason and "Adopt"): used only by lazily loaded screens, so they are added to the catalogs
// when the first of them loads, not with the first load (06-web.md section 12.2). Import this module for its effect
// from every part that shows an `analysis.*` text.
import { extendCatalogs } from '..';
import de from './analysis.de.json';
import en from './analysis.en.json';

extendCatalogs('analysis', { de, en });
