from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_BUYWELL_URL = "https://buywell.pro/api"


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    buywell_url: str = DEFAULT_BUYWELL_URL
    connection_token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.connection_token)

    def validate(self) -> "RuntimeConfig":
        parsed = urlsplit(self.buywell_url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in ({"https", "http"} if local else {"https"}):
            raise ConfigurationError("Buywell URL must use HTTPS")
        if not parsed.netloc or parsed.query or parsed.fragment:
            raise ConfigurationError("Buywell URL is invalid")
        if self.connection_token and len(self.connection_token) < 8:
            raise ConfigurationError("Buywell connection key is too short")
        return self


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> RuntimeConfig:
        with self._lock:
            if not self.path.exists():
                return RuntimeConfig()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ConfigurationError("Could not read module configuration") from error
            if not isinstance(data, dict) or set(data) - {"buywell_url", "connection_token"}:
                raise ConfigurationError("Module configuration contains unknown fields")
            return RuntimeConfig(
                buywell_url=str(data.get("buywell_url") or DEFAULT_BUYWELL_URL).rstrip("/"),
                connection_token=str(data.get("connection_token") or "").strip(),
            ).validate()

    def save(self, config: RuntimeConfig) -> None:
        config.validate()
        payload = {
            "buywell_url": config.buywell_url.rstrip("/"),
            "connection_token": config.connection_token,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def set_token(self, token: str) -> RuntimeConfig:
        current = self.load()
        updated = RuntimeConfig(current.buywell_url, token.strip()).validate()
        self.save(updated)
        return updated

    def clear_token(self) -> RuntimeConfig:
        current = self.load()
        updated = RuntimeConfig(current.buywell_url, "")
        self.save(updated)
        return updated
