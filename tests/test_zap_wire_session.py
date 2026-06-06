"""Tests for ZAP v1.0 connection-level session semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard import zap_transport as zt


def test_wire_frame_roundtrips_without_zap_py():
    frame = zt.ZapWireFrame(
        zt.ZapWireFrameType.DATA,
        seq=7,
        payload=b"offer-wire",
        capabilities=zt.ZapWireCapability.NESTED_X402,
    )

    out = zt.ZapWireFrame.decode(frame.encode())

    assert out == frame


def test_wire_frame_rejects_bad_magic_and_payload_size():
    frame = zt.ZapWireFrame(zt.ZapWireFrameType.HELLO)
    wire = bytearray(frame.encode())
    wire[0:4] = b"NOPE"

    with pytest.raises(zt.ZapWireSessionError, match="magic"):
        zt.ZapWireFrame.decode(bytes(wire))

    with pytest.raises(zt.ZapWireSessionError, match="16 KiB"):
        zt.ZapWireFrame(zt.ZapWireFrameType.DATA, payload=b"x" * (16 * 1024 + 1)).encode()


def test_wire_frame_rejects_unknown_capability_bits():
    wire = bytearray(zt.ZapWireFrame.hello(zt.ZapWireCapability.PQ_ENVELOPE).encode())
    wire[6:8] = (0x8000).to_bytes(2, "big")

    with pytest.raises(zt.ZapWireSessionError, match="capability"):
        zt.ZapWireFrame.decode(bytes(wire))


def test_handshake_negotiates_capability_intersection():
    client = zt.ZapWireSession(
        capabilities=zt.ZapWireCapability.PQ_ENVELOPE | zt.ZapWireCapability.NESTED_X402
    )
    server = zt.ZapWireSession(
        capabilities=zt.ZapWireCapability.NESTED_X402 | zt.ZapWireCapability.MPP_SESSION
    )

    hello_ack = server.accept_hello(client.make_hello())
    final_ack = client.accept_hello(hello_ack)

    assert hello_ack.frame_type == zt.ZapWireFrameType.HELLO_ACK
    assert hello_ack.capabilities == zt.ZapWireCapability.NESTED_X402
    assert client.peer_capabilities == zt.ZapWireCapability.NESTED_X402
    assert server.peer_capabilities == zt.ZapWireCapability.NESTED_X402
    assert final_ack.frame_type == zt.ZapWireFrameType.ACK


def test_data_ack_and_request_id_duplicate_handling():
    sender = zt.ZapWireSession()
    receiver = zt.ZapWireSession()

    data = sender.make_data(b"payment-offer-wire", request_id="req-1")
    first = receiver.receive_data(data, request_id="req-1")
    duplicate = receiver.receive_data(data, request_id="req-1")

    assert first.status == "accepted"
    assert first.payload == b"payment-offer-wire"
    assert first.ack is not None
    assert sender.receive_ack(first.ack) == data.seq
    assert sender.next_retry(data.seq) is None

    assert duplicate.status == "duplicate"
    assert duplicate.ack is not None
    assert duplicate.ack.acked_seq() == data.seq


def test_retry_policy_backoff_and_attempt_cap():
    session = zt.ZapWireSession(
        retry_policy=zt.ZapRetryPolicy(initial_timeout_ms=100, max_timeout_ms=250, max_attempts=3)
    )
    data = session.make_data(b"payment-proof-wire", request_id="req-2")

    assert session.next_retry(data.seq) == (data, 100)
    assert session.next_retry(data.seq) == (data, 200)
    assert session.next_retry(data.seq) == (data, 250)

    with pytest.raises(zt.ZapWireSessionError, match="exceeded retry"):
        session.next_retry(data.seq)


def test_fin_surfaces_unacked_sequences():
    session = zt.ZapWireSession()
    first = session.make_data(b"one")
    second = session.make_data(b"two")

    orphaned = session.receive_fin(zt.ZapWireFrame(zt.ZapWireFrameType.FIN, seq=99))

    assert orphaned == [first.seq, second.seq]
    assert session.closed is True


def test_static_zap_wire_v1_conformance_vectors():
    vector_path = Path(__file__).parent / "conformance" / "zap_wire_v1_vectors.json"
    vectors = json.loads(vector_path.read_text())

    for vector in vectors:
        frame = zt.ZapWireFrame(
            frame_type=zt.ZapWireFrameType[vector["frame_type"]],
            seq=vector["seq"],
            capabilities=zt.ZapWireCapability(vector["capabilities"]),
            payload=bytes.fromhex(vector["payload_hex"]),
        )
        assert frame.encode().hex() == vector["wire_hex"], vector["name"]
        assert zt.ZapWireFrame.decode(bytes.fromhex(vector["wire_hex"])) == frame


def test_ack_payload_is_four_byte_sequence_echo():
    ack = zt.ZapWireFrame.ack(0x01020304)

    assert ack.payload.hex() == "01020304"
    assert ack.acked_seq() == 0x01020304

    with pytest.raises(zt.ZapWireSessionError, match="uint32"):
        zt.ZapWireFrame.ack(0x1_0000_0000)
