"""Role-level transport dispatch, including repair and auxiliary calls."""
from __future__ import annotations

import threading


class RoleProvider:
    name = "role_router"

    def __init__(self, cfg, factory):
        self.cfg, self.factory = cfg, factory
        self.providers = {}
        self.lock = threading.Lock()
        self.client = self  # existing runtime cleanup calls client.close()

    def generate(self, spec):
        kind = self.cfg.provider_for(spec.role)
        with self.lock:
            if kind not in self.providers:
                self.providers[kind] = self.factory(kind)
            provider = self.providers[kind]
        return provider.generate(spec)

    def list_models(self):
        with self.lock:
            kind = self.cfg.provider.kind
            if kind not in self.providers:
                self.providers[kind] = self.factory(kind)
            provider = self.providers[kind]
        return provider.list_models()

    def close(self):
        for provider in self.providers.values():
            close = getattr(getattr(provider, "client", None), "close", None)
            if callable(close):
                close()
