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

- When a passkey is **added** (or created at registration or in a recovery), a passkey without PRF is refused at once
  with the screen "This passkey cannot open your data" / "Dieser Passkey kann Ihre Daten nicht öffnen", and it is not
  kept. The screen always names the passkeys that work and says that Bitwarden currently cannot – whichever provider
  was used.
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
- If the product ever offers **"Remember this browser" / "Diesen Browser merken"** during this check, do **not** choose
  it: it would keep your data's key on the test device.

## Steps (one row)

| # | Step | Expected | Record |
|---|---|---|---|
| 1 | On the device of the row, open the platform in the browser of the row and sign in (with any passkey that works). | Your organization opens. | browser and its version, system version, device model |
| 2 | Open Settings → "Passkeys and recovery" / "Passkeys und Wiederherstellung". Under "Add passkey" choose **"Add passkey on this device" / "Passkey auf diesem Gerät hinzufügen"** – for a security key or a phone (rows 3, 9, 10) instead **"Add passkey on another device (QR code or security key)" / "Passkey auf einem anderen Gerät hinzufügen (QR-Code oder Sicherheitsschlüssel)"**. When the system asks where to save the passkey, choose the **provider of the row** (security key: insert or tap it; phone: scan the QR code). | The page says the passkey was created and shows **"Confirm with the new passkey" / "Mit dem neuen Passkey bestätigen"**. | – |
| 3 | Press "Confirm with the new passkey" and confirm with the same passkey (Face ID, Touch ID, PIN, tap). | Either "The passkey was added. It signs you in and opens your data." and the list shows the new passkey with "Signs you in and opens your data" → **PRF: yes**; or the screen "This passkey cannot open your data" appears and the passkey is not added → **PRF: no**. | PRF yes/no; provider and its version; the exact wording if it differs |
| 4 | Only if PRF: yes – sign out, then sign in and pick the new passkey. | One gesture signs you in **and** your tasks open with their content; no question for a recovery code. | Login yes/no, number of gestures |
| 5 | Only if PRF: yes – on a **second device** that uses the same provider account (e.g. another Apple device with the same iCloud account, another Chrome with the same Google account), sign in with the same passkey. Skip for security keys and when you have no second device. | As step 4. If it signs in but shows "This passkey cannot open your data on this device", record it – it is a finding – and choose "Sign out" (not "Remember this browser"). | Synced login yes/no/skipped |
| 6 | Clean up: if you do not want to keep the passkey, remove it in Settings → "Passkeys and recovery" (never your last one). If the check gave PRF: no, also delete the passkey the provider kept (the product already removed it from your account). | The list shows the passkeys you had before. | – |

Time: steps 1–3 about 4 minutes, 4–5 about 4 minutes, 6 about 1 minute.

## Rows

The rows of the release table, with the expected outcome from the providers' published behaviour (to be confirmed
or refuted – the expectation is not a result):

| # | Browser / system | Provider | Button in step 2 | Expected |
|---|---|---|---|---|
| 1 | Safari, macOS 15 or later | iCloud Keychain | this device | PRF: yes |
| 2 | Safari, iOS 18 or later | iCloud Keychain | this device | PRF: yes |
| 3 | Firefox, desktop | hardware security key (FIDO2 with `hmac-secret`) | another device | PRF: yes |
| 4 | Chrome, Android | Google Password Manager | this device | PRF: yes |
| 5 | Chrome, desktop (macOS or Windows) | Google Password Manager | this device | PRF: yes |
| 6 | Edge or Chrome, Windows 11 | Windows Hello | this device | unknown – the main open question |
| 7 | Chrome or Safari, desktop | 1Password (browser extension) | this device | unknown |
| 8 | Chrome or Firefox, desktop | Bitwarden (browser extension) | this device | PRF: no (refused) |
| 9 | Chrome or Safari, desktop | hardware security key | another device | PRF: yes |
| 10 | Chrome, desktop, passkey on a phone through the QR code | the phone's provider | another device | unknown (not in the release table yet; add the row when it is checked) |

## Reporting

One line per row to the operator: row number, date, device model, system and browser versions, provider and version,
"PRF yes/no", "login yes/no", "synced login yes/no/skipped", and any wording that differed. Nothing else – no names,
no account, no screenshots with codes. The operator copies it into the release table:

| Release table column | Filled from this procedure |
|---|---|
| PRF | step 3: pass with "PRF: yes", or "no PRF" |
| Login | step 4 (and 5): pass/fail |
| Registration | **not covered** – the procedure adds a passkey to an existing account; the registration uses the same PRF check, so a "PRF: yes" here predicts it, but the column stays open until a registration was done on the device |
| Recovery | **not covered** – a recovery registers a new passkey that must give PRF; it needs a recovery code and is done separately |
| Terminal, Countdown | **not covered** – they depend on the real browser (e.g. Safari on iOS), not on the provider; they stay "open – manual" until checked on that browser, and are never copied from the automated rows |

A row with "PRF: no" for a provider expected to give PRF, or a synced passkey that does not open the data, is a
finding: write it in the findings section of the release file and tell the operator.
