from __future__ import annotations


class VertexError(Exception):
    """Base exception for Vertex failures."""


class ConfigError(VertexError):
    """Raised when report configuration is missing or invalid."""


class PersonaSchemaError(ConfigError):
    """Raised when persona signal enforcement configuration is invalid."""


class AuthError(VertexError):
    """Raised when authentication to an external system fails."""


class CredentialExpired(AuthError):
    """Raised when a credential (PAT, AAD token, managed-identity token) has expired
    mid-flight and cannot auto-refresh.  Callers must surface this as an
    ``ActionRequired`` operator prompt rather than a crash.

    Attributes
    ----------
    auth_method:
        Short label for the expired credential type (e.g. ``"ADO_PAT"``,
        ``"AAD_device_code"``, ``"managed_identity"``).
    connector:
        The Zone-C connector that detected the expiry (e.g. ``"ADO"``,
        ``"Graph"``, ``"Kusto"``).
    """

    def __init__(self, message: str, *, auth_method: str = "", connector: str = "") -> None:
        super().__init__(message)
        self.auth_method = auth_method
        self.connector = connector


class QueryError(VertexError):
    """Raised when an external query fails."""


class QueryTimeoutError(QueryError):
    """Raised when an external query exceeds the configured timeout."""


class RenderError(VertexError):
    """Raised when a template cannot be rendered or required render data is missing."""


class ConfirmError(VertexError):
    """Raised when confirmation or archival promotion cannot complete safely."""


class StateError(VertexError):
    """Raised when file-backed workflow state is missing, locked, or inconsistent."""


class StateConsistencyError(StateError):
    """Raised when archive, review, or baseline state disagree about the latest issue."""


class VertexMigrationError(VertexError):
    """Raised when a layout migration is in an inconsistent or split-brain state."""
