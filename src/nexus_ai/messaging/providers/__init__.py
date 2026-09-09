"""Provider-neutral channel adapters (NXS-P09).

A :class:`~nexus_ai.messaging.providers.base.MessagingProvider` normalizes a specific
provider's transport and payloads into the shared messaging domain objects. Provider
payloads never become the canonical contract; a raw provider URL is never fetched
through unsafe code; a provider-supplied MIME / filename / organization id is never
trusted.
"""
