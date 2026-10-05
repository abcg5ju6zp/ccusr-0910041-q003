from asyncio import CancelledError
from collections.abc import Sequence
from os import PathLike
from typing import Any

from sanic.helpers import STATUS_CODES
from sanic.models.protocol_types import Range


class RequestCancelled(CancelledError):
    quiet = True


class ServerKilled(Exception):
    """项目内部接口说明。"""

    quiet = True


class SanicException(Exception):
    """项目内部接口说明。"""

    status_code: int = 500
    quiet: bool | None = False
    headers: dict[str, str] = {}
    message: str = ""

    def __init__(
        self,
        message: str | bytes | None = None,
        status_code: int | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.context = context
        self.extra = extra
        status_code = status_code or getattr(
            self.__class__, "status_code", None
        )
        quiet = (
            quiet
            if quiet is not None
            else getattr(self.__class__, "quiet", None)
        )
        headers = headers or getattr(self.__class__, "headers", {})
        if message is None:
            message = self.message
            if not message and status_code:
                msg = STATUS_CODES.get(status_code, b"")
                message = msg.decode()
        elif isinstance(message, bytes):
            message = message.decode()

        super().__init__(message)

        self.status_code = status_code or self.status_code
        self.quiet = quiet
        self.headers = headers
        try:
            self.message = message
        except AttributeError:
            ...


class HTTPException(SanicException):
    """项目内部接口说明。"""

    def __init__(
        self,
        message: str | bytes | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )


class NotFound(HTTPException):
    """项目内部接口说明。"""

    status_code = 404
    quiet = True


class BadRequest(HTTPException):
    """项目内部接口说明。"""

    status_code = 400
    quiet = True


InvalidUsage = BadRequest
BadURL = BadRequest


class MethodNotAllowed(HTTPException):
    """项目内部接口说明。"""

    status_code = 405
    quiet = True

    def __init__(
        self,
        message: str | bytes | None = None,
        method: str = "",
        allowed_methods: Sequence[str] | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ):
        super().__init__(
            message,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )
        if allowed_methods:
            self.headers = {
                **self.headers,
                "Allow": ", ".join(allowed_methods),
            }
        self.method = method
        self.allowed_methods = allowed_methods


MethodNotSupported = MethodNotAllowed


class ServerError(HTTPException):
    """项目内部接口说明。"""

    status_code = 500


InternalServerError = ServerError


class ServiceUnavailable(HTTPException):
    """项目内部接口说明。"""

    status_code = 503
    quiet = True


class URLBuildError(HTTPException):
    """项目内部接口说明。"""

    status_code = 500


class FileNotFound(NotFound):
    """项目内部接口说明。"""

    def __init__(
        self,
        message: str | bytes | None = None,
        path: PathLike | None = None,
        relative_url: str | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ):
        super().__init__(
            message,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )
        self.path = path
        self.relative_url = relative_url


class RequestTimeout(HTTPException):
    """项目内部接口说明。"""

    status_code = 408
    quiet = True


class PayloadTooLarge(HTTPException):
    """项目内部接口说明。"""

    status_code = 413
    quiet = True


class URITooLong(HTTPException):
    """项目内部接口说明。"""

    status_code = 414
    quiet = True


class HeaderNotFound(BadRequest):
    """项目内部接口说明。"""


class InvalidHeader(BadRequest):
    """项目内部接口说明。"""


class RangeNotSatisfiable(HTTPException):
    """项目内部接口说明。"""

    status_code = 416
    quiet = True

    def __init__(
        self,
        message: str | bytes | None = None,
        content_range: Range | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ):
        super().__init__(
            message,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )
        if content_range is not None:
            self.headers = {
                **self.headers,
                "Content-Range": f"bytes */{content_range.total}",
            }


ContentRangeError = RangeNotSatisfiable


class ExpectationFailed(HTTPException):
    """项目内部接口说明。"""

    status_code = 417
    quiet = True


HeaderExpectationFailed = ExpectationFailed


class Forbidden(HTTPException):
    """项目内部接口说明。"""

    status_code = 403
    quiet = True


class InvalidRangeType(RangeNotSatisfiable):
    """项目内部接口说明。"""

    status_code = 416
    quiet = True


class PyFileError(SanicException):
    def __init__(
        self,
        file,
        status_code: int | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ):
        super().__init__(
            "could not execute config file %s" % file,
            status_code=status_code,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )


class ConfigError(SanicException):
    """配置版本化发布相关错误的基类。"""


class ConfigValidationError(ConfigError):
    """候选配置快照未通过校验时抛出。

    所有校验器产生的错误信息会汇总在 ``errors`` 中，
    校验失败时配置内容与版本号均保持不变。
    """

    def __init__(
        self,
        errors: str | Sequence[str],
        status_code: int | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = tuple(str(error) for error in errors)
        super().__init__(
            "Config validation failed: " + "; ".join(self.errors),
            status_code=status_code,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )


class ConfigConflictError(ConfigError):
    """候选快照基于已过期的版本，激活被拒绝。

    并发发布者基于同一版本生成候选快照时，只有最先激活的
    候选会成功，其余候选会收到本异常，重新 stage 后可重试。
    """


class ConfigStateError(ConfigError):
    """当前配置版本状态不允许该操作。

    例如重复激活同一候选快照、回退到未知版本，
    或版本状态持久化失败。
    """


class Unauthorized(HTTPException):
    """项目内部接口说明。"""

    status_code = 401
    quiet = True

    def __init__(
        self,
        message: str | bytes | None = None,
        scheme: str | None = None,
        *,
        quiet: bool | None = None,
        context: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        **challenges,
    ):
        super().__init__(
            message,
            quiet=quiet,
            context=context,
            extra=extra,
            headers=headers,
        )

        # if auth-scheme is specified, set "WWW-Authenticate" header
        if scheme is not None:
            values = [f'{k!s}="{v!s}"' for k, v in challenges.items()]
            challenge = ", ".join(values)

            self.headers = {
                **self.headers,
                "WWW-Authenticate": f"{scheme} {challenge}".rstrip(),
            }


class LoadFileException(SanicException):
    """项目内部接口说明。"""


class InvalidSignal(SanicException):
    """项目内部接口说明。"""


class WebsocketClosed(SanicException):
    """项目内部接口说明。"""

    quiet = True
    message = "Client has closed the websocket connection"
