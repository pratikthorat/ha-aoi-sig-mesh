import pytest

from mesh.errors import MeshError
from mesh.network import SEQ_MAX, MeshNetwork

NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")


def make(state=None, saved=None):
    return MeshNetwork(
        net_key=NET_KEY,
        app_key=APP_KEY,
        address=0x0001,
        state=state,
        on_persist=saved.append if saved is not None else None,
    )


def test_keys_and_aid():
    net = make()
    assert net.keys.nid == 0x68
    assert net.aid == 0x26
    assert (net.address, net.net_key_index, net.app_key_index) == (0x0001, 0, 0)


def test_repr_hides_keys():
    text = repr(make())
    assert NET_KEY.hex() not in text
    assert APP_KEY.hex() not in text
    assert "0x0001" in text


def test_constructor_validates():
    with pytest.raises(ValueError):
        MeshNetwork(net_key=NET_KEY[:15], app_key=APP_KEY, address=1)
    with pytest.raises(ValueError):
        MeshNetwork(net_key=NET_KEY, app_key=APP_KEY[:15], address=1)
    for address in (0x0000, 0x8000):
        with pytest.raises(ValueError):
            MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=address)


def test_seq_counts_up():
    net = make()
    assert net.next_seq() == 0
    assert net.next_seq() == 1
    assert net.next_seq(3) == 2  # reserves 2, 3, 4
    assert net.next_seq() == 5


def test_seq_block_is_persisted_before_use():
    saved = []
    net = make(saved=saved)
    net.next_seq()
    assert saved == [{"iv_index": 0, "iv_update": False, "seq": 65, "replay": {}}]
    for _ in range(64):  # seqs 1..64 fit in the reserved block
        net.next_seq()
    assert len(saved) == 1
    assert net.next_seq() == 65
    assert saved[-1]["seq"] == 130


def test_failed_persist_hands_out_no_seq():
    calls = []

    def flaky(state):
        calls.append(state)
        if len(calls) == 1:
            raise OSError("disk full")

    net = MeshNetwork(net_key=NET_KEY, app_key=APP_KEY, address=1, on_persist=flaky)
    with pytest.raises(OSError):
        net.next_seq()
    assert net.next_seq() == 0
    assert calls[-1]["seq"] == 65


def test_restart_never_reuses_seq():
    saved = []
    net = make(saved=saved)
    used = [net.next_seq() for _ in range(100)]
    restarted = make(state=saved[-1])  # crash: only the last persisted state survives
    assert restarted.next_seq() > max(used)


def test_seq_exhaustion_raises():
    net = make(state={"seq": SEQ_MAX})
    assert net.next_seq() == SEQ_MAX
    with pytest.raises(MeshError):
        net.next_seq()
    with pytest.raises(MeshError):
        make(state={"seq": SEQ_MAX}).next_seq(2)


def test_replay_accepts_only_increasing_seq():
    net = make()
    assert net.accept_replay(0x00B0, 5, 0)
    assert not net.accept_replay(0x00B0, 5, 0)
    assert not net.accept_replay(0x00B0, 4, 0)
    assert net.accept_replay(0x00B0, 6, 0)


def test_replay_newer_iv_index_lowers_the_seq_floor():
    net = make()
    assert net.accept_replay(0x00B0, 1000, 0)
    assert net.accept_replay(0x00B0, 1, 1)
    assert not net.accept_replay(0x00B0, 2000, 0)  # old IV index


def test_replay_is_per_source():
    net = make()
    assert net.accept_replay(0x00B0, 5, 0)
    assert net.accept_replay(0x00C0, 5, 0)


def test_forget_replay_after_reprovision():
    net = make()
    for src in (0x00B0, 0x00B1, 0x00C0):
        net.accept_replay(src, 500, 0)
    net.forget_replay(0x00B0, 2)  # node at 0x00B0 with two elements was reset
    assert net.accept_replay(0x00B0, 0, 0)
    assert net.accept_replay(0x00B1, 0, 0)
    assert not net.accept_replay(0x00C0, 0, 0)


def test_replay_cache_round_trips_through_state():
    saved = []
    net = make(saved=saved)
    net.accept_replay(0x00B0, 7, 0)
    assert saved == []  # replay updates alone do not write to disk
    state = net.persistent_state()
    assert state["replay"] == {"176": [0, 7]}
    assert not make(state=state).accept_replay(0x00B0, 7, 0)
