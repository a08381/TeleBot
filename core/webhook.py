"""Registry for additional HTTP routes hosted alongside the Telegram webhook."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

WebhookHandler = Callable[[Any], Awaitable[Any]]
_ROUTES: dict[str, WebhookHandler] = {}


def webhook_route(path: str) -> Callable[[WebhookHandler], WebhookHandler]:
    """Register a GET endpoint on the shared webhook listener."""
    normalized = "/" + path.strip("/")
    if normalized == "/":
        raise ValueError("webhook route path must not be '/'")

    def decorate(func: WebhookHandler) -> WebhookHandler:
        if normalized in _ROUTES:
            raise ValueError(f"duplicate webhook route: {normalized}")
        _ROUTES[normalized] = func
        return func

    return decorate


def webhook_routes() -> dict[str, WebhookHandler]:
    """Return a snapshot of registered plugin webhook routes."""
    return dict(_ROUTES)


def clear_webhook_routes() -> None:
    """Clear plugin routes before the plugin loader reloads modules."""
    _ROUTES.clear()
