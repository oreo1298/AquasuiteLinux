"""Exceptions raised by the core."""

from __future__ import annotations


class AquaError(Exception):
    """Base class for errors the app reports to the user."""


class DeviceError(AquaError):
    """A device could not be opened, read or written."""


class PermissionDenied(DeviceError):
    """The hidraw node exists but this user may not open it (udev rule missing)."""


class NotSupported(AquaError):
    """The device (or this backend) cannot do what was asked."""


class ConfigError(AquaError):
    """The configuration is invalid (unknown sensor, cycle between virtual sensors, ...)."""


class ServiceError(AquaError):
    """Talking to the background service failed."""
