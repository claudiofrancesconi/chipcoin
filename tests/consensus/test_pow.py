from dataclasses import replace

from chipcoin.consensus.models import BlockHeader
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.consensus.pow import (
    bits_to_target,
    calculate_next_work_required,
    header_work,
    target_to_bits,
    verify_proof_of_work,
)


def test_compact_target_roundtrip_preserves_target() -> None:
    original_bits = 0x1D00FFFF
    target = bits_to_target(original_bits)

    assert target_to_bits(target) == original_bits


def test_header_work_is_positive() -> None:
    header = BlockHeader(
        version=1,
        previous_block_hash="00" * 32,
        merkle_root="11" * 32,
        timestamp=1,
        bits=0x207FFFFF,
        nonce=0,
    )

    assert header_work(header) > 0


def test_verify_proof_of_work_accepts_easy_header() -> None:
    merkle = "22" * 32
    previous = "00" * 32

    for nonce in range(1_000_000):
        header = BlockHeader(
            version=1,
            previous_block_hash=previous,
            merkle_root=merkle,
            timestamp=1_700_000_000,
            bits=0x207FFFFF,
            nonce=nonce,
        )
        if verify_proof_of_work(header):
            assert True
            return

    raise AssertionError("Expected to find a valid nonce for an easy target.")


def test_calculate_next_work_required_increases_difficulty_when_blocks_are_fast() -> None:
    faster_bits = calculate_next_work_required(
        previous_bits=MAINNET_PARAMS.genesis_bits,
        actual_timespan_seconds=(MAINNET_PARAMS.target_block_time_seconds * MAINNET_PARAMS.difficulty_adjustment_window) // 2,
        params=MAINNET_PARAMS,
    )

    assert bits_to_target(faster_bits) < bits_to_target(MAINNET_PARAMS.genesis_bits)


def test_calculate_next_work_required_decreases_difficulty_when_blocks_are_slow() -> None:
    slower_bits = calculate_next_work_required(
        previous_bits=MAINNET_PARAMS.genesis_bits,
        actual_timespan_seconds=(MAINNET_PARAMS.target_block_time_seconds * MAINNET_PARAMS.difficulty_adjustment_window) * 2,
        params=MAINNET_PARAMS,
    )

    assert bits_to_target(slower_bits) >= bits_to_target(MAINNET_PARAMS.genesis_bits)
