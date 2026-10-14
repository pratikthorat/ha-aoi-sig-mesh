import asyncio

from mesh.config import (
    config_appkey_add,
    config_composition_get,
    config_model_app_bind,
    config_model_publication_set,
    config_node_reset,
    parse_composition_status,
)
from mesh.models import (
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
    PDU_BEACON,
    PDU_NETWORK,
    PDU_PROXY_CONFIG,
    FilterStatus,
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
from mesh.transport import decrypt_upper, encrypt_upper, parse_lower_access, unsegmented_access
from tests.sim.mesh_sim import APP_KEY, CLIENT, SimMesh, SimNode, run

DEV_KEY = bytes.fromhex("9d6dd0e96eb25dc19a40ed9914f8f03f")


def light(**kwargs):
    return SimNode(unicast=0x00B0, dev_key=DEV_KEY, **kwargs)


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


def client_network_pdu(sim, dst, access, seq):
    upper = encrypt_upper(
        APP_KEY, akf=1, szmic=0, seq=seq, src=CLIENT, dst=dst, iv_index=0, access_payload=access
    )
    return encrypt_network_pdu(
        sim.keys,
        ctl=0,
        ttl=5,
        seq=seq,
        src=CLIENT,
        dst=dst,
        transport_pdu=unsegmented_access(upper, akf=1, aid=sim.aid),
        iv_index=0,
    )


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
