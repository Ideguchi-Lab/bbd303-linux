"""Public exception hierarchy."""


class BBD303Error(Exception):
    """Base error for this package."""


class ConfigurationError(BBD303Error):
    """Configuration is absent or invalid."""


class DiscoveryError(BBD303Error):
    """The configured controller could not be found."""


class DeviceBusyError(BBD303Error):
    """Another local process owns the controller lock."""


class DeviceStateError(BBD303Error):
    """The controller state makes the requested action unsafe."""


class MotionTimeoutError(BBD303Error):
    """A motion did not settle before its deadline."""


class XACommunicationError(BBD303Error):
    """The XA SDK rejected an operation or lost communication."""

