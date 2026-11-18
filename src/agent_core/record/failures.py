"""The failure values of a verification (RECORD-PROTOCOL.md section 12.1) that are no error value.

Each value is defined once. A failure value that is also an error value of an append (such as
`event_invalid`, `chain_broken` or `event_id_taken`) is defined with that error value and imported
from there. This module imports nothing, so that every other module of the record can import it.
"""

from __future__ import annotations

FILE_INVALID = "file_invalid"
POSITION_GAP = "position_gap"
ROW_MISMATCH = "row_mismatch"
RECORD_HASH_MISMATCH = "record_hash_mismatch"
EVENT_REFUSED = "event_refused"
CHECKPOINT_INVALID = "checkpoint_invalid"
