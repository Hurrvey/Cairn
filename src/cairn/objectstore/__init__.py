"""Object storage.

Originals, parsed artifacts, icons, exports. One Protocol, several drivers, so a
deployment can run MinIO on a laptop and S3 in production without a code change.

Everything streams. A 200 MB PDF buffered in memory across eight concurrent
uploads is 1.6 GB of RSS and an OOM-killed worker, so ``put``/``get`` take and
return async iterators, and the one convenience that materialises bytes requires
an explicit cap.
"""

from cairn.objectstore.base import ObjectInfo, ObjectPage, ObjectStore
from cairn.objectstore.errors import ObjectNotFound, ObjectStoreUnavailable, ObjectTooLarge
from cairn.objectstore.keys import ObjectKeys

__all__ = [
    "ObjectInfo",
    "ObjectKeys",
    "ObjectNotFound",
    "ObjectPage",
    "ObjectStore",
    "ObjectStoreUnavailable",
    "ObjectTooLarge",
]
