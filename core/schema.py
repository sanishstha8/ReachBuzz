"""
Schema generation hooks.

The API is served at two prefixes — ``/api/v1/`` and, for the integrations that
predate versioning, the bare ``/api/``. Both are real and both are supported,
but documenting both would double every endpoint in the reference and leave a
reader deciding which of two identical entries to use.

So the schema describes v1 only. That is the address this project would like a
new client to use, and a documented API with one obvious form is worth more than
an exhaustive one with two.
"""

from __future__ import annotations

VERSIONED_PREFIX = "/api/v1/"

#: Paths outside the versioned tree that still belong in the reference, because
#: they are part of using the API rather than an unversioned duplicate of it.
ALWAYS_INCLUDE = ("/api/schema",)


def only_versioned_endpoints(endpoints, **kwargs):
    """
    Drop the unversioned aliases from the generated schema.

    A preprocessing hook rather than an ``exclude_paths`` list: the aliases are
    generated from the same urlconf as the real routes, so anything hand-written
    here would drift the moment somebody adds an endpoint.
    """
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if path.startswith(VERSIONED_PREFIX) or path.startswith(ALWAYS_INCLUDE)
    ]
