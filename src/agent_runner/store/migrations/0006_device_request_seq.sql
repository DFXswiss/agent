-- Device requests (RUN-09, decisions/RUN-09.md): the last response number sent for a request, stored before the
-- response is sent, so responses keep `seq` 1..n without a gap or a reused number across restarts
-- (04-crypto.md section 4.5, 05-runner.md section 11.1).
ALTER TABLE runner.device_request
    ADD COLUMN last_seq integer NOT NULL DEFAULT 0 CHECK (last_seq >= 0);
