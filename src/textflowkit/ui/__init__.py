"""TextFlowKit local browser interface.

A separately launched, loopback-only workspace that mounts the existing
developer HTTP app under ``/api`` and adds a browser-session layer, packaged
static assets, a streamed upload, and a fixed-format download. It is a *local*
interface, not a hosted website: it is never started by the developer HTTP
launcher, never binds beyond loopback, and ships no CDN asset, external script,
or telemetry.
"""

from __future__ import annotations

__all__ = ["app", "capabilities", "launcher", "paths", "security", "uploads"]
