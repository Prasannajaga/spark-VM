"""Backward-compatible import shim for Firecracker process wrapper."""

from .firecracker.process import FirecrackerProcess

__all__ = ["FirecrackerProcess"]
