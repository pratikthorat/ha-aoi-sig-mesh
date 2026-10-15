"""Mesh core additions for the integration: confirmed IV jumps, replay drops, input checks."""

import logging
import pickle

import pytest

from mesh.errors import MeshError, ProvisioningError, SeqNotSaved
from mesh.network import BeaconOutcome, MeshNetwork
from mesh.node import allocate_unicast
from mesh.pdu import SecureBeacon
from tests.sim.mesh_sim import APP_KEY, CLIENT, NET_KEY


def network(durable=True, **state):
    saved = []
    net = MeshNetwork(
        net_key=NET_KEY,
        app_key=APP_KEY,
        address=CLIENT,
        state={"iv_index": 0, "iv_update": False, "seq": 0, "replay": {}, **state},
        on_persist=saved.append,
        persist_is_durable=durable,
    )
    return net, saved


def beacon(iv_index, iv_update=False):
    return SecureBeacon(key_refresh=False, iv_update=iv_update, iv_index=iv_index)


def test_a_confirmed_iv_jump_is_adopted_with_seq_from_zero():
    net, saved = network(iv_index=2, seq=500)
    assert net.apply_beacon(beacon(9)) is BeaconOutcome.TOO_FAR
    assert net.iv_index == 2

    net.accept_iv_jump(beacon(9))

    assert (net.iv_index, net.iv_update) == (9, False)
    assert (saved[-1]["iv_index"], saved[-1]["seq"]) == (9, 64)
    assert net.next_seq() == 0


def test_with_an_async_store_the_jump_waits_for_its_save():
    net, saved = network(durable=False, iv_index=2, seq=500)
    net.accept_iv_jump(beacon(9))
    with pytest.raises(SeqNotSaved):
        net.next_seq()

    net.mark_persisted(saved[-1])

    assert net.next_seq() == 0


def test_a_stale_pre_jump_confirmation_does_not_unlock_seqs_after_the_jump():
    net, saved = network(durable=False, iv_index=2, seq=500)
    net.prime()
    stale = saved[-1]  # (2, Normal, ceiling 564): still in flight when the user confirms
    net.accept_iv_jump(beacon(9))

    net.mark_persisted(stale)  # a higher ceiling, but at the old transmit IV index

    with pytest.raises(SeqNotSaved):
        net.next_seq()
    net.mark_persisted(saved[-1])
    assert net.next_seq() == 0


def test_a_jump_to_an_in_progress_target_transmits_with_the_previous_iv_index():
    net, saved = network(iv_index=2, seq=500)

    net.accept_iv_jump(beacon(9, iv_update=True))

    assert net.tx_iv_index == 8
    assert (saved[-1]["iv_index"], saved[-1]["iv_update"], saved[-1]["seq"]) == (9, True, 64)
    assert net.next_seq() == 0


def test_one_confirmed_save_covers_the_first_sends_after_an_iv_update():
    net, saved = network(durable=False, iv_index=3, seq=900)
    assert net.apply_beacon(beacon(4)) is BeaconOutcome.UPDATED
    assert (saved[-1]["iv_index"], saved[-1]["seq"]) == (4, 64)
    writes = len(saved)

    net.mark_persisted(saved[-1])

    assert net.next_seq() == 0
    assert len(saved) == writes  # no second save before the first send


def test_an_iv_jump_never_goes_backwards():
    net, saved = network(iv_index=5, seq=40)
    for iv in (4, 5):
        with pytest.raises(ValueError):
            net.accept_iv_jump(beacon(iv))
    assert net.iv_index == 5
    assert saved == []


def test_an_iv_jump_outside_the_iv_index_range_is_refused():
    net, saved = network(iv_index=5, seq=40)
    for iv in (-1, 0x1_0000_0000):
        with pytest.raises(ValueError):
            net.accept_iv_jump(beacon(iv))
    assert net.iv_index == 5
    assert saved == []


def test_an_iv_jump_must_raise_the_transmit_iv_index():
    net, saved = network(iv_index=5, seq=500)
    before = net.persistent_state()
    # IV 6 In Progress still transmits with IV 5, so restarting seq would reuse (5, seq).
    with pytest.raises(ValueError):
        net.accept_iv_jump(beacon(6, iv_update=True))

    assert (net.iv_index, net.iv_update, net.tx_iv_index) == (5, False, 5)
    assert net.persistent_state() == before  # seq ceiling unchanged
    assert saved == []
    assert net.next_seq() == 500


def test_replay_drops_are_counted_and_logged_per_source(caplog):
    net, _ = network()
    caplog.set_level(logging.DEBUG, logger="mesh.network")
    assert net.accept_replay(0x00B0, 10, 0)
    assert not net.accept_replay(0x00B0, 10, 0)
    assert not net.accept_replay(0x00B0, 3, 0)  # a power-cycled node restarting its seq
    assert net.accept_replay(0x00C0, 1, 0)

    assert net.replay_drops == {0x00B0: 2}
    assert "Replay drop from 0x00B0" in caplog.text


def test_provisioning_error_survives_pickling():
    err = pickle.loads(pickle.dumps(ProvisioningError("confirmation", "values differ")))

    assert err.step == "confirmation"
    assert str(err) == "confirmation: values differ"


@pytest.mark.parametrize(
    ("low", "high"),
    [(0x0000, 0x7FFF), (0x0010, 0x000F), (0x0001, 0x8000)],
)
def test_allocate_unicast_rejects_a_bad_range(low, high):
    with pytest.raises(ValueError):
        allocate_unicast([], 1, low=low, high=high)


def test_allocate_unicast_still_reports_a_full_range():
    with pytest.raises(MeshError):
        allocate_unicast([(0x7F00, 0x100)], 1, low=0x7F00, high=0x7FFF)
