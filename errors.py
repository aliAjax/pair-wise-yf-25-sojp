"""共享异常类型，供各模块抛出业务错误。"""


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
