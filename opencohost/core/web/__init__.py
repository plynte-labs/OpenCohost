"""Simple web lookups for owner turns (web-fetch-laya-20261009).

A plain HTTPS search and fetch, no browser: ``search`` parses DuckDuckGo's
HTML results, ``fetch`` downloads a page behind an SSRF guard, ``extract``
turns HTML into short text chunks, ``rank`` lets Laya pick the useful links
and chunks, and ``lookup`` runs the whole step inside one time budget.
"""
