"""Enterprise messaging subsystem (NXS-P09: NXS-WA-001, NXS-EMAIL-001, NXS-SMS-001).

Provider-neutral inbound + outbound messaging for WhatsApp, Email and SMS on the
NXS-P06 conversation model and the NXS-P07 credential / governed-transport boundary.
The Customer / Identity / Conversation / timeline model is owned by NXS-P06 and is
never duplicated here — messaging owns channel accounts, provider abstractions,
inbound normalization, outbound delivery, message records, delivery lifecycle,
webhook verification, deduplication and channel events.
"""
