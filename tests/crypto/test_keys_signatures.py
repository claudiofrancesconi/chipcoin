from chipcoin.consensus.hashes import double_sha256
from chipcoin.crypto.addresses import address_to_public_key_hash, is_valid_address, public_key_hash, public_key_to_address
from chipcoin.crypto.keys import (
    derive_public_key,
    generate_private_key,
    parse_private_key_hex,
    serialize_private_key_hex,
)
from chipcoin.crypto.signatures import sign_digest, verify_digest


def test_generate_private_key_and_roundtrip_hex() -> None:
    private_key = generate_private_key()

    assert len(private_key) == 32
    assert parse_private_key_hex(serialize_private_key_hex(private_key)) == private_key


def test_sign_and_verify_digest_with_secp256k1() -> None:
    private_key = parse_private_key_hex("0000000000000000000000000000000000000000000000000000000000000001")
    public_key = derive_public_key(private_key)
    digest = double_sha256(b"chipcoin-signed-message")
    signature = sign_digest(private_key, digest)

    assert verify_digest(public_key, digest, signature) is True
    assert verify_digest(public_key, double_sha256(b"tampered"), signature) is False


def test_address_derivation_has_checksum_and_embedded_pubkey_hash() -> None:
    private_key = parse_private_key_hex("0000000000000000000000000000000000000000000000000000000000000002")
    public_key = derive_public_key(private_key)
    address = public_key_to_address(public_key)

    assert address.startswith("CHC")
    assert is_valid_address(address) is True
    assert address_to_public_key_hash(address) == public_key_hash(public_key)
