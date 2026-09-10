"""NXS-P12 — ElevenLabs Voice.

The permanent, provider-neutral real-time voice layer. It connects an ACTIVE NXS-P11
media session to a real-time voice provider (ElevenLabs) behind Nexus-owned contracts:

    PSTN / SIP -> Asterisk 22 LTS / ARI -> NXS-P11 Telephony Foundation
        -> Call + ACTIVE MediaSession -> NXS-P12 Voice Gateway -> ElevenLabs
        -> (future) NXS-P13 AI Agent Runtime

P12 owns VOICE PROVIDER INTEGRATION only. NXS-P11 stays authoritative for call state,
call ownership, phone numbers, SIP routing, hangup, DTMF and Asterisk call control. P12
does NOT own autonomous reasoning, tool execution, workflows or business logic — those
belong to NXS-P13 and later.
"""
