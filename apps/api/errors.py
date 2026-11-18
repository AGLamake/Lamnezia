"""API errors: a code and an English message for programs, and the HTTP status they answer with."""

from apps.api.models import ApiToken


class ApiError(Exception):
    """Answered as ``{"error": {"code": ..., "message": ...}}`` with `status`."""

    status = 400
    code = "bad_request"
    message = "The request is not valid."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message

    def headers(self) -> dict[str, str]:
        return {}


class TokenMissingError(ApiError):
    status = 401
    code = "token_missing"
    message = "Send the token in the header: Authorization: Bearer <token>."

    def headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": 'Bearer realm="api"'}


class TokenInvalidError(TokenMissingError):
    code = "token_invalid"
    message = "The token is unknown, disabled or expired."


class TokenRejectedError(TokenInvalidError):
    """A known token that no longer works; the call is written to the audit."""

    def __init__(self, token: ApiToken) -> None:
        super().__init__()
        self.token = token


class TooManyCallsError(ApiError):
    status = 429
    code = "rate_limited"
    message = "Too many calls. Try again in a minute."

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)}


class NotFoundError(ApiError):
    status = 404
    code = "not_found"
    message = "Nothing like this, or it is outside the token's zone."


class MethodNotAllowedError(ApiError):
    status = 405
    code = "method_not_allowed"
    message = "The API is read only: use GET."

    def headers(self) -> dict[str, str]:
        return {"Allow": "GET"}


class BadParameterError(ApiError):
    code = "bad_parameter"

    def __init__(self, name: str, expected: str) -> None:
        super().__init__(f"Parameter {name!r} must be {expected}.")
