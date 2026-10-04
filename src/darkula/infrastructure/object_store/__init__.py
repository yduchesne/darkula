# SPDX-License-Identifier: AGPL-3.0-only
"""ObjectStore implementations.

PR 3 ships the deterministic, offline stores: :class:`InMemoryObjectStore`
and :class:`LocalFileObjectStore`. PR 8 adds :class:`S3CompatibleObjectStore`,
the provider-neutral production adapter used by both the S3 and R2 drivers.
"""

from darkula.infrastructure.object_store.in_memory import InMemoryObjectStore
from darkula.infrastructure.object_store.local import LocalFileObjectStore
from darkula.infrastructure.object_store.s3 import S3CompatibleObjectStore

__all__ = [
    "InMemoryObjectStore",
    "LocalFileObjectStore",
    "S3CompatibleObjectStore",
]
