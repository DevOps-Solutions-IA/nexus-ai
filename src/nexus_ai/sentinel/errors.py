"""Safe fixed Sentinel error classifications; no payloads or credentials."""


class SentinelDenied(Exception):
    """Authority, policy or identity could not be established."""


class SentinelConflict(Exception):
    """Expected durable revision or idempotency semantics did not match."""
