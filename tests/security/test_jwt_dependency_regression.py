"""Dependency security regressions; Nexus retains its EdDSA-only TokenService."""

import datetime as dt
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus_ai.core.errors import TokenValidationError
from nexus_ai.domain.auth.keys import LocalEd25519KeyProvider
from nexus_ai.domain.auth.tokens import TokenService


@pytest.mark.parametrize("decode", [jwt.decode, jwt.decode_complete])
def test_unsigned_inspection_cannot_mutate_options_or_disable_later_validation(decode):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    expired = jwt.encode(
        {"sub": "local-subject", "exp": dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)},
        private,
        algorithm="RS256",
    )
    options = {"verify_signature": False}
    decode(expired, options=options)
    assert options == {"verify_signature": False}
    options["verify_signature"] = True
    with pytest.raises(jwt.ExpiredSignatureError):
        decode(expired, private.public_key(), algorithms=["RS256"], options=options)
    assert options == {"verify_signature": True}


def test_malformed_rsa_jwk_does_not_suppress_valid_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    valid = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    valid["kid"] = "valid-local-test-key"
    malformed = {**valid, "kid": "malformed-local-test-key", "d": "AAAAAA"}
    parsed = jwt.PyJWKSet.from_dict({"keys": [malformed, valid]})
    assert [key.key_id for key in parsed.keys] == ["valid-local-test-key"]
    encoded = jwt.encode({"sub": "local-subject"}, private, algorithm="RS256")
    assert jwt.decode(encoded, parsed.keys[0].key, algorithms=["RS256"])["sub"] == ("local-subject")


def test_unsigned_token_with_known_kid_is_rejected():
    service = TokenService(
        LocalEd25519KeyProvider(Ed25519PrivateKey.generate()),
        issuer="nexus-ai",
        audience="nexus-ai-backend",
        access_token_ttl_seconds=900,
        clock_skew_seconds=30,
    )
    token = service.issue_access_token(
        subject=uuid.uuid7(),
        organization_id=uuid.uuid7(),
        session_id=uuid.uuid7(),
        now=dt.datetime.now(dt.UTC),
    )
    header = jwt.get_unverified_header(token)
    payload = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(payload, key=None, algorithm="none", headers=header | {"alg": "none"})
    with pytest.raises(TokenValidationError):
        service.verify_access_token(forged)
    assert service.verify_access_token(token).subject == uuid.UUID(payload["sub"])
