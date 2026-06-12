# deep_link.py
#
# Copyright (C) 2026 Tyler Reece
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Parse ``jellyfin://`` deep-link URIs into a bare item id.

No gi/GTK imports — intentionally headless-testable so the pytest suite covers
the grammar directly.

Accepted shapes (be liberal; extract the item GUID and ignore the rest):

* ``jellyfin://items/<id>``         (collection-style path)
* ``jellyfin://item/<id>``          (singular variant)
* ``jellyfin://<id>``               (bare id as host)
* any of the above with ``?id=<id>``/``&serverId=...`` query noise — an
  explicit ``id=`` query parameter WINS over a path token
* the Jellyfin web URL shape a user might copy from a browser:
  ``https://host/web/#/details?id=<id>&serverId=...``

The "id" we hand back is a Jellyfin item GUID: a 32-hex-char token (with or
without dashes). We validate the extracted token against that shape so query
junk / slugs don't get mistaken for an id. Returns ``None`` for anything we
can't pull a plausible id out of.
"""

import re
from urllib.parse import urlsplit, parse_qs

# Jellyfin item ids are 32 hex chars, optionally dash-grouped (8-4-4-4-12).
# Accept both the packed form and the canonical dashed UUID form.
_ID_RE = re.compile(
    r"^[0-9a-fA-F]{32}$"
    r"|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Strip dashes when normalising for the return value so callers can match the
# packed ids the db stores; matching is done case-insensitively by callers.
_DASH_RE = re.compile(r"-")


def _looks_like_id(token):
    """True if ``token`` is a Jellyfin item GUID (packed or dashed hex)."""
    return bool(token) and bool(_ID_RE.match(token))


def _normalise(token):
    """Return the packed (dash-stripped) id, or None if it isn't an id."""
    if not _looks_like_id(token):
        return None
    return _DASH_RE.sub("", token)


def parse_jellyfin_uri(uri):
    """Extract a Jellyfin item id from a deep-link / web URI.

    Returns the packed (dash-stripped) id string, or ``None`` if no plausible
    id is present. An explicit ``?id=`` query parameter takes precedence over a
    path/host token.
    """
    if not uri or not isinstance(uri, str):
        return None
    uri = uri.strip()
    if not uri:
        return None

    # urlsplit handles the query for jellyfin:// and https:// alike. For the
    # web "fragment" shape (.../web/#/details?id=...), the id lives in the
    # fragment's own query string, so parse that separately too.
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None

    # 1) Explicit id= query param wins (covers both ?id= and the &id= noise
    #    case, and tolerates serverId / other params alongside it).
    for source in (parts.query, parts.fragment):
        if not source:
            continue
        # The fragment may itself be "/details?id=...": split off its query.
        frag_query = source.split("?", 1)[1] if "?" in source else source
        qs = parse_qs(frag_query, keep_blank_values=False)
        for key in ("id", "Id", "itemId", "ItemId"):
            if key in qs and qs[key]:
                norm = _normalise(qs[key][0])
                if norm:
                    return norm

    # 2) Fall back to a path/host token. For jellyfin:// the host slot may hold
    #    the bare id (jellyfin://<id>); items/<id> / item/<id> put it in the
    #    path. Collect candidate tokens and return the first id-shaped one.
    candidates = []
    if parts.netloc:
        candidates.append(parts.netloc)
    if parts.path:
        candidates.extend(seg for seg in parts.path.split("/") if seg)
    # The web shape carries its id only in the fragment query (handled above);
    # but tolerate a fragment path token too just in case.
    if parts.fragment:
        frag_path = parts.fragment.split("?", 1)[0]
        candidates.extend(seg for seg in frag_path.split("/") if seg)

    for token in candidates:
        norm = _normalise(token)
        if norm:
            return norm

    return None
