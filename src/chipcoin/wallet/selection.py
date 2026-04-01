"""Wallet-side input selection strategies."""

from __future__ import annotations

from .models import SelectionResult, SpendCandidate


def select_inputs(candidates: list[SpendCandidate], target_value: int) -> SelectionResult:
    """Select spendable inputs for a target value."""

    if target_value <= 0:
        raise ValueError("Target value must be positive.")
    ordered = sorted(candidates, key=lambda candidate: (candidate.amount_chipbits, candidate.txid, candidate.index))
    selected: list[SpendCandidate] = []
    total = 0
    for candidate in ordered:
        selected.append(candidate)
        total += candidate.amount_chipbits
        if total >= target_value:
            return SelectionResult(
                selected=tuple(selected),
                total_input_chipbits=total,
                change_chipbits=total - target_value,
            )
    raise ValueError("Insufficient spendable balance for the requested amount and fee.")
