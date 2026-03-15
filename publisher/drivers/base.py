"""Abstract base class for platform publishing drivers."""
from __future__ import annotations

import abc
from typing import Any, Dict, Optional


class BaseDriver(abc.ABC):
    """Every platform driver must implement these methods."""

    @abc.abstractmethod
    def login_url(self) -> str:
        """Return the platform's creator/login page URL."""

    @abc.abstractmethod
    async def check_login(self, page: Any) -> bool:
        """Return True if the current browser page has a valid logged-in session."""

    @abc.abstractmethod
    async def publish(
        self,
        page: Any,
        file_path: str,
        title: str,
        description: str,
        tags: str,
        options: Optional[Dict[str, Any]] = None,
        cover_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload and publish content. Returns {"ok": bool, "url": str, "error": str}."""
