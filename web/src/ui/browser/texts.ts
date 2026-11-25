// The texts of the platform browser's views (chat card and Browser tab), loaded with their lazy chunk
// (`core/i18n/lazy/browser.<locale>.json`, `addCatalogs`): imported for its effect by every module that shows them.
import { addCatalogs } from '../../core/i18n';

addCatalogs(import.meta.glob('../../core/i18n/lazy/browser.*.json', { eager: true, import: 'default' }));
