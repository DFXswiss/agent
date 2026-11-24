# HOT-11 – decisions

Package: urgent fix of the hermetic scenario E2E-20, which was red after KOR-25 and KOR-40, and of its artefact finding
"secret in artefacts: github token".

## Finding

The GitHub token in the artefacts came from the GitHub double, not from the product. On disconnect, the device revokes
the token it minted through the device flow (KOR-40). It uses GitHub's unauthenticated `POST /credentials/revoke
{"credentials": [token]}`, so the token has to be in the request body. The double journaled every request body
unchanged and redacted only the headers. As a result, `github-journal.jsonl`, an artefact of the run, held the `ghu_`
token. The runner logs only the outcome of the revocation.

## Decisions

1. **The double redacts the credential fields of GitHub's API.** The fields in `CREDENTIAL_FIELDS` (`credentials`,
   `access_token`, `refresh_token`, `client_secret`) are journaled as `<redacted N chars>`, like the credential
   headers. The double does not mask credential shapes in general: a leak by a session must stay visible to the
   artefact scan.
2. **E2E-20 test 1 follows KOR-40 and ADR-066.** A device-flow token is revocable, so after disconnecting the page
   shows the "revoked" note and not the link to GitHub's settings. A later operation of a session is held for a new
   connection: the waiting card appears and GitHub gets no call. The operation is not rejected at once. Its rejection
   after the 15-minute hold is covered by the runner tests.
3. **E2E-20 test 4 waits for three holds.** Each held request is answered `pending` after 20 s (ADR-066), so the
   session's answer comes after about 62 s. That is more than the one minute `expectAnswerInChat` allows. The test now
   waits up to 180 s. The product behaviour is correct.
