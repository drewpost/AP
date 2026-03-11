"""Shared location filtering for all discovery modules."""

# Patterns that indicate a remote job is available in the UK.
# If a remote job mentions a specific country/region and NONE of these match,
# it's rejected. Pure "Remote" with no qualifier is accepted.
REMOTE_ACCEPT = [
    "uk", "united kingdom", "britain", "england", "london", "surrey",
    "emea", "europe", "eu", "eea",
    "worldwide", "global", "anywhere", "international",
]


def location_ok(location: str | None, accept: list[str], reject: list[str],
                remote_reject: list[str] | None = None) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs: accepted only if they DON'T specify a non-UK country/region.
    Uses an accept-list approach — if the remote location mentions a specific
    place and it's not UK/EMEA/global, it's rejected.

    Non-remote jobs must match an accept pattern and not match a reject pattern.
    """
    if not location:
        return True

    loc = location.lower()

    is_remote = any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed"))

    if is_remote:
        # Strip out the remote keywords to see what's left
        remainder = loc
        for r in ("remote", "anywhere", "work from home", "wfh", "distributed"):
            remainder = remainder.replace(r, "")
        # Strip punctuation and whitespace
        remainder = remainder.strip(" -–—/,;:()")

        # Pure "Remote" with no geo qualifier — accept
        if not remainder:
            return True

        # Check if remainder mentions UK or broad region we can work in
        for pattern in REMOTE_ACCEPT:
            if pattern in remainder:
                return True

        # Has a geo qualifier that's not UK-compatible — reject
        return False

    # Non-remote: reject matches
    for r in reject:
        if r.lower() in loc:
            return False

    # Non-remote: accept matches
    for a in accept:
        if a.lower() in loc:
            return True

    return False
