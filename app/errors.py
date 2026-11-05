class JobError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JobInterrupted(Exception):
    """The server is shutting down; retry this job after restart."""


class JobCancelled(Exception):
    """The user asked to stop this job; keep any completed files."""
