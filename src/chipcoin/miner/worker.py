"""Template-based Chipcoin mining worker."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..consensus.merkle import merkle_root
from ..consensus.models import Block, BlockHeader, Transaction, TxOutput
from ..consensus.pow import verify_proof_of_work
from ..consensus.serialization import deserialize_transaction, serialize_block
from .config import MinerWorkerConfig
from .template_client import MiningApiError, MiningNodeClient


@dataclass(frozen=True)
class ActiveTemplate:
    """One currently mined template fetched from one node."""

    node_url: str
    payload: dict[str, Any]
    fetched_at: float
    next_status_check_at: float
    extra_nonce: int = 0
    nonce_cursor: int = 0


@dataclass(frozen=True)
class TemplateRefreshDecision:
    """Describe why one active template must be replaced."""

    reason: str
    details: dict[str, object]


class MinerWorker:
    """A lightweight worker that mines only from node-provided block templates."""

    def __init__(
        self,
        config: MinerWorkerConfig,
        *,
        logger: logging.Logger | None = None,
        time_module=time,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("chipcoin.miner.worker")
        self.time = time_module
        self.clients = [
            MiningNodeClient(base_url=node_url, timeout_seconds=config.request_timeout_seconds)
            for node_url in config.node_urls
        ]
        if not self.clients:
            raise ValueError("At least one mining node URL is required.")
        self._active_client_index = 0
        self.accepted_blocks = 0
        self.rejected_blocks = 0

    def run(self) -> dict[str, object]:
        """Start mining until stopped or until the optional run budget expires."""

        deadline = None if self.config.run_seconds is None else self.time.monotonic() + self.config.run_seconds
        active_template: ActiveTemplate | None = None
        while True:
            if deadline is not None and self.time.monotonic() >= deadline:
                break
            if active_template is None:
                try:
                    active_template = self._acquire_template()
                except MiningApiError as exc:
                    self.logger.warning("all mining nodes unavailable error=%s", exc)
                    continue
                continue
            solved_block, active_template = self._mine_batch(active_template)
            if solved_block is None:
                refresh_decision = self._template_refresh_decision(active_template)
                if refresh_decision is not None:
                    self._log_template_refresh(active_template, refresh_decision)
                    active_template = None
                continue
            result = self._submit_block(active_template, solved_block)
            if bool(result.get("accepted")):
                self.accepted_blocks += 1
                self.logger.info(
                    "block accepted height=%s hash=%s node=%s",
                    active_template.payload["height"],
                    result.get("block_hash"),
                    active_template.node_url,
                )
                if self.config.mining_min_interval_seconds > 0:
                    self.time.sleep(self.config.mining_min_interval_seconds)
            else:
                self.rejected_blocks += 1
                self.logger.info(
                    "block rejected reason=%s template_id=%s node=%s",
                    result.get("reason"),
                    active_template.payload["template_id"],
                    active_template.node_url,
                )
            active_template = None
        return {
            "mining": True,
            "accepted_blocks": self.accepted_blocks,
            "rejected_blocks": self.rejected_blocks,
            "miner_id": self.config.miner_id,
            "node_urls": list(self.config.node_urls),
        }

    def _acquire_template(self) -> ActiveTemplate:
        """Fetch one fresh mining template from the first healthy node."""

        last_error: Exception | None = None
        for offset in range(len(self.clients)):
            index = (self._active_client_index + offset) % len(self.clients)
            client = self.clients[index]
            try:
                status = client.status()
                template = client.get_block_template(
                    payout_address=self.config.payout_address,
                    miner_id=self.config.miner_id,
                    template_mode="full_block",
                )
            except MiningApiError as exc:
                last_error = exc
                self.logger.warning("mining node unavailable node=%s error=%s", client.base_url, exc)
                continue
            self._active_client_index = index
            self.logger.info(
                "mining template acquired node=%s tip=%s height=%s template_id=%s",
                client.base_url,
                status.get("best_tip_hash"),
                template.get("height"),
                template.get("template_id"),
            )
            return ActiveTemplate(
                node_url=client.base_url,
                payload=template,
                fetched_at=self.time.monotonic(),
                next_status_check_at=self.time.monotonic() + self.config.polling_interval_seconds,
            )
        self.time.sleep(self.config.polling_interval_seconds)
        if last_error is None:
            raise RuntimeError("No mining nodes configured.")
        raise last_error

    def _mine_batch(self, active_template: ActiveTemplate) -> tuple[Block | None, ActiveTemplate]:
        """Search one nonce batch for a valid PoW solution."""

        template_payload = active_template.payload
        coinbase = self._build_coinbase(template_payload, active_template.extra_nonce)
        transactions = (coinbase, *self._decode_template_transactions(template_payload))
        header_base = BlockHeader(
            version=int(template_payload["version"]),
            previous_block_hash=str(template_payload["previous_block_hash"]),
            merkle_root=merkle_root([transaction.txid() for transaction in transactions]),
            timestamp=max(int(template_payload["curtime"]), int(template_payload["mintime"])),
            bits=int(template_payload["bits"]),
            nonce=0,
        )
        start_nonce = active_template.nonce_cursor
        end_nonce = start_nonce + self.config.nonce_batch_size
        for nonce in range(start_nonce, end_nonce):
            header = BlockHeader(
                version=header_base.version,
                previous_block_hash=header_base.previous_block_hash,
                merkle_root=header_base.merkle_root,
                timestamp=header_base.timestamp,
                bits=header_base.bits,
                nonce=nonce,
            )
            if verify_proof_of_work(header):
                return Block(header=header, transactions=transactions), active_template
        return None, ActiveTemplate(
            node_url=active_template.node_url,
            payload=active_template.payload,
            fetched_at=active_template.fetched_at,
            next_status_check_at=active_template.next_status_check_at,
            extra_nonce=active_template.extra_nonce + 1,
            nonce_cursor=0,
        )

    def _submit_block(self, active_template: ActiveTemplate, block: Block) -> dict[str, object]:
        """Submit one solved block back to the active node."""

        client = self.clients[self._active_client_index]
        return client.submit_block(
            template_id=str(active_template.payload["template_id"]),
            serialized_block=serialize_block(block).hex(),
            miner_id=self.config.miner_id,
        )

    def _template_refresh_decision(self, active_template: ActiveTemplate) -> TemplateRefreshDecision | None:
        """Return why the current template should be replaced, if needed."""

        now = self.time.time()
        expiry_at = int(active_template.payload["template_expiry"])
        if now >= expiry_at - self.config.template_refresh_skew_seconds:
            return TemplateRefreshDecision(
                reason="expired",
                details={"template_expiry": expiry_at, "now": int(now)},
            )
        if self.time.monotonic() < active_template.next_status_check_at:
            return None
        client = self.clients[self._active_client_index]
        try:
            status = client.status()
        except MiningApiError as exc:
            return TemplateRefreshDecision(
                reason="status_refresh_failed",
                details={"error": str(exc)},
            )
        current_tip_hash = str(status["best_tip_hash"])
        template_previous_hash = str(active_template.payload["previous_block_hash"])
        if current_tip_hash != template_previous_hash:
            return TemplateRefreshDecision(
                reason="tip_changed",
                details={
                    "template_previous_block_hash": template_previous_hash,
                    "current_best_tip_hash": current_tip_hash,
                    "current_best_height": int(status["best_height"]),
                },
            )
        return None

    def _template_is_stale(self, active_template: ActiveTemplate) -> bool:
        """Backwards-compatible boolean helper for tests and callers."""

        return self._template_refresh_decision(active_template) is not None

    def _log_template_refresh(self, active_template: ActiveTemplate, decision: TemplateRefreshDecision) -> None:
        """Emit one precise refresh log line for the active template."""

        template_id = str(active_template.payload["template_id"])
        node_url = active_template.node_url
        if decision.reason == "expired":
            self.logger.info(
                "template expired template_id=%s node=%s template_expiry=%s now=%s",
                template_id,
                node_url,
                decision.details["template_expiry"],
                decision.details["now"],
            )
            return
        if decision.reason == "tip_changed":
            self.logger.info(
                "template stale template_id=%s node=%s reason=tip_changed template_previous_block_hash=%s current_best_tip_hash=%s current_best_height=%s",
                template_id,
                node_url,
                decision.details["template_previous_block_hash"],
                decision.details["current_best_tip_hash"],
                decision.details["current_best_height"],
            )
            return
        if decision.reason == "status_refresh_failed":
            self.logger.warning(
                "template refresh failed template_id=%s node=%s reason=status_refresh_failed error=%s",
                template_id,
                node_url,
                decision.details["error"],
            )
            return
        self.logger.info(
            "template replaced template_id=%s node=%s reason=%s",
            template_id,
            node_url,
            decision.reason,
        )

    def _build_coinbase(self, template_payload: dict[str, Any], extra_nonce: int) -> Transaction:
        """Construct one worker-side coinbase for the active template."""

        return Transaction(
            version=1,
            inputs=(),
            outputs=(
                TxOutput(
                    value=int(template_payload["coinbase_value_chipbits"]),
                    recipient=str(template_payload["payout_address"]),
                ),
                *tuple(
                    TxOutput(
                        value=int(output["amount_chipbits"]),
                        recipient=str(output["recipient"]),
                    )
                    for output in template_payload["node_reward_outputs"]
                ),
            ),
            metadata={
                "coinbase": "true",
                "height": str(template_payload["height"]),
                "extra_nonce": str(extra_nonce),
                "miner_id": self.config.miner_id,
            },
        )

    def _decode_template_transactions(self, template_payload: dict[str, Any]) -> tuple[Transaction, ...]:
        """Decode non-coinbase template transactions from raw hex."""

        transactions: list[Transaction] = []
        for row in template_payload["transactions"]:
            transaction, offset = deserialize_transaction(bytes.fromhex(str(row["raw_hex"])))
            if offset != len(bytes.fromhex(str(row["raw_hex"]))):
                raise ValueError("Template transaction contained trailing bytes.")
            transactions.append(transaction)
        return tuple(transactions)
