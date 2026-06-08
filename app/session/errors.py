from __future__ import annotations


class SessionError(RuntimeError):
    """Base error for session contract failures."""


class SessionNotFoundError(SessionError):
    pass


class SessionOwnershipError(SessionError):
    pass


class SessionEndedError(SessionError):
    pass


class SessionArchivedError(SessionError):
    pass


class SessionTransitionError(SessionError):
    pass


class SessionMigrationError(SessionError):
    pass
