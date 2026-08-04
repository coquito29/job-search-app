"""Work history for the autofill profile.

Until now this list lived as a literal inside ``_build_full_profile()`` in
app.py, which meant editing your work history in the app did nothing — the
hardcoded value won every time. Same shape as the bug where saved answers were
overridden by hardcoded constants.

Now the saved copy in ``profile.settings.work_experience`` wins, and the list
below is only a seed for an account that has never saved one.

Ordering is not cosmetic. The Chrome extension reads ``work_experience[0]`` for
"current employer", "current title" and "are you currently employed?", so
whichever entry the user marked current has to lead.
"""

# Seed only. Edit your work history in the app, not here.
DEFAULT_WORK_EXPERIENCE = [
    {
        "company":    "Harrah's Casino",
        "title":      "Bartender",
        "start_date": "2024-07",
        "end_date":   "",
        "current":    True,
        "location":   "Atlantic City, NJ",
        "address":    "777 Harrah's Blvd, Atlantic City, NJ 08401",
    },
    {
        "company":    "Ocean Casino Resort",
        "title":      "Casino Shift Manager",
        "start_date": "2021-06",
        "end_date":   "2022-07",
        "current":    False,
        "location":   "Atlantic City, NJ",
        "address":    "500 Boardwalk, Atlantic City, NJ 08401",
    },
    {
        "company":    "Ocean Casino Resort",
        "title":      "Bartender",
        "start_date": "2019-06",
        "end_date":   "2020-08",
        "current":    False,
        "location":   "Atlantic City, NJ",
        "address":    "500 Boardwalk, Atlantic City, NJ 08401",
    },
]


def normalize_work_experience(raw):
    """Clean and order a saved work-history list. Returns [] if unusable.

    Deliberately does NOT infer ``current`` from a blank end date. A missing
    end date is ambiguous, and guessing wrong here puts a false "current
    employer" on a real job application — that is the user's claim to make,
    not ours.
    """
    if not isinstance(raw, list):
        return []

    cleaned = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        company = str(item.get("company") or "").strip()[:120]
        title = str(item.get("title") or "").strip()[:120]
        if not company and not title:
            continue  # a row with neither is a blank the user never filled in
        cleaned.append({
            "company":    company,
            "title":      title,
            "start_date": str(item.get("start_date") or "").strip()[:10],
            "end_date":   str(item.get("end_date") or "").strip()[:10],
            "current":    bool(item.get("current")),
            "location":   str(item.get("location") or "").strip()[:120],
            "address":    str(item.get("address") or "").strip()[:200],
        })

    # Two stable passes: newest first, then float the current job(s) to the top.
    # ISO-ish "YYYY-MM" strings sort correctly as text; a blank start sorts last.
    cleaned.sort(key=lambda e: e["start_date"], reverse=True)
    cleaned.sort(key=lambda e: not e["current"])

    # At most one job can be current. Two would make work_experience[0] depend
    # on sort tie-breaking, and that entry is what fills "current employer" on
    # a real application. The most recent one wins; the rest are demoted.
    seen_current = False
    for entry in cleaned:
        if entry["current"] and seen_current:
            entry["current"] = False
        elif entry["current"]:
            seen_current = True
    return cleaned


def work_experience_for(settings):
    """Saved work history if the user has one, otherwise the seed list."""
    saved = normalize_work_experience((settings or {}).get("work_experience"))
    return saved or [dict(e) for e in DEFAULT_WORK_EXPERIENCE]
