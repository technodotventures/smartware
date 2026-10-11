"""Python client for the ORP memory protocol (HTTP binding, spec 0.1).

    from orp_client import OrpClient
    client = OrpClient("https://memory.example.com", token_env="ORP_TOKEN")
    client.remember("Alice prefers tea")             # readable by you alone
    pack = client.recall("what does Alice drink?")    # budgeted, cited context
"""
from .client import Advanced, ChangeFeed, MEMORY_INPUT_KEYS, OrpClient, as_of_time, utc_now
from .errors import (BudgetTooSmallError, DegradedError, ERRORS, ForbiddenError, InternalError, InvalidParameterError,
                     NotFoundError, OrpError, PayloadTooLargeError, RateLimitedError, TransportError,
                     UnauthorizedError, UnavailableError, UnsupportedParameterError, VersionConflictError)
from .types import (ContextItem, ContextPack, ForgetResult, Memory, Profile, ProfileItem, RememberResult, Whoami)

__version__ = "0.1.0"

__all__ = [
    "OrpClient", "Advanced", "ChangeFeed", "MEMORY_INPUT_KEYS", "as_of_time", "utc_now",
    "OrpError", "UnsupportedParameterError", "InvalidParameterError", "VersionConflictError", "ForbiddenError",
    "NotFoundError", "BudgetTooSmallError", "RateLimitedError", "UnauthorizedError", "PayloadTooLargeError",
    "DegradedError", "UnavailableError", "InternalError", "TransportError", "ERRORS",
    "ContextItem", "ContextPack", "ForgetResult", "Memory", "Profile", "ProfileItem", "RememberResult", "Whoami",
]
