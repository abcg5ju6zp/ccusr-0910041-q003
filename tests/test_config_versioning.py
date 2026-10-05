import logging
import re
import threading

from unittest.mock import Mock

import pytest

from sanic import Sanic
from sanic.config import (
    SENSITIVE_MASK,
    Config,
    ConfigCandidate,
    ConfigSnapshot,
)
from sanic.exceptions import (
    ConfigConflictError,
    ConfigStateError,
    ConfigValidationError,
)
from sanic.response import json


def test_initial_version():
    config = Config()
    assert config.version == 0
    assert config.versions == (0,)
    assert config.history == ()


def test_stage_and_activate():
    config = Config()
    candidate = config.stage({"FOO": "bar"}, BAZ=2)
    assert isinstance(candidate, ConfigCandidate)
    assert candidate.base_version == 0
    assert candidate.version is None
    assert not candidate.activated

    change = candidate.activate()
    assert config.version == 1
    assert config.FOO == "bar"
    assert config.BAZ == 2
    assert candidate.version == 1
    assert candidate.activated
    assert change.version == 1
    assert change.previous_version == 0
    assert change.added == {"FOO": "bar", "BAZ": 2}
    assert change.removed == {}
    assert change.updated == {}
    assert not change.is_rollback
    assert set(change.changed_keys) == {"FOO", "BAZ"}


def test_direct_mutation_does_not_change_version():
    config = Config()
    config.DIRECT = 1
    assert config.version == 0
    assert config.DIRECT == 1


def test_activation_applies_batch_atomically():
    config = Config()
    observed = []
    config.add_change_listener(
        lambda change: observed.append((config.A, config.B, config.C))
    )
    config.stage({"A": 1, "B": 2, "C": 3}).activate()
    # 监听器被调用时，整批变更已一次性全部生效
    assert observed == [(1, 2, 3)]


def test_empty_candidate_still_bumps_version():
    config = Config()
    change = config.stage().activate()
    assert change.version == 1
    assert not change
    assert change.changed_keys == ()


def test_stage_rejects_overlapping_update_and_remove():
    config = Config()
    with pytest.raises(ValueError, match="both updated and removed"):
        config.stage({"A": 1}, remove=["A"])


def test_stage_remove_keys():
    config = Config()
    config.stage({"TEMP": 1, "KEEP": 2}).activate()
    change = config.stage(remove=["TEMP", "NEVER_EXISTED"]).activate()
    assert "TEMP" not in config
    assert config.KEEP == 2
    assert change.removed == {"TEMP": 1}


def test_failed_validation_leaves_config_unchanged():
    config = Config()
    config.register_validator(
        lambda proposed: (
            "PORT must be positive" if proposed.get("PORT", 1) <= 0 else None
        )
    )
    listener = Mock()
    config.add_change_listener(listener)

    candidate = config.stage({"PORT": -1})
    with pytest.raises(ConfigValidationError) as excinfo:
        candidate.activate()

    assert excinfo.value.errors == ("PORT must be positive",)
    assert config.version == 0
    assert "PORT" not in config
    listener.assert_not_called()
    assert not candidate.activated
    assert candidate.version is None


def test_candidate_can_retry_after_validation_failure():
    config = Config()
    state = {"allow": False}

    def validator(proposed):
        if not state["allow"]:
            return "not allowed yet"

    config.register_validator(validator)
    candidate = config.stage({"A": 1})
    with pytest.raises(ConfigValidationError):
        candidate.activate()

    state["allow"] = True
    change = candidate.activate()
    assert change.version == 1
    assert config.A == 1


def test_validation_collects_all_errors():
    config = Config()

    def raising_validator(proposed):
        raise ConfigValidationError(["first", "second"])

    config.register_validator(raising_validator)
    config.register_validator(lambda proposed: "third")
    config.register_validator(lambda proposed: None)

    with pytest.raises(ConfigValidationError) as excinfo:
        config.stage({"A": 1}).activate()
    assert excinfo.value.errors == ("first", "second", "third")


def test_validator_receives_merged_proposed():
    config = Config()
    config.stage({"EXISTING": 1}).activate()
    seen = {}

    def validator(proposed):
        seen["existing"] = proposed["EXISTING"]
        seen["new"] = proposed["NEW"]

    config.register_validator(validator)
    config.stage({"NEW": 2}).activate()
    assert seen == {"existing": 1, "new": 2}


def test_validator_cannot_mutate_proposed():
    config = Config()

    def validator(proposed):
        with pytest.raises(TypeError):
            proposed["HACK"] = 1

    config.register_validator(validator)
    config.stage({"A": 1}).activate()
    assert "HACK" not in config


def test_candidate_validate_dry_run():
    config = Config()
    config.register_validator(
        lambda proposed: "bad" if proposed.get("X") else None
    )
    bad = config.stage({"X": 1})
    good = config.stage({"Y": 2})
    assert bad.validate() == ["bad"]
    assert good.validate() == []
    assert config.version == 0


def test_register_validator_twice_warns(caplog):
    config = Config()

    def validator(proposed): ...

    config.register_validator(validator)
    with caplog.at_level(logging.WARNING):
        config.register_validator(validator)
    assert len(config._validators) == 1
    assert any(
        "has already been registered" in record.message
        for record in caplog.records
    )


def test_stale_candidate_rejected():
    config = Config()
    first = config.stage({"A": 1})
    second = config.stage({"B": 2})

    first.activate()
    with pytest.raises(ConfigConflictError, match="based on version 0"):
        second.activate()

    assert "B" not in config
    assert config.version == 1

    change = config.stage({"B": 2}).activate()
    assert change.version == 2
    assert config.B == 2


def test_repeated_activation_raises():
    config = Config()
    candidate = config.stage({"A": 1})
    candidate.activate()
    with pytest.raises(ConfigStateError, match="already been activated"):
        candidate.activate()
    assert config.version == 1


def test_concurrent_publishers_single_winner():
    config = Config()
    barrier = threading.Barrier(5)
    activated = []
    conflicts = []

    def publish(i):
        candidate = config.stage({f"KEY_{i}": i})
        barrier.wait(timeout=5)
        try:
            candidate.activate()
            activated.append(i)
        except ConfigConflictError:
            conflicts.append(i)

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(activated) == 1
    assert len(conflicts) == 4
    assert config.version == 1


def test_concurrent_publishers_with_retry():
    config = Config()
    barrier = threading.Barrier(8)

    def publish(i):
        barrier.wait(timeout=5)
        for _ in range(50):
            try:
                config.stage({f"KEY_{i}": i}).activate()
                return
            except ConfigConflictError:
                continue
        raise AssertionError("publisher could not activate")

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert config.version == 8
    for i in range(8):
        assert config[f"KEY_{i}"] == i


def test_rollback_to_previous_version():
    config = Config()
    config.stage({"A": 1}).activate()
    config.stage({"A": 2, "B": 3}).activate()

    change = config.rollback()
    assert config.version == 3
    assert change.rollback_of == 1
    assert change.is_rollback
    assert config.A == 1
    assert "B" not in config
    assert change.removed == {"B": 3}
    assert change.updated == {"A": (2, 1)}


def test_rollback_to_specific_and_initial_version():
    config = Config()
    config.stage({"A": 1}).activate()
    config.stage({"B": 2}).activate()

    change = config.rollback(0)
    assert config.version == 3
    assert change.rollback_of == 0
    assert "A" not in config
    assert "B" not in config


def test_rollback_unknown_or_current_version():
    config = Config()
    config.stage({"A": 1}).activate()

    with pytest.raises(ConfigStateError, match="Unknown config version"):
        config.rollback(99)
    with pytest.raises(ConfigStateError, match="already at version"):
        config.rollback(config.version)
    assert config.version == 1


def test_rollback_without_previous_version():
    config = Config()
    with pytest.raises(ConfigStateError, match="No previous config version"):
        config.rollback()


def test_rollback_notifies_listeners():
    config = Config()
    changes = []
    config.add_change_listener(changes.append)
    config.stage({"A": 1}).activate()
    config.rollback()
    assert len(changes) == 2
    assert changes[1].rollback_of == 0
    assert changes[1].is_rollback


def test_history_is_bounded():
    config = Config(history_size=3)
    for i in range(5):
        config.stage({"N": i}).activate()

    assert config.versions == (3, 4, 5)
    assert len(config.history) == 3
    with pytest.raises(ConfigStateError, match="Unknown config version"):
        config.rollback(2)

    change = config.rollback()
    assert change.rollback_of == 4


def test_history_size_must_be_positive():
    with pytest.raises(ValueError, match="history_size"):
        Config(history_size=0)


def test_listener_notified_once_per_activation():
    config = Config()
    changes = []
    config.add_change_listener(changes.append)
    config.stage({"A": 1}).activate()
    config.stage({"B": 2}).activate()
    assert len(changes) == 2
    assert [change.version for change in changes] == [1, 2]


def test_listener_observes_committed_state():
    config = Config()
    seen = []
    config.add_change_listener(
        lambda change: seen.append((change.version, config.version, config.A))
    )
    config.stage({"A": 1}).activate()
    assert seen == [(1, 1, 1)]


def test_listener_exception_does_not_break_activation(caplog):
    config = Config()
    calls = []

    def bad_listener(change):
        raise RuntimeError("boom")

    config.add_change_listener(bad_listener)
    config.add_change_listener(lambda change: calls.append(change.version))

    with caplog.at_level(logging.ERROR, logger="sanic.error"):
        config.stage({"A": 1}).activate()

    assert config.version == 1
    assert calls == [1]
    assert any(
        "Config change listener" in record.message for record in caplog.records
    )


def test_add_change_listener_twice_warns(caplog):
    config = Config()

    def listener(change): ...

    config.add_change_listener(listener)
    with caplog.at_level(logging.WARNING):
        config.add_change_listener(listener)
    assert len(config._change_listeners) == 1
    assert any(
        "has already been registered" in record.message
        for record in caplog.records
    )


def test_remove_change_listener():
    config = Config()
    calls = []

    def listener(change):
        calls.append(change.version)

    config.add_change_listener(listener)
    config.remove_change_listener(listener)
    config.stage({"A": 1}).activate()
    assert calls == []


def test_sensitive_values_masked_in_change():
    config = Config()
    change = config.stage(
        {
            "DATABASE_PASSWORD": "s3cr3t",
            "API_TOKEN": "tok-123",
            "PLAIN_SETTING": "visible",
        }
    ).activate()

    assert change.added["DATABASE_PASSWORD"] == SENSITIVE_MASK
    assert change.added["API_TOKEN"] == SENSITIVE_MASK
    assert change.added["PLAIN_SETTING"] == "visible"
    # 实际配置值不受遮蔽影响
    assert config.DATABASE_PASSWORD == "s3cr3t"
    assert "s3cr3t" not in repr(change)
    assert "tok-123" not in repr(change)


def test_sensitive_updated_and_removed_masked():
    config = Config()
    config.stage({"MY_SECRET": "one", "KEEP_ME": 1}).activate()

    change = config.stage({"MY_SECRET": "two"}).activate()
    assert change.updated["MY_SECRET"] == (SENSITIVE_MASK, SENSITIVE_MASK)

    change = config.rollback(0)
    assert change.removed["MY_SECRET"] == SENSITIVE_MASK
    assert change.removed["KEEP_ME"] == 1


def test_mark_sensitive_custom_keys_and_patterns():
    config = Config()
    config.mark_sensitive("innocent_name", re.compile(r"^INTERNAL_"))
    change = config.stage(
        {"INNOCENT_NAME": "x", "INTERNAL_ID": "y", "OTHER": "z"}
    ).activate()
    assert change.added["INNOCENT_NAME"] == SENSITIVE_MASK
    assert change.added["INTERNAL_ID"] == SENSITIVE_MASK
    assert change.added["OTHER"] == "z"


def test_mark_sensitive_rejects_invalid_type():
    config = Config()
    with pytest.raises(TypeError, match="str or re.Pattern"):
        config.mark_sensitive(123)


def test_snapshot_is_stable_across_activations():
    config = Config()
    config.stage({"A": 1}).activate()

    snapshot = config.snapshot()
    assert isinstance(snapshot, ConfigSnapshot)
    assert snapshot.version == 1
    assert snapshot["A"] == 1
    assert snapshot.A == 1

    config.stage({"A": 2, "B": 3}).activate()
    assert snapshot.version == 1
    assert snapshot["A"] == 1
    assert "B" not in snapshot
    assert config.A == 2


def test_snapshot_mapping_interface():
    config = Config()
    config.stage({"A": 1}).activate()
    snapshot = config.snapshot()

    assert "A" in snapshot
    assert "A" in dict(snapshot)
    assert snapshot.get("MISSING") is None
    assert len(snapshot) == len(config)
    with pytest.raises(AttributeError, match="Config has no"):
        snapshot.DOES_NOT_EXIST


def test_bound_context_manager():
    config = Config()
    config.stage({"A": 1}).activate()

    with config.bound() as bound:
        config.stage({"A": 2}).activate()
        assert bound.A == 1
        assert bound.version == 1
    assert config.A == 2


def test_persistence_recovers_version_after_restart(tmp_path):
    state = tmp_path / "config.state"
    config = Config(state_path=state)
    config.stage({"A": 1, "SERVICE_TOKEN": "t"}).activate()
    config.stage({"A": 2}).activate()

    recovered = Config(state_path=state)
    assert recovered.version == 2
    assert recovered.A == 2
    assert recovered.SERVICE_TOKEN == "t"
    assert recovered.versions == (0, 1, 2)
    assert [change.version for change in recovered.history] == [1, 2]

    change = recovered.stage({"B": 3}).activate()
    assert change.version == 3

    change = recovered.rollback()
    assert change.rollback_of == 2
    assert recovered.A == 2
    assert "B" not in recovered


def test_persisted_history_keeps_sensitive_values_masked(tmp_path):
    state = tmp_path / "config.state"
    config = Config(state_path=state)
    config.stage({"APP_SECRET": "shhh"}).activate()

    recovered = Config(state_path=state)
    (change,) = recovered.history
    assert change.added["APP_SECRET"] == SENSITIVE_MASK
    assert "shhh" not in repr(change)
    assert recovered.APP_SECRET == "shhh"


def test_corrupt_state_file_starts_fresh(tmp_path, caplog):
    state = tmp_path / "config.state"
    state.write_bytes(b"not a pickle")

    with caplog.at_level(logging.ERROR, logger="sanic.error"):
        config = Config(state_path=state)

    assert config.version == 0
    assert any(
        "Could not load config state" in record.message
        for record in caplog.records
    )


def test_unpersistable_value_aborts_activation(tmp_path):
    state = tmp_path / "config.state"
    config = Config(state_path=state)

    with pytest.raises(ConfigStateError, match="Failed to persist"):
        config.stage({"FN": lambda: None}).activate()

    assert config.version == 0
    assert "FN" not in config


def test_enable_persistence_after_init(tmp_path):
    state = tmp_path / "config.state"
    config = Config()
    config.stage({"A": 1}).activate()
    config.enable_persistence(state)

    recovered = Config(state_path=state)
    assert recovered.version == 1
    assert recovered.A == 1


def test_bound_snapshot_during_request(app: Sanic):
    app.config.stage({"MODE": "one"}).activate()

    @app.get("/")
    async def handler(request):
        with app.config.bound() as bound:
            app.config.stage({"MODE": "two"}).activate()
            return json({"mode": bound.MODE, "version": bound.version})

    _, response = app.test_client.get("/")
    assert response.json == {"mode": "one", "version": 1}
    assert app.config.MODE == "two"
    assert app.config.version == 2
