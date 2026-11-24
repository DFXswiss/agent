-- A `CK` epoch a former agent of its owner wrapped to the device (ADR-082 section 9.5, KOR-57): kept to open the
-- records sealed under it, never the epoch the device seals a new record under (`credentials.cache.newest_epoch`).
ALTER TABLE runner.scope_key DROP CONSTRAINT scope_key_source_check;
ALTER TABLE runner.scope_key ADD CONSTRAINT scope_key_source_check
    CHECK (source IN ('package', 'wrap', 'former_agent'));
