from work_history import (
    DEFAULT_WORK_EXPERIENCE,
    normalize_work_experience,
    work_experience_for,
)

fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ── Fallback: an account that has never saved a work history ────────────────
seed = work_experience_for({})
check("empty settings falls back to the seed list", seed[0]["company"] == "Harrah's Casino")
check("seed is not aliased (caller cannot mutate the module constant)",
      seed[0] is not DEFAULT_WORK_EXPERIENCE[0])
check("None settings is safe", work_experience_for(None)[0]["company"] == "Harrah's Casino")
check("garbage settings falls back", work_experience_for({"work_experience": "nope"})[0]["company"] == "Harrah's Casino")
check("empty list falls back", work_experience_for({"work_experience": []})[0]["company"] == "Harrah's Casino")

# ── Saved history wins over the seed ────────────────────────────────────────
saved = work_experience_for({"work_experience": [
    {"company": "Cholo Tech", "title": "Freelance Developer",
     "start_date": "2022-07", "end_date": "2026-01", "current": False},
]})
check("saved history replaces the seed entirely", len(saved) == 1 and saved[0]["company"] == "Cholo Tech")

# ── Ordering: [0] drives 'current employer' on every application form ───────
mixed = normalize_work_experience([
    {"company": "Old Gig",  "title": "Dev",       "start_date": "2019-06", "end_date": "2020-08", "current": False},
    {"company": "Cholo Tech", "title": "Freelance Dev", "start_date": "2022-07", "end_date": "2026-01", "current": False},
    {"company": "Harrah's Casino", "title": "Bartender", "start_date": "2024-07", "end_date": "", "current": True},
])
check("the entry marked current leads", mixed[0]["company"] == "Harrah's Casino",
      "got " + mixed[0]["company"])
check("past jobs follow newest-first",
      [e["company"] for e in mixed[1:]] == ["Cholo Tech", "Old Gig"],
      str([e["company"] for e in mixed[1:]]))

# A current job with an older start date still leads — 'current' outranks date.
lead = normalize_work_experience([
    {"company": "Recent Past", "title": "X", "start_date": "2025-01", "end_date": "2025-09", "current": False},
    {"company": "Ongoing",     "title": "Y", "start_date": "2020-01", "end_date": "",        "current": True},
])
check("current outranks a more recent past job", lead[0]["company"] == "Ongoing")

# ── The honesty guard: a blank end date must NOT imply 'current' ────────────
amb = normalize_work_experience([
    {"company": "Ended But Blank", "title": "X", "start_date": "2022-07", "end_date": "", "current": False},
])
check("blank end date does not silently become 'current'", amb[0]["current"] is False)

# ── Junk handling ──────────────────────────────────────────────────────────
junk = normalize_work_experience([
    {"company": "", "title": ""},          # blank row the user never filled
    "not a dict",
    {"company": "Real Co", "title": "Real Title"},
    None,
])
check("blank and malformed rows are dropped", len(junk) == 1 and junk[0]["company"] == "Real Co")
check("non-list input returns empty", normalize_work_experience("nope") == [])
check("every entry has the full field set",
      all(set(e) == {"company", "title", "start_date", "end_date", "current", "location", "address"}
          for e in mixed))
check("current is always a real bool", all(isinstance(e["current"], bool) for e in mixed))

# A row with only a title (no company) is still real data — keep it.
title_only = normalize_work_experience([{"title": "Freelance Developer"}])
check("title-only row is kept", len(title_only) == 1)

print()
print(("FAILED: " + ", ".join(fails)) if fails else "All work-history assertions passed")
raise SystemExit(1 if fails else 0)
