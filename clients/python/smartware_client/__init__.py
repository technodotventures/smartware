"""Python client for the Smartware memory protocol (HTTP binding, spec 0.1).

    from smartware_client import SmartwareClient
    client = SmartwareClient("https://memory.example.com", token_env="SMARTWARE_TOKEN")
    client.remember("Alice prefers tea")             # readable by you alone
    pack = client.recall("what does Alice drink?")    # budgeted, cited context
"""
from .client import Advanced, ChangeFeed, MEMORY_INPUT_KEYS, SmartwareClient, as_of_time, utc_now
from .errors import (BudgetTooSmallError, DegradedError, ERRORS, ForbiddenError, InternalError, InvalidParameterError,
                     NotFoundError, SmartwareError, PayloadTooLargeError, RateLimitedError, TransportError,
                     UnauthorizedError, UnavailableError, UnsupportedParameterError, VersionConflictError)
from .types import (ContextItem, ContextPack, ForgetResult, Memory, Profile, ProfileItem, RememberResult, Whoami)

__version__ = "2.0.0"

__all__ = [
    "SmartwareClient", "Advanced", "ChangeFeed", "MEMORY_INPUT_KEYS", "as_of_time", "utc_now",
    "SmartwareError", "UnsupportedParameterError", "InvalidParameterError", "VersionConflictError", "ForbiddenError",
    "NotFoundError", "BudgetTooSmallError", "RateLimitedError", "UnauthorizedError", "PayloadTooLargeError",
    "DegradedError", "UnavailableError", "InternalError", "TransportError", "ERRORS",
    "ContextItem", "ContextPack", "ForgetResult", "Memory", "Profile", "ProfileItem", "RememberResult", "Whoami",
]
