class AppError(Exception):
    """An actionable error that can be returned without upstream secrets."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
