# Next Upgrade

## 1. Freeze A Transport-Agnostic Message Envelope

Before changing the relay transport, define one JSON message shape that works for both file relay and socket relay.

Suggested fields:

- `id`: globally unique message id
- `sender`: agent or human name
- `seq`: per-sender monotonic sequence number
- `type`: `message`, `ack`, `system`, or `presence`
- `body`: message payload
- `ack_for`: optional referenced message id
- `ts`: creation timestamp

Reason:

If the envelope is stable first, ordering, dedupe, ack behavior, and future transport upgrades stay simple.

## 2. Define Delivery Semantics Before Transport

Keep the first behavior model minimal: broadcast messages to all participants, require per-sender monotonic `seq`, and make receivers idempotent so duplicate reads do not break the room.

Rules:

- each sender increments `seq` by 1 for every new outbound message
- receivers track the highest seen `seq` per sender
- duplicate or old messages are ignored safely
- `ack` is explicit and references `ack_for`
- transport does not define ordering across different senders

Reason:

This gives the room predictable behavior on files today and carries cleanly into WebSocket or UDS transport later.
