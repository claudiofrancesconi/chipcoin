from chipcoin.consensus.hashes import double_sha256_hex


def test_double_sha256_hex_has_expected_length() -> None:
    assert len(double_sha256_hex(b"chipcoin")) == 64
