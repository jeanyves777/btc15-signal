import asyncio
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from btc15_signal.execution import KalshiExecutionClient, event_order
from btc15_signal.store import Store


def test_event_order_maps_up_and_down_to_single_yes_book():
    assert event_order("UP", 0.35) == ("bid", 0.35)
    assert event_order("UP", 0.50, exiting=True) == ("ask", 0.50)
    assert event_order("DOWN", 0.35) == ("ask", 0.65)
    assert event_order("DOWN", 0.50, exiting=True) == ("bid", 0.50)


def test_trade_proposal_can_only_be_claimed_once(tmp_path):
    store = Store(str(tmp_path / "proposals.db"))
    proposal = store.create_proposal("reversion", 0, "TEST", "DOWN", 0.35, 0.50, 2, 1000, 900000, 1)
    assert store.claim_proposal(proposal.id, 100) is not None
    assert store.claim_proposal(proposal.id, 100) is None


def test_kalshi_signature_uses_full_api_path(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "kalshi.key"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr("btc15_signal.execution.time.time", lambda: 1234.567)
    client = KalshiExecutionClient(
        "https://external-api.kalshi.com/trade-api/v2", "key-id", str(key_path)
    )
    headers = client._headers("POST", "/portfolio/events/orders")
    private_key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1234567POST/trade-api/v2/portfolio/events/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    asyncio.run(client.close())
