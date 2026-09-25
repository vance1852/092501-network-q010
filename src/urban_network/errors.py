"""路线与离线回执服务使用的可观察错误。"""
from __future__ import annotations


class ServiceError(RuntimeError):
    code = "service_error"
    status = 400


class NotFound(ServiceError):
    code = "not_found"
    status = 404


class ValidationFailed(ServiceError):
    code = "validation_failed"
    status = 422


class Conflict(ServiceError):
    code = "conflict"
    status = 409


class InvalidState(Conflict):
    code = "invalid_state"
    status = 409
