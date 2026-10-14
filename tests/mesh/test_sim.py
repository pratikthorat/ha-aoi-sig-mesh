import asyncio

import pytest

from mesh.config import (
    STATUS_SUCCESS,
    config_appkey_add,
    config_composition_get,
    config_model_app_bind,
    config_model_publication_set,
    config_node_reset,
    parse_composition_status,
)
from mesh.errors import LinkClosedError
from mesh.models import (
    OP_ONOFF_SET,
    ctl_set,
    ctl_temperature_range_get,
    ctl_temperature_set,
    health_attention_set,
    lightness_get,
    lightness_set,
    onoff_get,
    onoff_set,
)
from mesh.pdu import (
    FILTER_ACCEPT,
    FILTER_REJECT,
    PDU_BEACON,
    PDU_NETWORK,
    PDU_PROXY_CONFIG,
    FilterStatus,
    NetworkKeys,
    ProxyReassembler,
    SecureBeacon,
    add_addresses,
    decrypt_network_pdu,
    decrypt_proxy_config,
    encrypt_network_pdu,
    encrypt_proxy_config,
    parse_filter_status,
    parse_secure_beacon,
    proxy_pdus,
    set_filter_type,
)
from mesh.transport import (
    SegmentAck,
    SegmentReassembler,
    decrypt_upper,
    encrypt_upper,
    parse_lower_access,
    parse_segment_ack,
    segmented_access,
    unsegmented_access,
)
from tests.sim.mesh_sim import APP_KEY, CLIENT, TUYA_VENDOR_MODEL, SimMesh, SimNode, run

DEV_KEY = bytes.fromhex("9d6dd0e96eb25dc19a40ed9914f8f03f")


def light(**kwargs):
    node = SimNode(unicast=0x00B0, dev_key=DEV_KEY, **kwargs)
    node.bind_all()
    return node


def test_spec_light_behaviour():
    node = light()
    assert node.handle(0x00B0, lightness_set(1000, 1), dev_key=False) == [
        (0x00B0, bytes.fromhex("824ee803"))
    ]
    assert (node.on, node.lightness) == (True, 1000)
    node.handle(0x00B0, lightness_set(0, 2), dev_key=False)
    assert node.on is False
    assert node.handle(0x00B0, lightness_get(), dev_key=False) == [
        (0x00B0, bytes.fromhex("824e0000"))
    ]
    node.handle(0x00B0, ctl_set(2000, 3000, 3), dev_key=False)
    assert (node.on, node.lightness, node.kelvin) == (True, 2000, 3000)


def test_tuya_quirks():
    node = light(tuya_quirks=True)
    replies = node.handle(0x00B0, lightness_set(1000, 1), dev_key=False)
    assert replies == [(0x00B0, bytes.fromhex("824e0000"))]  # ignored while off
    assert node.on is False
    node.handle(0x00B0, onoff_set(True, 2), dev_key=False)
    node.handle(0x00B0, lightness_set(0, 3), dev_key=False)
    assert (node.on, node.lightness) == (True, 0xFFFF)  # Lightness 0 does not turn it off
    node.handle(0x00B0, onoff_set(False, 4), dev_key=False)
    node.handle(0x00B1, ctl_temperature_set(3000, 5), dev_key=False)
    assert node.kelvin == 4000  # CTL Temperature Set ignored while off


def test_ctl_temperature_only_on_second_element():
    node = light()
    assert node.handle(0x00B0, ctl_temperature_set(3000, 1), dev_key=False) == []
    assert node.handle(0x00B1, ctl_temperature_set(3000, 1), dev_key=False) == [
        (0x00B1, bytes.fromhex("8266b80b0000"))
    ]
    assert node.handle(0x00B0, ctl_temperature_range_get(), dev_key=False) == [
        (0x00B0, bytes.fromhex("8263008c0a6419"))
    ]


def test_unacknowledged_sets_get_no_reply():
    node = light()
    assert node.handle(0x00B0, onoff_set(True, 1, ack=False), dev_key=False) == []
    assert node.on is True
    assert node.handle(0x00B0, health_attention_set(5), dev_key=False) == []
    assert node.attention == 5


def test_plug_only_answers_onoff():
    plug = SimNode(unicast=0x0010, dev_key=DEV_KEY, kind="onoff")
    plug.bind_all()
    assert plug.element_count == 1
    assert plug.handle(0x0010, lightness_get(), dev_key=False) == []
    assert plug.handle(0x0010, onoff_get(), dev_key=False) == [(0x0010, bytes.fromhex("820400"))]


def test_config_messages():
    node = light(has_app_key=False)
    assert node.handle(0x00B0, config_appkey_add(0, 0, APP_KEY), dev_key=True) == [
        (0x00B0, bytes.fromhex("800300000000"))
    ]
    assert node.has_app_key
    [(_, reply)] = node.handle(0x00B0, config_model_app_bind(0x00B1, 0, 0x1306), dev_key=True)
    assert reply == bytes.fromhex("803e00b10000000613")
    assert (0x00B1, 0x1306) in node.bindings
    publication = config_model_publication_set(0x00B0, 0x0001, 0, 0x1000)
    [(_, reply)] = node.handle(0x00B0, publication, dev_key=True)
    assert reply == bytes.fromhex("8019") + b"\x00" + publication[1:]
    assert node.publications == {(0x00B0, 0x1000): 0x0001}
    [(_, reply)] = node.handle(0x00B0, config_composition_get(), dev_key=True)
    assert parse_composition_status(reply[1:]) == node.composition()
    assert node.handle(0x00B0, config_node_reset(), dev_key=True) == [
        (0x00B0, bytes.fromhex("804a"))
    ]
    assert node.handle(0x00B0, onoff_get(), dev_key=False) == []  # unprovisioned now


def notifications(transport):
    received = []
    transport.set_notify_callback(received.append)
    return received


def messages(parts):
    rx = ProxyReassembler()
    return [m for m in map(rx.feed, parts) if m is not None]


def client_network_pdu(sim, dst, access, seq, *, dev_key=None):
    akf, key, aid = (0, dev_key, 0) if dev_key else (1, APP_KEY, sim.aid)
    upper = encrypt_upper(
        key, akf=akf, szmic=0, seq=seq, src=CLIENT, dst=dst, iv_index=0, access_payload=access
    )
    return encrypt_network_pdu(
        sim.keys,
        ctl=0,
        ttl=5,
        seq=seq,
        src=CLIENT,
        dst=dst,
        transport_pdu=unsegmented_access(upper, akf=akf, aid=aid),
        iv_index=0,
    )


async def write_pdus(transport, *pdus, pdu_type=PDU_NETWORK):
    """Write whole proxy PDUs to the sim, then let its notifications arrive."""
    for pdu in pdus:
        for part in proxy_pdus(pdu_type, pdu, transport.max_write):
            await transport.write(part)
    await asyncio.sleep(0)


def access_replies(sim, received, *, dev_key=None, iv_index=0):
    """Decrypt the unsegmented access messages the sim sent to the client."""
    replies = []
    for pdu_type, payload in messages(received):
        net = decrypt_network_pdu(sim.keys, payload, iv_index)
        if pdu_type != PDU_NETWORK or net.ctl:
            continue
        lower = parse_lower_access(net.transport_pdu)
        replies.append(
            decrypt_upper(
                APP_KEY if lower.akf else dev_key,
                akf=lower.akf,
                szmic=0,
                seq=net.seq,
                src=net.src,
                dst=net.dst,
                iv_index=iv_index,
                upper=lower.data,
            )
        )
    return replies


def test_connect_sends_a_beacon():
    sim = SimMesh()

    async def scenario():
        received = notifications(sim.connect())
        await asyncio.sleep(0)
        return messages(received)

    [(pdu_type, payload)] = run(scenario())
    assert pdu_type == PDU_BEACON
    assert parse_secure_beacon(sim.keys, payload) == SecureBeacon(False, False, 0)


def test_onoff_set_round_trip():
    sim = SimMesh()
    node = sim.add_node("ctl")
    assert (node.unicast, sim.dev_key_for(0x00B1)) == (0x00B0, node.dev_key)

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        pdu = client_network_pdu(sim, node.unicast, onoff_set(True, 1), seq=1)
        for part in proxy_pdus(PDU_NETWORK, pdu, transport.max_write):
            await transport.write(part)
        await asyncio.sleep(0)
        return messages(received)

    [(pdu_type, payload)] = run(scenario())
    assert pdu_type == PDU_NETWORK
    net = decrypt_network_pdu(sim.keys, payload, 0)
    assert (net.src, net.dst) == (node.unicast, CLIENT)
    lower = parse_lower_access(net.transport_pdu)
    access = decrypt_upper(
        APP_KEY, akf=1, szmic=0, seq=net.seq, src=net.src, dst=CLIENT, iv_index=0, upper=lower.data
    )
    assert access == bytes.fromhex("820401")
    assert node.on
    assert sim.filter_addresses == {CLIENT}  # auto-added from the client's src


def test_proxy_filter_messages():
    sim = SimMesh()

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        for seq, message in enumerate((set_filter_type(FILTER_ACCEPT), add_addresses(CLIENT))):
            pdu = encrypt_proxy_config(sim.keys, seq=seq, src=CLIENT, iv_index=0, message=message)
            for part in proxy_pdus(PDU_PROXY_CONFIG, pdu, transport.max_write):
                await transport.write(part)
        await asyncio.sleep(0)
        return messages(received)

    replies = run(scenario())
    assert [pdu_type for pdu_type, _ in replies] == [PDU_PROXY_CONFIG, PDU_PROXY_CONFIG]
    statuses = [
        parse_filter_status(decrypt_proxy_config(sim.keys, payload, 0).transport_pdu)
        for _, payload in replies
    ]
    assert statuses == [FilterStatus(FILTER_ACCEPT, 0), FilterStatus(FILTER_ACCEPT, 1)]
    assert sim.filter_addresses == {CLIENT}


def test_replayed_pdu_is_dropped():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        pdu = client_network_pdu(sim, node.unicast, onoff_get(), seq=1)
        await write_pdus(transport, pdu, pdu)  # the same bytes twice
        return access_replies(sim, received)

    assert run(scenario()) == [bytes.fromhex("820400")]
    assert len(sim.from_client) == 2  # both logged, only the first delivered


def test_lower_seq_from_same_src_is_dropped():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        await write_pdus(
            transport,
            client_network_pdu(sim, node.unicast, onoff_set(True, 1), seq=5),
            client_network_pdu(sim, node.unicast, onoff_set(False, 2), seq=4),
        )
        return access_replies(sim, received)

    assert run(scenario()) == [bytes.fromhex("820401")]
    assert node.on is True


def test_accept_replays_opts_out():
    sim = SimMesh()
    sim.accept_replays = True
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        pdu = client_network_pdu(sim, node.unicast, onoff_get(), seq=1)
        await write_pdus(transport, pdu, pdu)
        return access_replies(sim, received)

    assert run(scenario()) == [bytes.fromhex("820400")] * 2


def test_forget_replay_accepts_a_restarted_seq():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        await write_pdus(transport, client_network_pdu(sim, node.unicast, onoff_get(), seq=9))
        sim.forget_replay(CLIENT, 1)
        await write_pdus(transport, client_network_pdu(sim, node.unicast, onoff_get(), seq=0))
        return access_replies(sim, received)

    assert run(scenario()) == [bytes.fromhex("820400")] * 2


def test_unbound_model_is_ignored():
    sim = SimMesh()
    node = sim.add_node("ctl", configured=False)
    assert node.bindings == set()
    node.handle(node.unicast, config_appkey_add(0, 0, APP_KEY), dev_key=True)
    assert node.has_app_key
    assert node.handle(node.unicast, onoff_set(True, 1), dev_key=False) == []
    assert node.on is False
    assert node.received[-1] == (node.unicast, OP_ONOFF_SET, onoff_set(True, 1)[2:])
    node.handle(node.unicast, config_model_app_bind(node.unicast, 0, 0x1000), dev_key=True)
    assert node.handle(node.unicast, onoff_set(True, 2), dev_key=False) == [
        (node.unicast, bytes.fromhex("820401"))
    ]
    assert node.on is True


def test_configured_node_is_bound_to_every_app_key_model():
    node = SimMesh().add_node("ctl", configured=True)
    assert node.bindings == {
        (0x00B0, 0x1000),
        (0x00B0, 0x1300),
        (0x00B0, 0x1303),
        (0x00B0, TUYA_VENDOR_MODEL),
        (0x00B1, 0x1306),
    }


def test_bind_to_missing_model_returns_invalid_model():
    node = SimNode(unicast=0x00B0, dev_key=DEV_KEY)
    [(_, reply)] = node.handle(0x00B0, config_model_app_bind(0x00B0, 0, 0x1306), dev_key=True)
    assert reply == bytes.fromhex("803e02b00000000613")  # 0x1306 is on element 1
    [(_, reply)] = node.handle(0x00B0, config_model_app_bind(0x00B2, 0, 0x1000), dev_key=True)
    assert reply == bytes.fromhex("803e01b20000000010")  # no element 2
    publication = config_model_publication_set(0x00B1, 0x0001, 0, 0x1000)
    [(_, reply)] = node.handle(0x00B0, publication, dev_key=True)
    assert reply == bytes.fromhex("801902") + publication[1:]
    publication = config_model_publication_set(0x00AF, 0x0001, 0, 0x1000)
    [(_, reply)] = node.handle(0x00B0, publication, dev_key=True)
    assert reply == bytes.fromhex("801901") + publication[1:]
    assert (node.bindings, node.publications) == (set(), {})
    [(_, reply)] = node.handle(
        0x00B0, config_model_app_bind(0x00B0, 0, TUYA_VENDOR_MODEL), dev_key=True
    )
    assert reply[2] == STATUS_SUCCESS
    assert node.bindings == {(0x00B0, TUYA_VENDOR_MODEL)}


def test_config_to_secondary_element_is_ignored():
    node = light(has_app_key=False)
    assert node.handle(0x00B1, config_composition_get(), dev_key=True) == []
    assert node.handle(0x00B1, config_appkey_add(0, 0, APP_KEY), dev_key=True) == []
    assert node.handle(0x00B1, config_node_reset(), dev_key=True) == []
    assert (node.has_app_key, node.reset) == (False, False)


def test_reset_node_forgets_segments():
    sim = SimMesh()
    node = sim.add_node("ctl")
    forgotten = []

    class SpyReassembler(SegmentReassembler):
        def forget(self, first, count):
            forgotten.append((first, count))
            super().forget(first, count)

    sim._segments = SpyReassembler()

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        reset = client_network_pdu(
            sim, node.unicast, config_node_reset(), seq=1, dev_key=node.dev_key
        )
        await write_pdus(transport, reset)
        return access_replies(sim, received, dev_key=node.dev_key)

    assert run(scenario()) == [bytes.fromhex("804a")]
    assert node.reset
    assert forgotten == [(0x00B0, 2)]


def test_sets_need_the_exact_short_or_long_form():
    node = light()
    assert node.handle(0x00B0, onoff_set(True, 1) + b"\x00", dev_key=False) == []
    assert node.handle(0x00B0, onoff_set(True, 1)[:3], dev_key=False) == []
    assert node.on is False
    assert node.handle(0x00B0, onoff_set(True, 2, transition_ms=100), dev_key=False) == [
        (0x00B0, bytes.fromhex("820401"))
    ]
    assert node.handle(0x00B0, lightness_set(1000, 3) + b"\x00", dev_key=False) == []
    assert node.handle(0x00B0, ctl_set(1000, 3000, 4) + b"\x00", dev_key=False) == []
    assert node.handle(0x00B1, ctl_temperature_set(3000, 5) + b"\x00", dev_key=False) == []
    assert (node.lightness, node.kelvin) == (0xFFFF, 4000)
    node.handle(0x00B0, lightness_set(1000, 6, transition_ms=100), dev_key=False)
    node.handle(0x00B0, ctl_set(2000, 3000, 7, transition_ms=100), dev_key=False)
    assert (node.lightness, node.kelvin) == (2000, 3000)
    node.handle(0x00B1, ctl_temperature_set(5000, 8, transition_ms=100), dev_key=False)
    assert node.kelvin == 5000


def test_kelvin_is_clamped_to_the_node_range():
    node = light(on=True)
    assert node.handle(0x00B0, ctl_set(1000, 2000, 1), dev_key=False) == [
        (0x00B0, bytes.fromhex("8260e8038c0a"))  # 2000 K reported as 2700 K
    ]
    assert node.handle(0x00B1, ctl_temperature_set(9000, 2), dev_key=False) == [
        (0x00B1, bytes.fromhex("826664190000"))  # 9000 K reported as 6500 K
    ]
    assert node.kelvin == 6500


def client_segments(sim, dst, access, first_seq, *, iv_index=0):
    """A client app-key message as segmented network PDUs (seqs first_seq, first_seq + 1, ...)."""
    upper = encrypt_upper(
        APP_KEY,
        akf=1,
        szmic=0,
        seq=first_seq,
        src=CLIENT,
        dst=dst,
        iv_index=iv_index,
        access_payload=access,
    )
    lowers = segmented_access(upper, akf=1, aid=sim.aid, szmic=0, seq_zero=first_seq)
    return [
        encrypt_network_pdu(
            sim.keys,
            ctl=0,
            ttl=5,
            seq=first_seq + index,
            src=CLIENT,
            dst=dst,
            transport_pdu=lower,
            iv_index=iv_index,
        )
        for index, lower in enumerate(lowers)
    ]


def segment_acks(sim, received, *, iv_index=0):
    acks = []
    for pdu_type, payload in messages(received):
        net = decrypt_network_pdu(sim.keys, payload, iv_index)
        if pdu_type == PDU_NETWORK and net.ctl:
            acks.append(parse_segment_ack(net.transport_pdu))
    return acks


def long_ctl_set():
    return ctl_set(1000, 3000, 1, transition_ms=100)  # 11 bytes: 2 segments with the TransMIC


def test_segmented_message_to_node_is_acked_and_answered():
    sim = SimMesh(iv_index=5)
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        segments = client_segments(sim, node.unicast, long_ctl_set(), 10, iv_index=5)
        assert len(segments) == 2
        await write_pdus(transport, *segments)
        return received

    received = run(scenario())
    assert segment_acks(sim, received, iv_index=5) == [SegmentAck(False, 10, 0b11)]
    assert access_replies(sim, received, iv_index=5) == [bytes.fromhex("8260e803b80b")]
    assert (node.on, node.lightness, node.kelvin) == (True, 1000, 3000)


def test_segmented_message_from_node_reassembles():
    sim = SimMesh()
    node = sim.add_node("ctl")
    access = bytes.fromhex("8260") + bytes(range(16))

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        sim.filter_addresses.add(CLIENT)
        sim.send_access(node.unicast, CLIENT, access, segmented=True)
        await asyncio.sleep(0)
        return messages(received)

    rx = SegmentReassembler()
    results = []
    for _, payload in run(scenario()):
        net = decrypt_network_pdu(sim.keys, payload, 0)
        results.append(rx.add(net.src, net.seq, parse_lower_access(net.transport_pdu), iv_index=0))
    assert [result.message is None for result in results] == [True, False]
    done = results[-1].message
    assert (
        decrypt_upper(
            APP_KEY,
            akf=1,
            szmic=0,
            seq=done.seq_auth,
            src=node.unicast,
            dst=CLIENT,
            iv_index=done.iv_index,
            upper=done.upper,
        )
        == access
    )


def test_silent_gives_no_reply_and_no_ack():
    sim = SimMesh()
    sim.silent = True
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        await write_pdus(transport, *client_segments(sim, node.unicast, long_ctl_set(), 1))
        await write_pdus(transport, client_network_pdu(sim, node.unicast, onoff_get(), seq=3))
        return received

    assert run(scenario()) == []
    assert node.lightness == 1000  # still applied, just not answered


def test_drop_from_client_loses_the_next_pdu_only():
    sim = SimMesh()
    sim.drop_from_client = 1
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        await write_pdus(
            transport,
            client_network_pdu(sim, node.unicast, onoff_set(True, 1), seq=1),
            client_network_pdu(sim, node.unicast, onoff_get(), seq=2),
        )
        return access_replies(sim, received)

    assert run(scenario()) == [bytes.fromhex("820400")]
    assert [pdu.seq for pdu in sim.from_client] == [2]
    assert node.on is False


def test_reject_segments_acks_with_block_ack_zero():
    sim = SimMesh()
    sim.reject_segments = True
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        await write_pdus(transport, *client_segments(sim, node.unicast, long_ctl_set(), 1))
        return received

    received = run(scenario())
    assert segment_acks(sim, received) == [SegmentAck(False, 1, 0)] * 2
    assert access_replies(sim, received) == []
    assert node.lightness == 0xFFFF


def test_reject_segments_respects_silent():
    sim = SimMesh()
    sim.reject_segments = sim.silent = True
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        await write_pdus(transport, *client_segments(sim, node.unicast, long_ctl_set(), 1))
        return received

    assert run(scenario()) == []


def remove_addresses(*addresses):
    return bytes([0x02]) + b"".join(a.to_bytes(2, "big") for a in addresses)


async def proxy_config(sim, transport, received, *config_messages):
    """Send proxy configuration messages; return the Filter Statuses that came back."""
    del received[:]
    pdus = [
        encrypt_proxy_config(sim.keys, seq=100 + i, src=CLIENT, iv_index=0, message=message)
        for i, message in enumerate(config_messages)
    ]
    await write_pdus(transport, *pdus, pdu_type=PDU_PROXY_CONFIG)
    statuses = [
        parse_filter_status(decrypt_proxy_config(sim.keys, payload, 0).transport_pdu)
        for _, payload in messages(received)
    ]
    del received[:]
    return statuses


async def delivered(sim, received, node, dst):
    """Whether a status the node sends to dst gets through the proxy filter."""
    del received[:]
    sim.send_access(node.unicast, dst, bytes.fromhex("820401"))
    await asyncio.sleep(0)
    got_through = bool(received)
    del received[:]
    return got_through


def test_ignore_proxy_config_sends_no_filter_status():
    sim = SimMesh()
    sim.ignore_proxy_config = True

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        return await proxy_config(sim, transport, received, set_filter_type(FILTER_ACCEPT))

    assert run(scenario()) == []


def test_accept_filter_drops_unlisted_dst():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        before = await delivered(sim, received, node, 0x0005)
        await proxy_config(sim, transport, received, add_addresses(0x0005))
        return before, await delivered(sim, received, node, 0x0005)

    assert run(scenario()) == (False, True)


def test_reject_filter_drops_listed_dst():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        statuses = await proxy_config(
            sim, transport, received, set_filter_type(FILTER_REJECT), add_addresses(0x0005, CLIENT)
        )
        listed = await delivered(sim, received, node, 0x0005)
        unlisted = await delivered(sim, received, node, 0x0006)
        await write_pdus(transport, client_network_pdu(sim, node.unicast, onoff_get(), seq=1))
        return statuses, listed, unlisted, access_replies(sim, received)

    statuses, listed, unlisted, replies = run(scenario())
    assert statuses == [FilterStatus(FILTER_REJECT, 0), FilterStatus(FILTER_REJECT, 2)]
    assert (listed, unlisted) == (False, True)
    assert replies == [bytes.fromhex("820400")]  # a client PDU takes its src off the reject list
    assert sim.filter_addresses == {0x0005}


def test_remove_addresses_and_unknown_filter_type():
    sim = SimMesh()

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        statuses = await proxy_config(
            sim, transport, received, add_addresses(0x0005, 0x0006), remove_addresses(0x0005)
        )
        return statuses, await proxy_config(sim, transport, received, set_filter_type(2))

    statuses, unknown = run(scenario())
    assert statuses == [FilterStatus(FILTER_ACCEPT, 2), FilterStatus(FILTER_ACCEPT, 1)]
    assert unknown == []
    assert (sim.filter_type, sim.filter_addresses) == (FILTER_ACCEPT, {0x0006})


def test_send_beacon_overrides():
    sim = SimMesh()
    other_key = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")

    async def scenario():
        transport = sim.connect(beacon=False)
        received = notifications(transport)
        sim.send_beacon(iv_index=7, iv_update=True)
        sim.send_beacon(net_key=other_key)
        await asyncio.sleep(0)
        return messages(received)

    [(_, overridden), (_, foreign)] = run(scenario())
    assert parse_secure_beacon(sim.keys, overridden) == SecureBeacon(False, True, 7)
    assert parse_secure_beacon(sim.keys, foreign) is None
    assert parse_secure_beacon(NetworkKeys.from_net_key(other_key), foreign) == SecureBeacon(
        False, False, 0
    )


def test_close_and_drop_disconnect_the_transport():
    sim = SimMesh()
    node = sim.add_node("ctl")

    async def scenario():
        events = []
        for how in ("close", "drop"):
            transport = sim.connect(beacon=False)
            received = notifications(transport)
            transport.set_disconnect_callback(lambda how=how: events.append(how))
            if how == "close":
                await transport.close()
            else:
                transport.drop()
            sim.filter_addresses.add(CLIENT)
            sim.local_change(node, True)
            await asyncio.sleep(0)
            assert (transport.is_connected, received) == (False, [])
            with pytest.raises(LinkClosedError):
                await transport.write(b"\x00")
        return events

    assert run(scenario()) == ["close", "drop"]
