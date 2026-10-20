import threading
import time

from flypoints.cache import Cache, make_key


def test_roundtrip_and_missing(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    assert c.get_json("ns", "k") is None
    c.put_json("ns", "k", {"a": [1, 2]}, 60)
    assert c.get_json("ns", "k") == {"a": [1, 2]}
    assert c.get_json("other", "k") is None
    c.put_json("ns", "k", 5, 60)  # overwrite
    assert c.get_json("ns", "k") == 5


def test_expiry_and_purge(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    c.put_json("ns", "old", 1, 0.05)
    c.put_json("ns", "new", 2, 60)
    time.sleep(0.1)
    assert c.get_json("ns", "old") is None
    assert c.purge_expired() == 1
    assert c.get_json("ns", "new") == 2


def test_persists_across_instances(tmp_path):
    Cache(tmp_path / "c.sqlite").put_json("ns", "k", "v", 60)
    assert Cache(tmp_path / "c.sqlite").get_json("ns", "k") == "v"


def test_make_key_stable():
    assert make_key("SJU", {"b": 1, "a": 2}) == make_key("SJU", {"a": 2, "b": 1})
    assert make_key("SJU", "MAD") != make_key("MAD", "SJU")


def test_thread_safe(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    errors = []

    def work(i):
        try:
            for j in range(20):
                c.put_json("ns", f"{i}-{j}", j, 60)
                assert c.get_json("ns", f"{i}-{j}") == j
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
