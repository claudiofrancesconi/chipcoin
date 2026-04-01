from chipcoin.consensus.hashes import double_sha256, double_sha256_hex
from chipcoin.consensus.merkle import merkle_root


def test_double_sha256_hex_matches_binary_digest() -> None:
    payload = b"chipcoin"

    assert double_sha256(payload).hex() == double_sha256_hex(payload)


def test_merkle_root_duplicates_last_hash_for_odd_leaf_count() -> None:
    leaves = ["11" * 32, "22" * 32, "33" * 32]

    root_with_odd_count = merkle_root(leaves)
    root_with_explicit_duplicate = merkle_root(leaves + ["33" * 32])

    assert root_with_odd_count == root_with_explicit_duplicate
