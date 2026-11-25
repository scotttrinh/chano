from __future__ import annotations

from typing import Any

import msgspec

from chano.status import UnprocessableContent


class ValidationErrorDetail(msgspec.Struct):
    detail: list[dict[str, Any]]


class RequestValidationError(UnprocessableContent[ValidationErrorDetail]):
    """Validation Error"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(ValidationErrorDetail(detail=errors))
