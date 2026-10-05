from __future__ import annotations

import pickle
import re
import time

from abc import ABC, ABCMeta, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from inspect import getmembers, isclass, isdatadescriptor
from os import environ
from os import replace as os_replace
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Iterator, Literal
from warnings import filterwarnings

from sanic.constants import LocalCertCreator
from sanic.errorpages import DEFAULT_FORMAT, check_error_format
from sanic.exceptions import (
    ConfigConflictError,
    ConfigStateError,
    ConfigValidationError,
)
from sanic.helpers import Default, _default
from sanic.http import Http
from sanic.log import error_logger
from sanic.utils import load_module_from_file_location, str_to_bool


FilterWarningType = (
    Literal["default"]
    | Literal["error"]
    | Literal["ignore"]
    | Literal["always"]
    | Literal["module"]
    | Literal["once"]
)

SANIC_PREFIX = "SANIC_"


DEFAULT_CONFIG = {
    "_FALLBACK_ERROR_FORMAT": _default,
    "ACCESS_LOG": False,
    "AUTO_EXTEND": True,
    "AUTO_RELOAD": False,
    "EVENT_AUTOREGISTER": False,
    "DEPRECATION_FILTER": "once",
    "FORWARDED_FOR_HEADER": "X-Forwarded-For",
    "FORWARDED_SECRET": None,  # nosec B105
    "GRACEFUL_SHUTDOWN_TIMEOUT": 15.0,
    "GRACEFUL_TCP_CLOSE_TIMEOUT": 5.0,
    "INSPECTOR": False,
    "INSPECTOR_HOST": "localhost",
    "INSPECTOR_PORT": 6457,
    "INSPECTOR_TLS_KEY": _default,
    "INSPECTOR_TLS_CERT": _default,
    "INSPECTOR_API_KEY": "",
    "KEEP_ALIVE_TIMEOUT": 120,
    "KEEP_ALIVE": True,
    "LOCAL_CERT_CREATOR": LocalCertCreator.AUTO,
    "LOCAL_TLS_KEY": _default,
    "LOCAL_TLS_CERT": _default,
    "LOCALHOST": "localhost",
    "LOG_EXTRA": _default,
    "MOTD": True,
    "MOTD_DISPLAY": {},
    "NO_COLOR": False,
    "NOISY_EXCEPTIONS": False,
    "PROXIES_COUNT": None,
    "REAL_IP_HEADER": None,
    "REQUEST_BUFFER_SIZE": 65536,
    "REQUEST_MAX_HEADER_SIZE": 8192,  # Cannot exceed 16384
    "REQUEST_ID_HEADER": "X-Request-ID",
    "REQUEST_MAX_SIZE": 100_000_000,
    "REQUEST_TIMEOUT": 60,
    "RESPONSE_TIMEOUT": 60,
    "TLS_CERT_PASSWORD": "",  # nosec B105
    "TOUCHUP": _default,
    "USE_UVLOOP": _default,
    "WEBSOCKET_MAX_SIZE": 2**20,  # 1 MiB
    "WEBSOCKET_PING_INTERVAL": 20,
    "WEBSOCKET_PING_TIMEOUT": 20,
}

SENSITIVE_MASK = "***"
DEFAULT_SENSITIVE_MARKERS = (
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "TOKEN",
    "CREDENTIAL",
    "API_KEY",
    "PRIVATE_KEY",
    "_KEY",
)


class ConfigChange:
    """一次配置激活（或回退）的变更结果。

    每次成功激活后，监听器收到的即为本对象。其中敏感配置项
    （如包含 SECRET、TOKEN、PASSWORD 等的键，或通过
    ``Config.mark_sensitive`` 登记的键）的值已被遮蔽，
    可以安全地写入日志或审计系统。
    """

    __slots__ = (
        "version",
        "previous_version",
        "added",
        "removed",
        "updated",
        "rollback_of",
        "activated_at",
    )

    def __init__(
        self,
        *,
        version: int,
        previous_version: int,
        added: dict[str, Any],
        removed: dict[str, Any],
        updated: dict[str, tuple[Any, Any]],
        rollback_of: int | None,
        activated_at: float,
    ):
        self.version = version
        self.previous_version = previous_version
        self.added = added
        self.removed = removed
        self.updated = updated
        self.rollback_of = rollback_of
        self.activated_at = activated_at

    @property
    def changed_keys(self) -> tuple[str, ...]:
        """本次变更涉及的键（新增、删除、修改的并集）。"""
        return tuple(sorted({*self.added, *self.removed, *self.updated}))

    @property
    def is_rollback(self) -> bool:
        """本次变更是否由回退产生。"""
        return self.rollback_of is not None

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.updated)

    def __repr__(self):
        return (
            f"ConfigChange(version={self.version}, "
            f"previous_version={self.previous_version}, "
            f"added={self.added}, removed={self.removed}, "
            f"updated={self.updated}, rollback_of={self.rollback_of})"
        )


class _VersionRecord:
    """单个已激活版本的完整快照及其变更结果。"""

    __slots__ = ("version", "values", "change")

    def __init__(
        self, version: int, values: dict[str, Any], change: ConfigChange | None
    ):
        self.version = version
        self.values = values
        self.change = change


class ConfigSnapshot(Mapping):
    """绑定到某一配置版本的不可变视图。

    在请求处理开始时通过 ``Config.snapshot()`` 或 ``Config.bound()``
    获取；此后即使其他发布者激活了新版本，本视图的内容与版本号
    保持不变，保证同一次请求处理期间读到一致的配置。
    """

    __slots__ = ("_version", "_values")

    def __init__(self, version: int, values: dict[str, Any]):
        self._version = version
        self._values = values

    @property
    def version(self) -> int:
        """本视图绑定的配置版本号。"""
        return self._version

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, attr: Any):
        try:
            return self._values[attr]
        except KeyError as ke:
            raise AttributeError(f"Config has no '{ke.args[0]}'")

    def __repr__(self):
        return f"ConfigSnapshot(version={self._version}, {self._values!r})"


class ConfigCandidate:
    """一份基于特定版本生成的候选配置快照。

    由 ``Config.stage()`` 创建，本身不会修改配置；调用
    ``activate()`` 时才会校验并原子生效。若激活时当前版本已
    不同于 ``base_version``（存在并发发布者），激活将被拒绝。
    """

    def __init__(
        self,
        config: Config,
        base_version: int,
        updates: dict[str, Any],
        removals: frozenset[str],
    ):
        self._config = config
        self.base_version = base_version
        self.updates = dict(updates)
        self.removals = frozenset(removals)
        self._activated_version: int | None = None
        self._consumed = False

    @property
    def version(self) -> int | None:
        """激活后得到的版本号；未激活时为 None。"""
        return self._activated_version

    @property
    def activated(self) -> bool:
        """本候选是否已成功激活。"""
        return self._consumed

    def proposed(self) -> dict[str, Any]:
        """返回候选与当前配置合并后的完整视图（不修改配置）。"""
        config = self._config
        with config._version_lock:
            proposed = {**dict(config), **self.updates}
        for key in self.removals:
            proposed.pop(key, None)
        return proposed

    def validate(self) -> list[str]:
        """对合并后的配置运行全部校验器。

        返回错误信息列表，空列表表示校验通过；本方法不产生任何
        副作用，可用于激活前的预检。
        """
        return self._config._run_validators(MappingProxyType(self.proposed()))

    def activate(self) -> ConfigChange:
        """校验并原子激活本候选快照。

        激活成功后配置立即整体切换到新版本，监听器收到一次
        ``ConfigChange`` 通知。校验失败、候选已被激活过、或
        当前版本已偏离 ``base_version`` 时抛出异常，配置保持
        不变。
        """
        config = self._config
        with config._version_lock:
            if self._consumed:
                raise ConfigStateError(
                    "Config candidate has already been activated"
                )
            if config._version != self.base_version:
                raise ConfigConflictError(
                    "Config candidate is based on version "
                    f"{self.base_version}, but the current version is "
                    f"{config._version}. Stage a new candidate and retry."
                )
            proposed = self.proposed()
            errors = config._run_validators(MappingProxyType(proposed))
            if errors:
                raise ConfigValidationError(errors)
            change = config._activate_locked(proposed)
            self._consumed = True
            self._activated_version = change.version
        config._notify_listeners(change)
        return change


class DescriptorMeta(ABCMeta):
    """项目内部接口说明。"""

    def __init__(cls, *_):
        cls.__setters__ = {name for name, _ in getmembers(cls, cls._is_setter)}

    @staticmethod
    def _is_setter(member: object):
        return isdatadescriptor(member) and hasattr(member, "setter")


class DetailedConverter(ABC):
    """项目内部接口说明。"""

    @abstractmethod
    def __call__(
        self, full_key: str, config_key: str, value: str, defaults: dict
    ) -> Any:
        """项目内部接口说明。"""


class Config(dict, metaclass=DescriptorMeta):
    """项目内部接口说明。"""

    ACCESS_LOG: bool
    AUTO_EXTEND: bool
    AUTO_RELOAD: bool
    EVENT_AUTOREGISTER: bool
    DEPRECATION_FILTER: FilterWarningType
    FORWARDED_FOR_HEADER: str
    FORWARDED_SECRET: str | None
    GRACEFUL_SHUTDOWN_TIMEOUT: float
    GRACEFUL_TCP_CLOSE_TIMEOUT: float
    INSPECTOR: bool
    INSPECTOR_HOST: str
    INSPECTOR_PORT: int
    INSPECTOR_TLS_KEY: Path | str | Default
    INSPECTOR_TLS_CERT: Path | str | Default
    INSPECTOR_API_KEY: str
    KEEP_ALIVE_TIMEOUT: int
    KEEP_ALIVE: bool
    LOCAL_CERT_CREATOR: str | LocalCertCreator
    LOCAL_TLS_KEY: Path | str | Default
    LOCAL_TLS_CERT: Path | str | Default
    LOCALHOST: str
    LOG_EXTRA: Default | bool
    MOTD: bool
    MOTD_DISPLAY: dict[str, str]
    NO_COLOR: bool
    NOISY_EXCEPTIONS: bool
    PROXIES_COUNT: int | None
    REAL_IP_HEADER: str | None
    REQUEST_BUFFER_SIZE: int
    REQUEST_MAX_HEADER_SIZE: int
    REQUEST_ID_HEADER: str
    REQUEST_MAX_SIZE: int
    REQUEST_TIMEOUT: int
    RESPONSE_TIMEOUT: int
    SERVER_NAME: str
    TLS_CERT_PASSWORD: str
    TOUCHUP: Default | bool
    USE_UVLOOP: Default | bool
    WEBSOCKET_MAX_SIZE: int
    WEBSOCKET_PING_INTERVAL: int
    WEBSOCKET_PING_TIMEOUT: int

    def __init__(
        self,
        defaults: dict[str, str | bool | int | float | None] | None = None,
        env_prefix: str | None = SANIC_PREFIX,
        keep_alive: bool | None = None,
        *,
        converters: Sequence[Callable[[str], Any]] | None = None,
        state_path: str | Path | None = None,
        history_size: int = 16,
    ):
        if history_size < 1:
            raise ValueError("history_size must be at least 1")
        # 版本化相关内部状态通过 object.__setattr__ 存放，
        # 避免经 __setattr__ 落入配置字典本身。
        object.__setattr__(self, "_version", 0)
        object.__setattr__(self, "_version_lock", RLock())
        object.__setattr__(self, "_change_listeners", [])
        object.__setattr__(self, "_validators", [])
        object.__setattr__(self, "_records", [])
        object.__setattr__(self, "_sensitive_keys", set())
        object.__setattr__(self, "_sensitive_patterns", [])
        object.__setattr__(self, "_state_path", None)
        object.__setattr__(self, "_history_size", int(history_size))

        defaults = defaults or {}
        self.defaults = {**DEFAULT_CONFIG, **defaults}
        super().__init__(self.defaults)
        self._configure_warnings()

        self._converters = [str, str_to_bool, float, int]

        if converters:
            for converter in converters:
                self.register_type(converter)

        if keep_alive is not None:
            self.KEEP_ALIVE = keep_alive

        if env_prefix != SANIC_PREFIX:
            if env_prefix:
                self.load_environment_vars(env_prefix)
        else:
            self.load_environment_vars(SANIC_PREFIX)

        self._configure_header_size()
        self._check_error_format()
        self._init = True

        self._records.append(_VersionRecord(0, dict(self), None))
        if state_path is not None:
            self.enable_persistence(state_path)

    def __getattr__(self, attr: Any):
        try:
            return self[attr]
        except KeyError as ke:
            raise AttributeError(f"Config has no '{ke.args[0]}'")

    def __setattr__(self, attr: str, value: Any) -> None:
        self.update({attr: value})

    def __setitem__(self, attr: str, value: Any) -> None:
        self.update({attr: value})

    def update(self, *other: Any, **kwargs: Any) -> None:
        """项目内部接口说明。"""
        kwargs.update({k: v for item in other for k, v in dict(item).items()})
        setters: dict[str, Any] = {
            k: kwargs.pop(k)
            for k in {**kwargs}.keys()
            if k in self.__class__.__setters__
        }

        for key, value in setters.items():
            try:
                super().__setattr__(key, value)
            except AttributeError:
                ...

        super().update(**kwargs)
        for attr, value in {**setters, **kwargs}.items():
            self._post_set(attr, value)

    def _post_set(self, attr, value) -> None:
        if self.get("_init"):
            if attr in (
                "REQUEST_MAX_HEADER_SIZE",
                "REQUEST_BUFFER_SIZE",
                "REQUEST_MAX_SIZE",
            ):
                self._configure_header_size()

        if attr == "LOCAL_CERT_CREATOR" and not isinstance(
            self.LOCAL_CERT_CREATOR, LocalCertCreator
        ):
            self.LOCAL_CERT_CREATOR = LocalCertCreator[
                self.LOCAL_CERT_CREATOR.upper()
            ]
        elif attr == "DEPRECATION_FILTER":
            self._configure_warnings()

    @property
    def FALLBACK_ERROR_FORMAT(self) -> str:
        if isinstance(self._FALLBACK_ERROR_FORMAT, Default):
            return DEFAULT_FORMAT
        return self._FALLBACK_ERROR_FORMAT

    @FALLBACK_ERROR_FORMAT.setter
    def FALLBACK_ERROR_FORMAT(self, value):
        self._check_error_format(value)
        if (
            not isinstance(self._FALLBACK_ERROR_FORMAT, Default)
            and value != self._FALLBACK_ERROR_FORMAT
        ):
            error_logger.warning(
                "Setting config.FALLBACK_ERROR_FORMAT on an already "
                "configured value may have unintended consequences."
            )
        self._FALLBACK_ERROR_FORMAT = value

    def _configure_header_size(self):
        Http.set_header_max_size(
            self.REQUEST_MAX_HEADER_SIZE,
            self.REQUEST_BUFFER_SIZE - 4096,
            self.REQUEST_MAX_SIZE,
        )

    def _configure_warnings(self):
        filterwarnings(
            self.DEPRECATION_FILTER,
            category=DeprecationWarning,
            module=r"sanic.*",
        )

    def _check_error_format(self, format: str | None = None):
        check_error_format(format or self.FALLBACK_ERROR_FORMAT)

    def load_environment_vars(self, prefix=SANIC_PREFIX):
        """项目内部接口说明。"""
        for key, value in environ.items():
            if not key.startswith(prefix) or not key.isupper():
                continue

            _, config_key = key.split(prefix, 1)

            for converter in reversed(self._converters):
                try:
                    if isinstance(converter, DetailedConverter):
                        self[config_key] = converter(
                            key, config_key, value, self.defaults
                        )
                    else:
                        self[config_key] = converter(value)
                    break
                except ValueError:
                    pass

    def update_config(self, config: bytes | str | dict[str, Any] | Any):
        """项目内部接口说明。"""
        if isinstance(config, (bytes, str, Path)):
            config = load_module_from_file_location(location=config)

        if not isinstance(config, dict):
            cfg = {}
            if not isclass(config):
                cfg.update(
                    {
                        key: getattr(config, key)
                        for key in config.__class__.__dict__.keys()
                    }
                )

            config = dict(config.__dict__)
            config.update(cfg)

        config = dict(filter(lambda i: i[0].isupper(), config.items()))

        self.update(config)

    load = update_config

    def register_type(self, converter: Callable[[str], Any]) -> None:
        """项目内部接口说明。"""
        if converter in self._converters:
            error_logger.warning(
                f"Configuration value converter '{converter.__name__}' has "
                "already been registered"
            )
            return
        self._converters.append(converter)

    @property
    def version(self) -> int:
        """当前激活的配置版本号。

        初始为 0，每次成功激活（含回退）递增 1；直接对配置项
        赋值不经过版本管理，不会改变版本号。
        """
        return self._version

    @property
    def versions(self) -> tuple[int, ...]:
        """当前保留、可用于回退的版本号（含初始版本 0）。"""
        return tuple(record.version for record in self._records)

    @property
    def history(self) -> tuple[ConfigChange, ...]:
        """保留期内的激活历史，按时间升序排列。"""
        return tuple(
            record.change
            for record in self._records
            if record.change is not None
        )

    def stage(
        self,
        updates: dict[str, Any] | None = None,
        /,
        *,
        remove: Sequence[str] = (),
        **kwargs: Any,
    ) -> ConfigCandidate:
        """基于当前版本生成一份候选配置快照（不修改配置）。

        ``updates`` 为待写入的键值，``remove`` 为待删除的键；
        候选需调用 ``ConfigCandidate.activate()`` 才会校验并
        原子生效。同一键不能同时出现在 updates 与 remove 中。
        """
        merged = dict(updates or {})
        merged.update(kwargs)
        overlap = set(merged).intersection(remove)
        if overlap:
            raise ValueError(
                "Config keys cannot be both updated and removed: "
                f"{sorted(overlap)}"
            )
        with self._version_lock:
            base_version = self._version
        return ConfigCandidate(self, base_version, merged, frozenset(remove))

    def rollback(self, version: int | None = None) -> ConfigChange:
        """回退到指定版本；缺省回退到保留的上一个版本。

        回退本身也是一次原子激活：生成新的版本号，配置内容整体
        恢复为目标版本的快照，监听器会收到 ``rollback_of`` 指向
        目标版本的变更结果。目标版本不在保留历史中时抛出
        ``ConfigStateError``。
        """
        with self._version_lock:
            if version is None:
                candidates = [
                    record
                    for record in self._records
                    if record.version < self._version
                ]
                if not candidates:
                    raise ConfigStateError(
                        "No previous config version to roll back to"
                    )
                target = candidates[-1]
            else:
                target = next(
                    (
                        record
                        for record in self._records
                        if record.version == version
                    ),
                    None,
                )
                if target is None:
                    raise ConfigStateError(
                        f"Unknown config version: {version}. "
                        f"Retained versions: {list(self.versions)}"
                    )
                if target.version == self._version:
                    raise ConfigStateError(
                        f"Config is already at version {version}"
                    )
            change = self._activate_locked(
                dict(target.values), rollback_of=target.version
            )
        self._notify_listeners(change)
        return change

    def snapshot(self) -> ConfigSnapshot:
        """获取绑定当前版本的不可变配置视图。"""
        with self._version_lock:
            return ConfigSnapshot(self._version, dict(self))

    @contextmanager
    def bound(self) -> Iterator[ConfigSnapshot]:
        """在一段代码块（如一次请求处理）内绑定当前配置版本。

        块内通过该视图读取配置，即使期间其他发布者激活了新版本，
        读到的内容与版本号仍保持进入时的状态。
        """
        yield self.snapshot()

    def add_change_listener(
        self, listener: Callable[[ConfigChange], None]
    ) -> None:
        """注册变更监听器。

        每次成功激活（含回退）后按注册顺序调用一次，参数为
        ``ConfigChange``；监听器抛出的异常会被记录日志，不影响
        已生效的激活与其余监听器。重复注册同一监听器会被忽略。
        """
        if listener in self._change_listeners:
            error_logger.warning(
                f"Config change listener '{listener}' has already "
                "been registered"
            )
            return
        self._change_listeners.append(listener)

    def remove_change_listener(
        self, listener: Callable[[ConfigChange], None]
    ) -> None:
        """移除已注册的变更监听器。"""
        self._change_listeners.remove(listener)

    def register_validator(
        self, validator: Callable[[Mapping[str, Any]], Any]
    ) -> None:
        """注册配置校验器。

        校验器接收合并后的完整配置（只读映射），返回 None 表示
        通过；返回错误信息字符串（或其可迭代对象）、或抛出
        ``ConfigValidationError`` 表示失败。激活时按注册顺序
        执行全部校验器，任一失败则本次激活被拒绝，配置与版本号
        保持不变。
        """
        if validator in self._validators:
            error_logger.warning(
                f"Config validator '{validator}' has already been registered"
            )
            return
        self._validators.append(validator)

    def mark_sensitive(self, *keys_or_patterns: str | re.Pattern) -> None:
        """登记额外的敏感配置项。

        接受精确键名（不区分大小写）或编译后的正则；敏感项的值
        不会出现在变更结果的差异摘要中，统一以 ``SENSITIVE_MASK``
        遮蔽。
        """
        for item in keys_or_patterns:
            if isinstance(item, str):
                self._sensitive_keys.add(item.upper())
            elif isinstance(item, re.Pattern):
                self._sensitive_patterns.append(item)
            else:
                raise TypeError(
                    "Sensitive marker must be a str or re.Pattern, "
                    f"not {type(item).__name__}"
                )

    def enable_persistence(self, path: str | Path) -> None:
        """启用版本状态持久化。

        之后每次激活会先将新状态原子写入 ``path``（临时文件 +
        替换），写入失败则本次激活被拒绝；若 ``path`` 已存在状态
        文件，则立即恢复其中的版本号、历史与配置内容，用于进程
        重启后的版本恢复。
        """
        path = Path(path)
        with self._version_lock:
            object.__setattr__(self, "_state_path", path)
            if path.exists():
                self._load_state_locked(path)
            else:
                self._save_state_locked()

    def _activate_locked(
        self, proposed: dict[str, Any], rollback_of: int | None = None
    ) -> ConfigChange:
        """在持有版本锁的前提下完成原子激活（调用前须已校验）。"""
        old = dict(self)
        change = ConfigChange(
            version=self._version + 1,
            previous_version=self._version,
            added={
                key: self._mask(key, value)
                for key, value in proposed.items()
                if key not in old
            },
            removed={
                key: self._mask(key, old[key])
                for key in old
                if key not in proposed
            },
            updated={
                key: (self._mask(key, old[key]), self._mask(key, value))
                for key, value in proposed.items()
                if key in old and not self._values_equal(old[key], value)
            },
            rollback_of=rollback_of,
            activated_at=time.time(),
        )
        record = _VersionRecord(change.version, dict(proposed), change)
        if self._state_path is not None:
            # 先持久化再提交内存状态，写入失败时本次激活整体无效
            self._save_state_locked(pending=record)
        self.update(proposed)
        for key in old:
            if key not in proposed:
                dict.__delitem__(self, key)
        object.__setattr__(self, "_version", change.version)
        self._records.append(record)
        del self._records[: -self._history_size]
        return change

    def _notify_listeners(self, change: ConfigChange) -> None:
        for listener in list(self._change_listeners):
            try:
                listener(change)
            except Exception:
                error_logger.exception(
                    "Config change listener %r raised an exception",
                    listener,
                )

    def _run_validators(self, proposed: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        for validator in self._validators:
            try:
                result = validator(proposed)
            except ConfigValidationError as exc:
                errors.extend(exc.errors)
            except Exception as exc:
                name = getattr(validator, "__name__", validator)
                errors.append(f"{name}: {exc}")
            else:
                if result is None:
                    continue
                if isinstance(result, str):
                    errors.append(result)
                else:
                    errors.extend(str(item) for item in result)
        return errors

    def _is_sensitive(self, key: str) -> bool:
        upper = key.upper()
        if upper in self._sensitive_keys:
            return True
        if any(marker in upper for marker in DEFAULT_SENSITIVE_MARKERS):
            return True
        return any(pattern.search(key) for pattern in self._sensitive_patterns)

    def _mask(self, key: str, value: Any) -> Any:
        if self._is_sensitive(key):
            return SENSITIVE_MASK
        return value

    @staticmethod
    def _values_equal(old: Any, new: Any) -> bool:
        try:
            return bool(old == new)
        except Exception:
            return False

    def _save_state_locked(
        self, pending: _VersionRecord | None = None
    ) -> None:
        records = list(self._records)
        if pending is not None:
            records.append(pending)
        version = pending.version if pending is not None else self._version
        state = {
            "version": version,
            "history": [
                self._record_to_dict(record)
                for record in records[-self._history_size :]
            ],
        }
        path = self._state_path
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            with open(tmp_path, "wb") as file:
                pickle.dump(state, file)
            os_replace(tmp_path, path)
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigStateError(
                f"Failed to persist config state to {path}: {exc}"
            ) from exc

    def _load_state_locked(self, path: Path) -> bool:
        try:
            with open(path, "rb") as file:
                state = pickle.load(file)
            version = state["version"]
            records = [
                self._record_from_dict(item) for item in state["history"]
            ]
            active = next(
                record for record in records if record.version == version
            )
        except FileNotFoundError:
            return False
        except Exception as exc:
            error_logger.error(
                "Could not load config state from %s (%s); "
                "starting with a fresh config state",
                path,
                exc,
            )
            return False
        # 以持久化的激活版本为准整体恢复；仅保留本进程注册的
        # 类型转换器（属于运行时机制而非配置值）。
        converters = self.get("_converters")
        current = dict(self)
        self.update(active.values)
        for key in current:
            if key not in active.values:
                dict.__delitem__(self, key)
        if converters is not None:
            dict.__setitem__(self, "_converters", converters)
        object.__setattr__(self, "_version", version)
        self._records[:] = records[-self._history_size :]
        return True

    @staticmethod
    def _record_to_dict(record: _VersionRecord) -> dict[str, Any]:
        change = record.change
        return {
            "version": record.version,
            "values": record.values,
            "change": {
                "version": change.version,
                "previous_version": change.previous_version,
                "added": change.added,
                "removed": change.removed,
                "updated": change.updated,
                "rollback_of": change.rollback_of,
                "activated_at": change.activated_at,
            }
            if change is not None
            else None,
        }

    @staticmethod
    def _record_from_dict(data: dict[str, Any]) -> _VersionRecord:
        change_data = data.get("change")
        change = (
            ConfigChange(**change_data) if change_data is not None else None
        )
        return _VersionRecord(data["version"], data["values"], change)
