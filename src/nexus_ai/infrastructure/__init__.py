"""Connectivity foundations for PostgreSQL, Valkey and NATS/JetStream.

Each adapter is constructed without side effects; connections open only in the
application lifespan and close idempotently on shutdown.
"""
