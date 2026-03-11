"""Shared location filtering for all discovery modules."""

import re

# Countries, regions, US states, and cities that indicate a remote job
# is NOT available in the UK.
DEFAULT_REMOTE_REJECT = [
    "usa", "united states", "u.s.", "america", "american",
    "canada", "canadian",
    "brazil", "brasil",
    "mexico", "méxico",
    "india", "indian",
    "australia", "australian",
    "new zealand",
    "singapore", "singaporean",
    "japan", "japanese",
    "china", "chinese",
    "korea", "korean",
    "vietnam", "vietnamese",
    "thailand", "thai",
    "philippines", "filipino",
    "indonesia", "indonesian",
    "malaysia", "malaysian",
    "taiwan",
    "hong kong",
    "colombia", "colombian",
    "argentina", "chile", "peru",
    "costa rica", "puerto rico",
    "south africa",
    "nigeria",
    "egypt",
    "israel",
    # US states commonly seen in remote job locations
    "california", "new york", "texas", "florida", "illinois",
    "washington dc", "virginia", "georgia", "massachusetts",
    "colorado", "arizona", "oregon", "pennsylvania", "ohio",
    "michigan", "minnesota", "north carolina", "new jersey",
    "connecticut", "maryland", "wisconsin", "missouri",
    "tennessee", "alabama", "louisiana", "kentucky",
    "boston", "san francisco", "seattle", "chicago", "denver",
    "austin", "atlanta", "miami", "portland", "phoenix",
    "los angeles", "dallas", "houston", "charlotte",
    "raleigh", "nashville", "detroit", "minneapolis",
    "toronto", "vancouver", "montreal", "ottawa",
    "mumbai", "bangalore", "hyderabad", "delhi", "pune",
    "são paulo", "bogotá", "buenos aires", "santiago",
    "sydney", "melbourne", "auckland",
]


def location_ok(location: str | None, accept: list[str], reject: list[str],
                remote_reject: list[str] | None = None) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs are accepted only if they don't specify a non-UK country.
    Non-remote jobs must match an accept pattern and not match a reject pattern.
    """
    if not location:
        return True

    loc = location.lower()

    is_remote = any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed"))

    if is_remote:
        # Check for bare "US" / "UK-only exclusion" with word boundaries
        # so we don't false-positive on "focus", "campus", etc.
        if re.search(r'\bUS\b', location):  # case-sensitive: "US" but not "us" in "focus"
            return False
        reject_patterns = remote_reject if remote_reject else DEFAULT_REMOTE_REJECT
        for pattern in reject_patterns:
            if pattern.lower() in loc:
                return False
        return True

    for r in reject:
        if r.lower() in loc:
            return False

    for a in accept:
        if a.lower() in loc:
            return True

    return False
