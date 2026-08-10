from app.core.security import hash_password, verify_password


def test_password_hash_uses_argon2_and_verifies() -> None:
    encoded = hash_password("A-safe-test-password-2026!")
    assert encoded.startswith("$argon2")
    assert verify_password("A-safe-test-password-2026!", encoded)
    assert not verify_password("wrong-password", encoded)
