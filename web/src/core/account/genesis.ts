// The private organization of a platform invitation (04-crypto.md section 5.3, 06-web.md section 3.1.1, ADR-026):
// org `O`, default workgroup `W`, `OK_1` and `WK_1` with the founder's self-wraps (built by `buildBatch` from the
// single `genesis{private:true}` entry), and the profile events `org.profile` ("Private"/"Privat"), `wg.profile`
// ("Default"/"Standard") and `member.profile` (the display name), all sealed with `OK_1` at `lh` 0 and sent in one
// `POST /v1/orgs` with `platform_invite_id`. The user's `CK_1` exists since the registration and is not touched.
// The request is idempotent: a retry sends the identical body (same IDs) and the hub answers `replayed`.
// `prepareOrg` builds the same for a non-private organization created from the org switcher (WEB-06).
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { fail } from '../crypto/errors';
import { newHeader, sealEvent } from '../crypto/event';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import type { Locale } from '../i18n/locale';
import { epochKey } from '../log/state';
import { buildBatch, createOrgBody } from '../log/rotation';
import type { VerifiedLog } from '../log/verify';
import { isRecord } from '../registry';

/** Display names are 1–80 characters (code points) after trimming (06-web.md section 3.1.1). */
export const DISPLAY_NAME_MAX = 80;

/** The trimmed display name, or null if it is empty or longer than `DISPLAY_NAME_MAX` code points. */
export function displayName(input: string): string | null {
  const name = input.trim();
  const length = [...name].length;
  return length >= 1 && length <= DISPLAY_NAME_MAX ? name : null;
}

/** The names of the private organization and its default workgroup per locale. */
export const PRIVATE_NAMES: Readonly<Record<Locale, { readonly org: string; readonly wg: string }>> = {
  en: { org: 'Private', wg: 'Default' },
  de: { org: 'Privat', wg: 'Standard' },
};

/** A prepared `POST /v1/orgs`: the body and what the client keeps after it was accepted. */
export interface PreparedOrg {
  readonly org: string;
  readonly defaultWg: string;
  readonly body: Readonly<Record<string, unknown>>;
  /** The log after genesis (verified while building). */
  readonly log: VerifiedLog;
  /** `epochKey(scope, scope_id, epoch)` → `OK_1`, `WK_1`. */
  readonly keys: ReadonlyMap<string, Secret>;
}

export interface PrivateOrgInput {
  readonly userId: string;
  readonly identity: Identity;
  readonly displayName: string;
  readonly locale: Locale;
  readonly platformInviteId: string;
  readonly now?: () => number;
}

/**
 * A further, non-private organization (06-web.md section 7.5, ADR-026): any user may create one; it has no platform
 * access until the operator grants it. Same genesis batch and profile events, `private: false`, the org name chosen by
 * the user, the default workgroup named per locale, no platform invitation.
 */
export interface OrgInput {
  readonly userId: string;
  readonly identity: Identity;
  readonly displayName: string;
  readonly orgName: string;
  readonly locale: Locale;
  readonly now?: () => number;
}

/** Builds genesis, self-wraps and the three profile events of a new private organization. */
export function preparePrivateOrg(input: PrivateOrgInput): Promise<PreparedOrg> {
  const { platformInviteId, ...rest } = input;
  return prepareGenesis({ ...rest, orgName: PRIVATE_NAMES[input.locale].org }, true, platformInviteId);
}

/** Builds genesis, self-wraps and the three profile events of a new non-private organization. */
export function prepareOrg(input: OrgInput): Promise<PreparedOrg> {
  return prepareGenesis(input, false);
}

async function prepareGenesis(input: OrgInput, isPrivate: boolean, platformInviteId?: string): Promise<PreparedOrg> {
  const name = displayName(input.displayName) ?? fail('header_invalid');
  const orgName = displayName(input.orgName) ?? fail('header_invalid');
  const now = input.now ?? Date.now;
  const org = uuid7(now());
  const wg = uuid7(now());
  const ts = formatTime(now());
  const author = { id: input.userId, identity: input.identity };
  const built = await buildBatch({
    log: null,
    org,
    author,
    ts,
    entries: [
      { type: 'genesis', subject: input.userId, pub: input.identity.public(), default_wg: wg, private: isPrivate },
    ],
  });
  const keys = new Map(built.newKeys.map((key) => [epochKey(key.scope, key.scope_id, key.epoch), key.key]));
  const orgKey = keys.get(epochKey('org', org, 1))!;
  const names = PRIVATE_NAMES[input.locale];
  const profile = async (kind: string, body: unknown, extra: { wg?: string; ref?: string }) => {
    const header = newHeader({
      id: uuid7(now()),
      org,
      scope: 'org',
      scope_id: org,
      epoch: 1,
      author: input.userId,
      kind,
      ts,
      lh: 0,
      ...extra,
    });
    return sealEvent(orgKey, header, bodyJson(body), input.identity.sig.privateKey);
  };
  const events = [
    await profile('org.profile', { name: orgName }, {}),
    await profile('wg.profile', { name: names.wg }, { wg }),
    await profile('member.profile', { display_name: name }, { ref: input.userId }),
  ];
  const body = { ...createOrgBody(built, platformInviteId), events };
  return { org, defaultWg: wg, body, log: built.log, keys };
}

/** `POST /v1/orgs`; the answer must name the prepared org and default workgroup. */
export async function createOrg(
  request: (method: string, target: string, options: { body: unknown }) => Promise<unknown>,
  prepared: PreparedOrg,
): Promise<void> {
  const answer = await request('POST', '/v1/orgs', { body: prepared.body });
  if (!isRecord(answer) || answer.org_id !== prepared.org || answer.default_wg !== prepared.defaultWg) {
    fail('header_invalid');
  }
}
