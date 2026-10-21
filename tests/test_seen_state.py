import json
from datetime import datetime, timedelta, timezone

from poll_discord import SeenState, load_state, prune_state, save_state

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def test_missing_or_corrupt_state_is_empty(tmp_path):
    path = tmp_path / "seen.json"
    assert load_state(path, NOW) == SeenState()
    path.write_text("{not json")
    assert load_state(path, NOW) == SeenState()


def test_loads_legacy_list_format_with_now_timestamp(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"seen": ["aaa", "bbb"]}))
    state = load_state(path, NOW)
    assert state.jobs == {"aaa": NOW.isoformat(), "bbb": NOW.isoformat()}
    assert state.linkedin_cards == {}


def test_new_format_round_trips(tmp_path):
    path = tmp_path / "seen.json"
    state = SeenState(
        jobs={"aaa": "2026-10-01T00:00:00+00:00"},
        linkedin_cards={"4012345678": "2026-10-21T11:00:00+00:00"},
    )
    save_state(state, path)
    data = json.loads(path.read_text())
    assert data["version"] == 2
    assert isinstance(data["seen"], dict)
    assert load_state(path, NOW) == state


def test_prune_drops_old_jobs_and_cards():
    state = SeenState(
        jobs={
            "fresh": (NOW - timedelta(days=29)).isoformat(),
            "stale": (NOW - timedelta(days=31)).isoformat(),
            "garbage": "not-a-date",
        },
        linkedin_cards={
            "1": (NOW - timedelta(hours=47)).isoformat(),
            "2": (NOW - timedelta(hours=49)).isoformat(),
        },
    )
    pruned = prune_state(
        state, NOW, seen_ttl=timedelta(days=30), card_ttl=timedelta(hours=48)
    )
    assert set(pruned.jobs) == {"fresh"}
    assert set(pruned.linkedin_cards) == {"1"}


def test_prune_keeps_everything_past_old_5000_cap():
    stamp = NOW.isoformat()
    state = SeenState(jobs={f"{i:064x}": stamp for i in range(6000)})
    assert len(prune_state(state, NOW).jobs) == 6000
