"""Backward-compatible import shim for Firecracker API wrapper."""

from .firecracker.api import FirecrackerAPIClient, FirecrackerAPIResponse

__all__ = ["FirecrackerAPIClient", "FirecrackerAPIResponse"]
