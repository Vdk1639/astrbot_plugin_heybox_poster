"""Local decisions that prove no platform write has begun."""


class PolicyBlockedError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
