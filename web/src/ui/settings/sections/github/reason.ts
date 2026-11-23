// The text of a failure code of a device request flow: the failure codes of device responses
// (`contracts/requests/objects.json`) and `request_failed` have their own messages (`requests.failure.<code>`); every
// other code (hub, crypto) comes from the error catalog. Shared with the Claude login and credential checks of WEB-10.
import { FAILURE_CODES, REQUEST_FAILED } from '../../../../core/requests/bodies';
import { useI18n } from '../../../i18n';

const OWN: readonly string[] = [...FAILURE_CODES, REQUEST_FAILED];

export function useReason(): (code: string) => string {
  const { t, error } = useI18n();
  return (code) => (OWN.includes(code) ? t(`requests.failure.${code}`) : error(code).title);
}
