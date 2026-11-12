"""The configuration of the packer service: a few values, from the environment.

nodo's ``src/utils/config.py`` loads ``config.yaml`` (and, on a first load, writes
one, with a wallet mnemonic and a gateway port). A service that does one thing has
no use for any of it, so this module keeps the interface that the rest of the
packer reads (``ConfigManager().get("a.b", default)``) and answers it from a short
table of defaults. ``start.sh`` and ``server.py`` set the environment variables
that name the directories.

A key that is not in the table answers the ``default`` of the caller, as nodo does
for a key that its ``config.yaml`` does not hold.

What decides the service id is not here. The hash is ``sha3_256`` (nodo's default,
``hashing.HASH``). The size above which a file is a block
(``packer.MIN_BUFFER_BLOCK_SIZE``) does not change the id: nodo documents that "an
object expands to the same bytes either way".
"""
import os
from typing import Any

from src.utils.singleton import Singleton


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _defaults() -> dict:
    nodo_dir = os.environ.get("NODO_DIR", "/opt/nodo")
    return {
        # Where nodo is installed. buildkit_env looks for bin/buildctl under it.
        "main.MAIN_DIR": nodo_dir,
        # Directories (start.sh exports them; server.py sets them too).
        "CACHE": os.environ.get("CACHE", "/var/lib/celaut/cache/"),
        "BLOCKDIR": os.environ.get("BLOCKDIR", "/var/lib/celaut/blocks/"),
        "STORAGE": os.environ.get("STORAGE", "/var/lib/celaut"),
        # The logger prints: the worker's output is the log of the service.
        "USE_PRINT": _bool_env("USE_PRINT", True),
        # The packer.
        "packer.ARM_PACKER_SUPPORT": True,
        "packer.X86_PACKER_SUPPORT": True,
        "packer.MIN_BUFFER_BLOCK_SIZE": _int_env("MIN_BUFFER_BLOCK_SIZE", 32768),
        "packer.WAIT_FOR_UNLOCK_MEMORY": _int_env("PACKER_WAIT_FOR_UNLOCK_MEMORY", 300),
        "packer.buildkit.DOCKERFILE_NAME": "Dockerfile",
        "PACKER_MEMORY_SIZE_FACTOR": _float_env("PACKER_MEMORY_SIZE_FACTOR", 6.0),
        "PACKER_MEMORY_PER_BLOCK": _int_env("PACKER_MEMORY_PER_BLOCK", 10_000),
        "PACKER_MEMORY_OVERHEAD": _int_env("PACKER_MEMORY_OVERHEAD", 40_000_000),
        "SAVE_ALL": _bool_env("SAVE_ALL", False),
        # The hash that identifies a service.
        "hashing.HASH": "sha3_256",
        # The BuildKit that start.sh runs in the guest (see buildkit_env).
        "dependencies.buildkit.BUILDKIT_SOCKET": os.environ.get(
            "BUILDKIT_SOCKET", os.path.join(nodo_dir, "buildkit", "buildkitd.sock")
        ),
    }


class ConfigManager(metaclass=Singleton):
    """Read-only view of the defaults above, with the interface of nodo's."""

    def __init__(self) -> None:
        self._config = _defaults()

    def get(self, key: str, default: Any = None) -> Any:
        value = self._config.get(key)
        return value if value is not None else default
