import json
import random

import pytest

from mesh.errors import MeshError, SeqNotSaved
from mesh.network import RESERVE_MARGIN, SEQ_BLOCK, SEQ_MAX, BeaconOutcome, MeshNetwork
from mesh.pdu import SecureBeacon

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
    for _ in range(32):  # seqs 1..32 stay more than RESERVE_MARGIN below the ceiling
        net.next_seq()
    assert len(saved) == 1
    assert net.next_seq() == 33  # within RESERVE_MARGIN of the ceiling: the next block is saved
    assert len(saved) == 2
    assert saved[-1]["seq"] == 129


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


def beacon(iv_index, iv_update=False):
    return SecureBeacon(key_refresh=False, iv_update=iv_update, iv_index=iv_index)


def test_rx_iv_index_follows_ivi():
    net = make(state={"iv_index": 5})
    assert (net.rx_iv_index(1), net.rx_iv_index(0)) == (5, 4)
    fresh = make()
    assert (fresh.rx_iv_index(0), fresh.rx_iv_index(1)) == (0, None)


def test_tx_iv_index():
    assert make(state={"iv_index": 5}).tx_iv_index == 5
    assert make(state={"iv_index": 5, "iv_update": True}).tx_iv_index == 4
    assert make(state={"iv_index": 0, "iv_update": True}).tx_iv_index == 0


def test_receive_during_iv_update_accepts_old_and_new():
    net = make(state={"iv_index": 6, "iv_update": True})
    assert (net.rx_iv_index(0), net.rx_iv_index(1)) == (6, 5)


def test_beacon_starts_iv_update_and_keeps_seq():
    saved = []
    net = make(saved=saved)
    used = net.next_seq()
    assert net.apply_beacon(beacon(1, iv_update=True)) is BeaconOutcome.UPDATED
    assert (net.iv_index, net.iv_update, net.tx_iv_index) == (1, True, 0)
    assert net.next_seq() > used  # still sending with IV index 0
    assert saved[-1]["iv_index"] == 1
    assert saved[-1]["iv_update"] is True


def test_beacon_ends_iv_update_and_resets_seq():
    saved = []
    net = make(state={"iv_index": 1, "iv_update": True, "seq": 500}, saved=saved)
    assert net.apply_beacon(beacon(1)) is BeaconOutcome.UPDATED
    assert (net.iv_update, net.tx_iv_index) == (False, 1)
    assert net.next_seq() == 0
    assert saved[0]["iv_update"] is False


def test_beacon_one_step_ahead_in_normal_state_resets_seq():
    net = make(state={"iv_index": 1, "seq": 500})
    assert net.apply_beacon(beacon(2)) is BeaconOutcome.UPDATED
    assert (net.iv_index, net.iv_update) == (2, False)
    assert net.next_seq() == 0


def test_beacon_same_iv_with_update_flag_is_ignored():
    saved = []
    net = make(state={"iv_index": 1, "seq": 500}, saved=saved)
    assert net.apply_beacon(beacon(1, iv_update=True)) is BeaconOutcome.UNCHANGED
    assert (net.iv_index, net.iv_update, net.tx_iv_index) == (1, False, 1)
    assert saved == []
    assert net.next_seq() == 500


def test_flapping_beacons_never_reuse_a_seq():
    net = make(state={"iv_index": 1, "iv_update": True, "seq": 500})
    assert net.apply_beacon(beacon(1)) is BeaconOutcome.UPDATED
    assert [net.next_seq() for _ in range(10)] == list(range(10))
    assert net.apply_beacon(beacon(1, iv_update=True)) is BeaconOutcome.UNCHANGED
    assert net.apply_beacon(beacon(1)) is BeaconOutcome.UNCHANGED
    assert net.next_seq() == 10
    assert net.tx_iv_index == 1


def test_beacon_at_iv_zero_with_update_flag_is_ignored():
    net = make()
    assert net.next_seq() == 0
    assert net.apply_beacon(beacon(0, iv_update=True)) is BeaconOutcome.UNCHANGED
    assert (net.iv_index, net.iv_update) == (0, False)
    assert net.next_seq() == 1


def test_ending_update_at_iv_zero_keeps_seq():
    net = make(state={"iv_index": 0, "iv_update": True, "seq": 500})
    assert net.apply_beacon(beacon(0)) is BeaconOutcome.UPDATED
    assert (net.iv_index, net.iv_update) == (0, False)
    assert net.next_seq() == 500  # the transmit IV index stayed 0


def test_update_to_next_iv_while_in_progress_resets_seq():
    net = make(state={"iv_index": 1, "iv_update": True, "seq": 500})
    assert net.apply_beacon(beacon(2, iv_update=True)) is BeaconOutcome.UPDATED
    assert (net.iv_index, net.iv_update, net.tx_iv_index) == (2, True, 1)
    assert net.next_seq() == 0  # IV index 1 was never used for sending


def test_beacon_with_nothing_new_is_unchanged():
    saved = []
    net = make(state={"iv_index": 1}, saved=saved)
    assert net.apply_beacon(beacon(1)) is BeaconOutcome.UNCHANGED
    assert net.apply_beacon(beacon(0)) is BeaconOutcome.UNCHANGED
    assert net.apply_beacon(beacon(0, iv_update=True)) is BeaconOutcome.UNCHANGED
    assert saved == []


def test_beacon_jump_too_far_is_not_applied():
    saved = []
    net = make(state={"iv_index": 1}, saved=saved)
    assert net.apply_beacon(beacon(3)) is BeaconOutcome.TOO_FAR
    assert (net.iv_index, net.iv_update) == (1, False)
    assert saved == []


@pytest.mark.parametrize("count", [0, -1])
def test_next_seq_rejects_non_positive_count(count):
    net = make(state={"seq": 500})
    with pytest.raises(ValueError):
        net.next_seq(count)
    assert net.next_seq() == 500


@pytest.mark.parametrize(
    "state",
    [
        {"iv_index": -1},
        {"iv_index": 0x1_0000_0000},
        {"seq": -1},
        {"seq": SEQ_MAX + 2},
        {"replay": {"176": [-1, 0]}},
        {"replay": {"176": [0x1_0000_0000, 0]}},
        {"replay": {"176": [0, -1]}},
        {"replay": {"176": [0, SEQ_MAX + 1]}},
    ],
)
def test_constructor_rejects_invalid_state(state):
    with pytest.raises(ValueError):
        make(state=state)


def test_state_survives_json_round_trip():
    net = make(state={"iv_index": 3, "iv_update": True})
    for _ in range(10):
        net.next_seq()
    net.accept_replay(0x00B0, 7, 2)
    net.accept_replay(0x00C0, 9, 3)
    state = net.persistent_state()
    restored = make(state=json.loads(json.dumps(state)))
    assert (restored.iv_index, restored.iv_update) == (3, True)
    assert not restored.accept_replay(0x00B0, 7, 2)
    assert restored.accept_replay(0x00B0, 8, 2)
    assert not restored.accept_replay(0x00C0, 9, 3)
    assert restored.next_seq() >= state["seq"]


def test_restart_after_iv_reset_resumes_safely():
    saved = []
    net = make(state={"iv_index": 1, "iv_update": True, "seq": 500}, saved=saved)
    assert net.apply_beacon(beacon(1)) is BeaconOutcome.UPDATED
    assert saved[-1]["seq"] == 0
    resaved = []
    restarted = make(state=saved[-1], saved=resaved)
    seq = restarted.next_seq()
    assert seq >= 0
    assert len(resaved) == 1  # a new ceiling was persisted before the seq was handed out
    assert resaved[0]["seq"] > seq
    assert (resaved[0]["iv_index"], resaved[0]["iv_update"]) == (1, False)


class DeferredStore:
    """An async store like Home Assistant's: on_persist only queues a write.

    The test decides when a queued write lands (``confirm``), is lost (``drop``) or when
    the process crashes (``restart``): only the last confirmed state survives a crash.
    """

    def __init__(self, disk=None):
        self.disk = disk
        self.queue = []
        self.net = None

    def start(self):
        self.queue = []  # pending writes die with the process
        self.net = MeshNetwork(
            net_key=NET_KEY,
            app_key=APP_KEY,
            address=0x0001,
            state=self.disk,
            on_persist=self.queue.append,
            persist_is_durable=False,
        )
        return self.net

    def confirm(self):
        """Every queued write lands, in order."""
        while self.queue:
            self.confirm_oldest()

    def confirm_oldest(self):
        self.disk = self.queue.pop(0)
        self.net.mark_persisted(self.disk)

    def drop_oldest(self):
        self.queue.pop(0)


def test_deferred_save_refuses_seqs_beyond_the_saved_ceiling():
    store = DeferredStore()
    net = store.start()
    net.prime()
    store.confirm()
    assert store.disk["seq"] == SEQ_BLOCK
    used = [net.next_seq() for _ in range(SEQ_BLOCK - RESERVE_MARGIN + 1)]
    assert [state["seq"] for state in store.queue] == [2 * SEQ_BLOCK]  # reserved ahead
    used += [net.next_seq() for _ in range(SEQ_BLOCK - len(used))]  # still covered by the disk
    assert used == list(range(SEQ_BLOCK))
    with pytest.raises(SeqNotSaved):
        net.next_seq()
    restarted = store.start()  # crash before the reservation was written
    restarted.prime()
    store.confirm()
    assert restarted.next_seq() >= max(used) + 1


def test_deferred_save_after_iv_reset_refuses_seqs_until_confirmed():
    store = DeferredStore({"iv_index": 5, "iv_update": True, "seq": 1000})
    net = store.start()
    assert net.tx_iv_index == 4
    assert net.apply_beacon(beacon(5)) is BeaconOutcome.UPDATED  # tx 4 -> 5 resets seq
    assert net.tx_iv_index == 5
    with pytest.raises(SeqNotSaved):
        net.next_seq()
    store.confirm()
    used = [net.next_seq() for _ in range(3)]
    assert (net.tx_iv_index, used) == (5, [0, 1, 2])
    restarted = store.start()
    assert restarted.tx_iv_index == 5  # never back to 4
    restarted.prime()
    store.confirm()
    assert restarted.next_seq() > max(used)


def test_late_confirmation_of_an_older_state_does_not_lower_the_saved_ceiling():
    store = DeferredStore()
    net = store.start()
    net.prime()
    older = store.queue.pop()
    net.mark_persisted(older)  # ceiling 64
    for _ in range(SEQ_BLOCK - RESERVE_MARGIN + 1):
        net.next_seq()
    newer = store.queue.pop()
    assert newer["seq"] == 2 * SEQ_BLOCK
    net.mark_persisted(newer)
    net.mark_persisted(older)  # a late confirmation of the earlier write
    while net.next_seq() < 2 * SEQ_BLOCK - 1:
        pass  # every seq below the newer ceiling is still allowed
    with pytest.raises(SeqNotSaved):
        net.next_seq()


def test_prime_reserves_a_block_before_the_first_seq():
    store = DeferredStore()
    with pytest.raises(SeqNotSaved):
        store.start().next_seq()  # nothing saved yet
    net = store.start()
    net.prime()
    assert [state["seq"] for state in store.queue] == [SEQ_BLOCK]
    store.confirm()
    assert [net.next_seq() for _ in range(RESERVE_MARGIN)] == list(range(RESERVE_MARGIN))
    assert store.queue == []  # no further save was needed


def test_random_deferred_saves_never_reuse_a_nonce():
    rnd = random.Random(20261015)
    for _ in range(500):
        store = DeferredStore(
            {
                "iv_index": rnd.randint(0, 3),
                "iv_update": rnd.random() < 0.5,
                "seq": rnd.randint(0, 200),
            }
        )
        net = store.start()
        handed_out = set()
        last_tx = 0
        for _ in range(80):
            action = rnd.random()
            if action < 0.45:
                count = rnd.randint(1, 5)
                tx = net.tx_iv_index
                try:
                    first = net.next_seq(count)
                except SeqNotSaved:
                    continue
                for seq in range(first, first + count):
                    assert (tx, seq) not in handed_out
                    handed_out.add((tx, seq))
                assert tx >= last_tx
                last_tx = tx
            elif action < 0.55:
                iv_index = max(net.iv_index + rnd.choice((-1, 0, 0, 1, 1, 2)), 0)
                net.apply_beacon(beacon(iv_index, iv_update=rnd.random() < 0.5))
            elif action < 0.75:
                if store.queue:
                    store.confirm_oldest()
            elif action < 0.83:
                if store.queue:
                    store.drop_oldest()
            elif action < 0.9:
                net.prime()
            else:
                net = store.start()  # crash: only the last confirmed state survives
