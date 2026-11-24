"""Live channels of the hub (03-api.md sections 5 and 6.2, 00-overview.md section 5.1).

- `publish` - the only way areas publish live frames and device hints (`publish(audience, frame, data)`,
  `hint(device_id, type, body)`);
- `sse` - `GET /v1/stream` for browsers (signed SSE read with `fetch`, cursor replay, resync, rights filter);
- `ws` - `GET /v1/device/ws` for devices (signed upgrade, content-free hints, heartbeats);
- `presence` - the online state of devices and the signals `device.presence` / `device.capacity`;
- `bus` - `LISTEN`/`NOTIFY` between hub processes;
- `service` - one live service per running app; `routes`, `jobs` - the discovered extension points.

The functions are imported from their modules (`from agent_hub.stream.publish import publish`): the package does not
re-export `publish`, because the module of that name is part of the interface since HUB-02.
"""
