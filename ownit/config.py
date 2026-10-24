# @exports: ConfigError(ValueError)
# @exports: Config(host: str, port: int, state: str|None) [frozen dataclass]
# @exports: load_config(environ: Mapping[str, str]|None = None) -> Config   # the ONLY place environment variables are read; raises ConfigError
# @env: OWNIT_HOST
# @env: OWNIT_PORT
# @env: OWNIT_STATE
# @schema: defaults host="127.0.0.1", port=8765, state=None (None => <scanned root>/.ownit/state.json, decided by server.py)
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class ConfigError(ValueError):
    """Raised when an OWNIT_* environment variable is invalid."""


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    state: str | None


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ

    host = env.get("OWNIT_HOST", DEFAULT_HOST).strip()
    if not host:
        raise ConfigError(
            "OWNIT_HOST is set but empty. Set it to an interface such as 127.0.0.1, or unset it."
        )

    raw_port = env.get("OWNIT_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(
            f"OWNIT_PORT={raw_port!r} is not an integer. Set it to a number from 0 to 65535 (0 picks a free port)."
        ) from None
    if not 0 <= port <= 65535:
        raise ConfigError(f"OWNIT_PORT={port} is out of range. Use a number from 0 to 65535.")

    state_raw = env.get("OWNIT_STATE")
    state: str | None = None
    if state_raw is not None:
        state = state_raw.strip()
        if not state:
            raise ConfigError(
                "OWNIT_STATE is set but empty. Set it to a file path such as /tmp/ownit-state.json, or unset it."
            )
        if os.path.isdir(state):
            raise ConfigError(
                f"OWNIT_STATE={state!r} is a directory. Point it at a file path, for example {state.rstrip('/')}/state.json."
            )

    return Config(host=host, port=port, state=state)
