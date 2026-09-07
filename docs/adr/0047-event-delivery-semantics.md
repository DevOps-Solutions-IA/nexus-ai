# ADR-0047: Event delivery semantics — at-least-once, not exactly-once

Status: Accepted (NXS-P04).

Context: Nexus AI is an event-driven platform. Future phases (P05 provisioner, P06 conversations, P09 channels, P13 agent runtime, P22 audit) react to business events. A distributed system cannot provide exactly-once *transport* without unbounded coordination; claiming it invites data-integrity bugs when the claim quietly fails.

Decision: Nexus AI guarantees **at-least-once event transport**. Exactly-once transport is **not** claimed. Duplicate-safe business effects come from **idempotent consumption**: every durable consumer records a PostgreSQL receipt keyed by `(consumer_name, event_id)` inside the same transaction as its business effect and only then acknowledges the message. The composed guarantee is:

> at-least-once transport + idempotent consumption = **effectively-once business effect**, where proven by a receipt.

JetStream publisher deduplication (`Nats-Msg-Id = event_id`, bounded `duplicate_window`) reduces duplicates on the wire but is a window, not a guarantee — consumer-side idempotency remains mandatory and is enforced by the framework, not left to each handler.

Consequences: handlers built on the P04 `DurableConsumer` are duplicate-safe by construction. A handler that bypasses the framework and performs a non-idempotent side effect is a defect. Ordering is not guaranteed across subjects; `causation_id`/`correlation_id` carry causal chains. The dedup window is a tuning parameter (`NXS_EVENTS__DEDUPE_WINDOW_SECONDS`), documented as a window with limitations.
