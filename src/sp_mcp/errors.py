class NormsError(Exception):
    def __init__(self, code, message, *, http_status=None, retryable=False):
        super().__init__(message)
        self.code, self.message = code, message
        self.http_status, self.retryable = http_status, retryable

    def payload(self):
        return {
            "status": "error",
            "error_code": self.code,
            "message": self.message,
            "http_status": self.http_status,
            "retryable": self.retryable,
        }
