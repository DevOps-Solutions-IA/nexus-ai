"""Regression for the dependency advisory found during P22 certification."""

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def test_malformed_rsa_jwk_does_not_suppress_valid_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    valid = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    valid["kid"] = "valid-local-test-key"
    malformed = {**valid, "kid": "malformed-local-test-key", "d": "AAAAAA"}
    parsed = jwt.PyJWKSet.from_dict({"keys": [malformed, valid]})
    assert [key.key_id for key in parsed.keys] == ["valid-local-test-key"]
    encoded = jwt.encode({"sub": "local-test-subject"}, private, algorithm="RS256")
    assert jwt.decode(encoded, parsed.keys[0].key, algorithms=["RS256"])["sub"] == (
        "local-test-subject"
    )
