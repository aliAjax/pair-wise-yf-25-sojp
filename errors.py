"""跨模块共享的业务异常：时段规则、分配事务与 HTTP 层都可能抛出。"""
from __future__ import annotations


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
