# PRF check on real devices – procedure for people

The automated suite proves the passkey flows with virtual authenticators only
([0.1.0.md §1](0.1.0.md#1-automated-rows-executed)). Whether a real passkey provider delivers the PRF output that
opens a user's data ([key model](../key-model.md) rule KM-02, [ADR-006](../adr/006-passkey-prf-key-management.md))
can only be seen on the real device. This page is the procedure for one person with one device and one provider:
**at most 10 minutes per row**, no new account, nothing to install, no technical knowledge needed.

It replaces the procedure of [0.1.0.md §3](0.1.0.md#3-procedure-per-manual-row), which was written before
registrations required PRF and dropped the recovery-code step (rules [E-12](../KONZEPT.md#e-12) and
[E-13](../KONZEPT.md#e-13)). Results go into the table of the current release file (today
[0.1.0.md §2](0.1.0.md#2-manual-rows-real-devices-a-person)).

## What the product already tells the user (self-report)

No extra tool is needed: the product shows, without sending anything new anywhere, whether a passkey opens the data.

- When a passkey is **added** (Settings → "Passkeys and recovery" → "Add passkey") or created at registration, a
  passkey without PRF is refused at once with the screen "This passkey cannot open your data" / "Dieser Passkey kann
  Ihre Daten nicht öffnen" (for Bitwarden with an extra line), and it is not kept.
- In the **passkey list** every passkey carries one of two lines: "Signs you in and opens your data" / "Meldet an und
  öffnet Ihre Daten" (PRF delivered) or "Signs you in only – does not open your data on this device" / "Meldet nur an –
  öffnet Ihre Daten auf diesem Gerät nicht" (an older passkey without PRF).

So a check is: add a passkey with the provider of the row and read what the product says. The result names a provider
and a device type, never a person, an account or a code.

## Before you start (once)

- An existing account on the platform that you can sign in to with another passkey (so that nothing is lost if the
  provider of the row fails). Use your own account; no test account and no invitation are needed.
- The device and provider of the row, updated to the current system version.
- Do not photograph or copy recovery codes, invitation links or anything from the address bar.

## Steps (one row)

| # | Step | Expected | Record |
|---|---|---|---|
| 1 | On the device of the row, open the platform in the browser of the row and sign in (with any passkey that works). | Your organization opens. | browser and its version, system version, device model |
| 2 | Settings → "Passkeys and recovery" → "Add passkey". Choose the **provider of the row** when the system asks where to save the passkey (for a security key: insert or tap it). | Either the new passkey appears in the list with "Signs you in and opens your data" → **PRF: yes**; or the screen "This passkey cannot open your data" appears and the passkey is not added → **PRF: no**. | PRF yes/no; provider and its version; the exact wording if it differs |
| 3 | Only if PRF: yes – sign out, then sign in and pick the new passkey. | One gesture (Face ID, Touch ID, PIN, tap) signs you in **and** your tasks open with their content; no question for a recovery code. | Login yes/no, number of gestures |
| 4 | Only if PRF: yes – on a **second device** that uses the same provider account (e.g. another Apple device with the same iCloud account, another Chrome with the same Google account), sign in with the same passkey. Skip for security keys and when you have no second device. | As step 3. A synced passkey that signs in but does not open the data shows "This passkey cannot open your data on this device" → record it, it is a finding. | Synced login yes/no/skipped |
| 5 | Clean up: if you do not want to keep the passkey, remove it in Settings → "Passkeys and recovery" (never your last one), and delete it in the provider if PRF: no left one there. | The list shows the passkeys you had before. | – |

Time: steps 1–2 about 3 minutes, 3–4 about 4 minutes, 5 about 1 minute.

## Rows

The rows of the release table, with the expected outcome from the providers' published behaviour (to be confirmed
or refuted – the expectation is not a result):

| # | Browser / system | Provider | Expected |
|---|---|---|---|
| 1 | Safari, macOS 15 or later | iCloud Keychain | PRF: yes |
| 2 | Safari, iOS 18 or later | iCloud Keychain | PRF: yes |
| 3 | Firefox, desktop | hardware security key (FIDO2 with `hmac-secret`) | PRF: yes |
| 4 | Chrome, Android | Google Password Manager | PRF: yes |
| 5 | Chrome, desktop (macOS or Windows) | Google Password Manager | PRF: yes |
| 6 | Edge or Chrome, Windows 11 | Windows Hello | unknown – the main open question |
| 7 | Chrome or Safari, desktop | 1Password (browser extension) | unknown |
| 8 | Chrome or Firefox, desktop | Bitwarden (browser extension) | PRF: no (refused with the Bitwarden line) |
| 9 | Chrome or Safari, desktop | hardware security key | PRF: yes |
| 10 | Chrome, desktop, passkey on a phone through the QR code | the phone's provider | unknown (not in the release table yet; add the row when it is checked) |

## Reporting

One line per row to the operator: row number, date, device model, system and browser versions, provider and version,
"PRF yes/no", "login yes/no", "synced login yes/no/skipped", and any wording that differed. Nothing else – no names,
no account, no screenshots with codes. The operator copies it into the release table (PRF column: pass with "PRF: yes"
or "no PRF"; Registration and Login columns: pass/fail from steps 2 and 3; the Recovery, Terminal and Countdown columns
do not depend on the provider and stay as the automated rows show unless something differs on the device).

A row with "PRF: no" for a provider expected to give PRF, or a synced passkey that does not open the data, is a
finding: write it in the findings section of the release file and tell the operator.
