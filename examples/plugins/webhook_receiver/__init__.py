from .manifest import MANIFEST
from .plugin import WebhookReceiverPlugin

PLUGIN_CLASS = WebhookReceiverPlugin

__all__ = ["MANIFEST", "PLUGIN_CLASS"]
