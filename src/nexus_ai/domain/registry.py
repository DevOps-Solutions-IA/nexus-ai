"""Import every ORM model so ``Base.metadata`` is complete.

Alembic autogeneration and the tenant schema guard both rely on this module having
imported all domain models. Add one import per new model module.
"""

from __future__ import annotations

from nexus_ai.domain.auth.models import (
    MembershipRecord,
    PermissionRecord,
    RefreshSessionRecord,
    RoleAssignmentRecord,
    RolePermissionRecord,
    RoleRecord,
    UserCredentialRecord,
    UserRecord,
)
from nexus_ai.domain.customers.models import (
    ConversationActivityRecord,
    ConversationParticipantRecord,
    ConversationRecord,
    CustomerIdentityRecord,
    CustomerRecord,
)
from nexus_ai.domain.events.models import (
    ConsumerReceiptRecord,
    EventDeadLetterRecord,
    EventOutboxRecord,
)
from nexus_ai.domain.integrations.models import (
    IntegrationExecutionRecord,
    IntegrationIdempotencyRecord,
    IntegrationOperationRecord,
    IntegrationRecord,
    IntegrationSecretRecord,
    WebhookEndpointRecord,
    WebhookReceiptRecord,
)
from nexus_ai.domain.messaging.models import (
    MessagingAccountRecord,
    MessagingInboundReceiptRecord,
    MessagingMessageRecord,
    MessagingSecretRecord,
    MessagingSendIdempotencyRecord,
)
from nexus_ai.domain.organizations.models import OrganizationRecord
from nexus_ai.domain.otp.models import OtpChallengeRecord
from nexus_ai.domain.provisioning.models import (
    DashboardConfigurationRecord,
    OrganizationSettingsRecord,
    PlatformGrantRecord,
    ProvisioningRequestRecord,
)
from nexus_ai.domain.telephony.models import (
    TelephonyAccountRecord,
    TelephonyCallEventRecord,
    TelephonyCallRecord,
    TelephonyMediaSessionRecord,
    TelephonyPhoneNumberRecord,
    TelephonySecretRecord,
)
from nexus_ai.domain.tools.models import (
    ToolDefinitionRecord,
    ToolExecutionRecord,
    ToolIdempotencyRecord,
)
from nexus_ai.domain.voice.models import (
    VoiceProfileRecord,
    VoiceProviderAccountRecord,
    VoiceProviderEventRecord,
    VoiceSecretRecord,
    VoiceSessionRecord,
    VoiceUsageRecordRecord,
)

__all__ = [
    "ConsumerReceiptRecord",
    "ConversationActivityRecord",
    "ConversationParticipantRecord",
    "ConversationRecord",
    "CustomerIdentityRecord",
    "CustomerRecord",
    "DashboardConfigurationRecord",
    "EventDeadLetterRecord",
    "EventOutboxRecord",
    "IntegrationExecutionRecord",
    "IntegrationIdempotencyRecord",
    "IntegrationOperationRecord",
    "IntegrationRecord",
    "IntegrationSecretRecord",
    "MembershipRecord",
    "MessagingAccountRecord",
    "MessagingInboundReceiptRecord",
    "MessagingMessageRecord",
    "MessagingSecretRecord",
    "MessagingSendIdempotencyRecord",
    "OrganizationRecord",
    "OrganizationSettingsRecord",
    "OtpChallengeRecord",
    "PermissionRecord",
    "PlatformGrantRecord",
    "ProvisioningRequestRecord",
    "RefreshSessionRecord",
    "RoleAssignmentRecord",
    "RolePermissionRecord",
    "RoleRecord",
    "TelephonyAccountRecord",
    "TelephonyCallEventRecord",
    "TelephonyCallRecord",
    "TelephonyMediaSessionRecord",
    "TelephonyPhoneNumberRecord",
    "TelephonySecretRecord",
    "ToolDefinitionRecord",
    "ToolExecutionRecord",
    "ToolIdempotencyRecord",
    "UserCredentialRecord",
    "UserRecord",
    "VoiceProfileRecord",
    "VoiceProviderAccountRecord",
    "VoiceProviderEventRecord",
    "VoiceSecretRecord",
    "VoiceSessionRecord",
    "VoiceUsageRecordRecord",
    "WebhookEndpointRecord",
    "WebhookReceiptRecord",
]
