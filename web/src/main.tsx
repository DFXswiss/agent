// Bootstrap (06-web.md section 2.2): configuration check against the hub (section 10.4), then the router.
import './index.css';
import { boot } from './ui/app/boot';

function localStorageOrNull(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

void boot({
  container: document.getElementById('root')!,
  fetch: window.fetch.bind(window),
  rootKey: import.meta.env.VITE_PLATFORM_ROOT_KEY,
  origin: window.location.origin,
  languages: navigator.languages,
  credentials: navigator.credentials,
  indexedDB: window.indexedDB,
  storage: localStorageOrNull(),
});
