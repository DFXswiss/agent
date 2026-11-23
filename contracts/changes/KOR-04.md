# KOR-04 – contract changes

## `cred_holders` in `GET /v1/me` (additive)

AUD-01 A-07: a browser only noticed that a device lost eligibility for the user's credential key `CK` from the
membership logs it holds; after the user was removed from an org whose log no browser of the user held, that org's
devices kept their wraps of the current `CK` and no rotation happened. `api/me.response.json` gets the optional object
`cred_holders` `{epoch, devices}`: the user's current `cred` epoch (newest `scope_epoch`, null before the first) and
the IDs of the devices holding a wrap of it (the user's own self-wrap left out). Only IDs the hub already stores as
wrap recipients, no key material. The hub always sends it; it is optional so that existing producers and fixtures stay
valid. Fixtures `fixtures/api/me.response/valid/cred-holders.json`, `valid/cred-holders-no-epoch.json`,
`invalid/cred-holders-bad-device.json`, `invalid/cred-holders-missing-epoch.json`.
