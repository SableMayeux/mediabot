from abc import ABC


class Provider(ABC):
    """
    Base class for external MediaBot providers.

    Providers own protocol/API behavior.

    Discord commands should never need to know
    authentication headers, endpoint quirks, or
    provider-specific URL handling.
    """

    name = "provider"

    async def start(self):
        pass

    async def close(self):
        pass
