import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.models import Block, OutPoint
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import serialize_transaction
from chipcoin.interfaces.http_api import HttpApiApp
from chipcoin.node.service import NodeService
from ..helpers import put_wallet_utxo, signed_payment, wallet_key


def _make_service(database_path: Path) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_400))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _call_wsgi(app, *, method: str, path: str, query: str = "", body: object | None = None, origin: str | None = None):
    encoded_body = b"" if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(encoded_body)),
        "wsgi.input": BytesIO(encoded_body),
    }
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    captured: dict[str, object] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    raw = b"".join(app(environ, start_response))
    payload = None if not raw else json.loads(raw.decode("utf-8"))
    return captured["status"], captured["headers"], payload


def test_http_api_health_status_and_tip() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        health_status, _, health_body = _call_wsgi(app, method="GET", path="/v1/health")
        status_status, _, status_body = _call_wsgi(app, method="GET", path="/v1/status")
        tip_status, _, tip_body = _call_wsgi(app, method="GET", path="/v1/tip")

        assert health_status == "200 OK"
        assert health_body == {"status": "ok"}
        assert status_status == "200 OK"
        assert status_body["network"] == "mainnet"
        assert tip_status == "200 OK"
        assert tip_body == {"height": None, "block_hash": None}


def test_http_api_blocks_and_block_lookup_by_height_and_hash() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        first = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(first)
        second = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(second)
        app = HttpApiApp(service)

        blocks_status, _, blocks_body = _call_wsgi(app, method="GET", path="/v1/blocks")
        by_height_status, _, by_height_body = _call_wsgi(app, method="GET", path="/v1/block", query="height=1")
        by_hash_status, _, by_hash_body = _call_wsgi(app, method="GET", path="/v1/block", query=f"hash={first.block_hash()}")

        assert blocks_status == "200 OK"
        assert [row["height"] for row in blocks_body] == [1, 0]
        assert by_height_status == "200 OK"
        assert by_height_body["block_hash"] == second.block_hash()
        assert by_height_body["height"] == 1
        assert by_hash_status == "200 OK"
        assert by_hash_body["block_hash"] == first.block_hash()
        assert by_hash_body["height"] == 0


def test_http_api_block_lookup_rejects_invalid_queries_and_returns_not_found() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        both_status, _, both_body = _call_wsgi(app, method="GET", path="/v1/block", query="hash=aa&height=0")
        missing_status, _, missing_body = _call_wsgi(app, method="GET", path="/v1/block")
        not_found_status, _, not_found_body = _call_wsgi(app, method="GET", path="/v1/block", query="height=5")

        assert both_status == "400 Bad Request"
        assert both_body["error"]["code"] == "invalid_request"
        assert missing_status == "400 Bad Request"
        assert missing_body["error"]["code"] == "invalid_request"
        assert not_found_status == "404 Not Found"
        assert not_found_body["error"]["code"] == "not_found"


def test_http_api_transaction_lookup_and_submit() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="11" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        app = HttpApiApp(service)
        raw_hex = serialize_transaction(transaction).hex()

        submit_status, _, submit_body = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={"raw_hex": raw_hex})
        tx_status, _, tx_body = _call_wsgi(app, method="GET", path=f"/v1/tx/{transaction.txid()}")

        assert submit_status == "200 OK"
        assert submit_body["accepted"] is True
        assert tx_status == "200 OK"
        assert tx_body["location"] == "mempool"
        assert tx_body["transaction"]["txid"] == transaction.txid()


def test_http_api_submit_tx_reports_invalid_and_validation_errors() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="22" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        raw_hex = serialize_transaction(transaction).hex()
        app = HttpApiApp(service)

        invalid_status, _, invalid_body = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={})
        first_status, _, _ = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={"raw_hex": raw_hex})
        second_status, _, second_body = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={"raw_hex": raw_hex})

        assert invalid_status == "400 Bad Request"
        assert invalid_body["error"]["code"] == "invalid_request"
        assert first_status == "200 OK"
        assert second_status == "400 Bad Request"
        assert second_body["error"]["code"] == "validation_error"


def test_http_api_tx_lookup_returns_not_found() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        status, _, body = _call_wsgi(app, method="GET", path=f"/v1/tx/{'aa' * 32}")

        assert status == "404 Not Found"
        assert body["error"]["code"] == "not_found"


def test_http_api_address_summary_and_utxos() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        put_wallet_utxo(service, OutPoint(txid="33" * 32, index=0), value=75, owner=owner, height=0, is_coinbase=False)
        put_wallet_utxo(service, OutPoint(txid="44" * 32, index=1), value=50, owner=owner, height=0, is_coinbase=True)
        app = HttpApiApp(service)

        summary_status, _, summary_body = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}")
        utxos_status, _, utxos_body = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}/utxos")

        assert summary_status == "200 OK"
        assert summary_body["address"] == owner.address
        assert summary_body["confirmed_balance_chipbits"] == 125
        assert summary_body["immature_balance_chipbits"] == 50
        assert summary_body["spendable_balance_chipbits"] == 75
        assert utxos_status == "200 OK"
        assert len(utxos_body) == 2
        assert {row["txid"] for row in utxos_body} == {"33" * 32, "44" * 32}


def test_http_api_address_history() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        recipient = wallet_key(1)
        funding_outpoint = OutPoint(txid="55" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)
        transaction = signed_payment(funding_outpoint, value=100, sender=owner, recipient=recipient.address, fee=10)
        service.receive_transaction(transaction)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)
        app = HttpApiApp(service)

        status, _, body = _call_wsgi(
            app,
            method="GET",
            path=f"/v1/address/{recipient.address}/history",
            query="limit=10&order=desc",
        )

        assert status == "200 OK"
        assert len(body) >= 1
        assert body[0]["txid"] == transaction.txid()
        assert body[0]["incoming_chipbits"] > 0
        assert "net_chipbits" in body[0]


def test_http_api_address_endpoints_reject_invalid_address() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        summary_status, _, summary_body = _call_wsgi(app, method="GET", path="/v1/address/not-a-valid-address")
        history_status, _, history_body = _call_wsgi(app, method="GET", path="/v1/address/not-a-valid-address/history")

        assert summary_status == "400 Bad Request"
        assert summary_body["error"]["code"] == "invalid_request"
        assert history_status == "400 Bad Request"
        assert history_body["error"]["code"] == "invalid_request"


def test_http_api_mempool_includes_machine_friendly_fee_rate() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="66" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        service.receive_transaction(transaction)
        app = HttpApiApp(service)

        status, _, body = _call_wsgi(app, method="GET", path="/v1/mempool")

        assert status == "200 OK"
        assert len(body) == 1
        assert body[0]["txid"] == transaction.txid()
        assert "fee_rate" in body[0]
        assert "fee_rate_chipbits_per_weight_unit" in body[0]
        assert isinstance(body[0]["fee_rate_chipbits_per_weight_unit"], float)


def test_http_api_peers_and_peers_summary() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("127.0.0.1", 8333)
        app = HttpApiApp(service)

        peers_status, _, peers_body = _call_wsgi(app, method="GET", path="/v1/peers")
        summary_status, _, summary_body = _call_wsgi(app, method="GET", path="/v1/peers/summary")

        assert peers_status == "200 OK"
        assert peers_body[0]["host"] == "127.0.0.1"
        assert "handshake_complete" in peers_body[0]
        assert summary_status == "200 OK"
        assert summary_body["peer_count"] == 1
        assert summary_body["peer_count_by_network"]["mainnet"] == 1


def test_http_api_blocks_validation() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        block = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(block)
        app = HttpApiApp(service)

        limit_status, _, limit_body = _call_wsgi(app, method="GET", path="/v1/blocks", query="limit=101")
        from_status, _, from_body = _call_wsgi(app, method="GET", path="/v1/blocks", query="from_height=10")

        assert limit_status == "400 Bad Request"
        assert limit_body["error"]["code"] == "invalid_request"
        assert from_status == "400 Bad Request"
        assert from_body["error"]["code"] == "invalid_request"


def test_http_api_cors_allow_list() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service, allowed_origins={"http://localhost:3000"})

        ok_status, ok_headers, _ = _call_wsgi(app, method="GET", path="/v1/health", origin="http://localhost:3000")
        blocked_status, blocked_headers, _ = _call_wsgi(app, method="GET", path="/v1/health", origin="http://evil.example")
        options_status, options_headers, options_body = _call_wsgi(
            app,
            method="OPTIONS",
            path="/v1/status",
            origin="http://localhost:3000",
        )

        assert ok_status == "200 OK"
        assert ok_headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert "Access-Control-Allow-Origin" not in blocked_headers
        assert blocked_status == "200 OK"
        assert options_status == "204 No Content"
        assert options_headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert options_headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
        assert options_headers["Access-Control-Allow-Headers"] == "Content-Type"
        assert options_body is None


def test_http_api_handles_concurrent_read_requests() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        for index in range(3):
            block = _mine_block(service.build_candidate_block(owner.address).block)
            service.apply_block(block)
        app = HttpApiApp(service)

        requests = [
            ("GET", "/v1/status", ""),
            ("GET", "/v1/blocks", "limit=3"),
            ("GET", f"/v1/address/{owner.address}", ""),
            ("GET", f"/v1/address/{owner.address}/utxos", ""),
            ("GET", f"/v1/address/{owner.address}/history", "limit=10&order=desc"),
        ]

        with ThreadPoolExecutor(max_workers=len(requests)) as executor:
            results = list(
                executor.map(
                    lambda request: _call_wsgi(app, method=request[0], path=request[1], query=request[2]),
                    requests,
                )
            )

        assert all(status == "200 OK" for status, _headers, _body in results)
