"""Guest control link of the runner (docs/platform/spec/03-api.md section 7): `client.GuestClient` (HTTP/1.1 over the
forwarded Unix socket with the bearer token), `reader.JournalReader` (long-poll journal reader), `cursor` (journal
cursor per VM in the store) and `errors` (closed error classes)."""
