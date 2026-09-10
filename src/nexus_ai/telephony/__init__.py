"""Telephony Foundation (NXS-P11: NXS-TEL-001).

The permanent, provider-neutral call-control and SIP/telephony domain that future phases
consume:

    PSTN / SIP provider -> SIP / telephony edge -> NXS Telephony Foundation
    -> Call / Media session -> (NXS-P12 ElevenLabs voice) -> (NXS-P13 AI Agent Runtime)

P11 owns the foundation ONLY: the call state machine, the provider adapter contract, the
Asterisk integration boundary (ARI), inbound + outbound call control, DTMF, and the
media-session domain P12 will attach external voice to. It ships NO voice agent, NO AI
reasoning, NO campaigns / workflows / scheduler, and NO call recording.
"""

from __future__ import annotations
