from location_filter import classify_location

# (location string, expected bucket) — drawn from real digest entries plus the
# ambiguity traps that broke the old US-state whitelist.
CASES = [
    # ── The roles that prompted this: remote, and unusable ──────────────
    ("Poznań, Poland",                          "NON_US"),
    ("Poland",                                  "NON_US"),
    ("Malta",                                   "NON_US"),
    ("Bucharest, Romania",                      "NON_US"),
    ("Bengaluru, India",                        "NON_US"),
    ("Almaty, Kazakhstan",                      "NON_US"),
    ("Remote - Europe",                         "NON_US"),
    ("EMEA",                                    "NON_US"),
    ("Remote (APAC)",                           "NON_US"),
    ("LATAM - Remote",                          "NON_US"),
    ("Toronto, ON",                             "NON_US"),
    ("Vancouver, Canada",                       "NON_US"),
    ("London, United Kingdom",                  "NON_US"),
    ("Manila, Philippines",                     "NON_US"),
    ("Mexico City, Mexico",                     "NON_US"),
    ("Tel Aviv, Israel",                        "NON_US"),
    ("São Paulo, Brazil",                       "NON_US"),

    # ── Should never be penalized: the 8 fast-apply matches' shapes ─────
    ("United States",                           "US"),
    ("USA",                                     "US"),
    ("US",                                      "US"),
    ("U.S.",                                    "US"),
    ("Remote, US",                              "US"),
    ("Remote - United States",                  "US"),
    ("US-based",                                "US"),
    ("Austin, TX",                              "US"),
    ("New York, NY",                            "US"),
    ("Phoenix, AZ",                             "US"),
    ("Boston, Massachusetts",                   "US"),
    ("Atlanta, Georgia",                        "US"),
    ("Washington, DC",                          "US"),
    ("San Juan, PR",                            "US"),
    ("Nationwide",                              "US"),
    ("North America",                           "US"),

    # ── Ambiguity traps ────────────────────────────────────────────────
    # "New Mexico" must not read as Mexico.
    ("Albuquerque, New Mexico",                 "US"),
    ("Santa Fe, NM",                            "US"),
    # A posting open to both is one he CAN apply to — US wins.
    ("Remote - US/EU",                          "US"),
    ("New York, NY / London, UK",               "US"),
    ("Remote (US, Canada)",                     "US"),
    # Bare state codes that are also English words — must not fire on prose,
    # but must still work in the "City, XX" shape.
    ("Portland, OR",                            "US"),
    ("Indianapolis, IN",                        "US"),
    ("Tulsa, OK",                               "US"),
    ("Portland, ME",                            "US"),
    ("Honolulu, HI",                            "US"),
    ("Wilmington, DE",                          "US"),
    # "India" must not swallow "Indiana".
    ("Indiana",                                 "US"),

    # ── Unknown: no country signal at all. Must stay neutral, because the
    #    upstream fallback is literally `loc = loc or "Remote"`. ─────────
    ("Remote",                                  "UNKNOWN"),
    ("",                                        "UNKNOWN"),
    (None,                                      "UNKNOWN"),
    ("Anywhere",                                "UNKNOWN"),
    ("Worldwide",                               "UNKNOWN"),
    ("Fully Remote",                            "UNKNOWN"),
    ("Remote - Work from home",                 "UNKNOWN"),
]

cls_fails = []
for loc, expected in CASES:
    got, label = classify_location(loc)
    mark = "ok " if got == expected else "FAIL"
    if got != expected:
        cls_fails.append((loc, expected, got))
    print(f"{mark} {str(loc)!r:38} -> {got:8} {label or ''}")

print()
print(f"{len(CASES) - len(cls_fails)}/{len(CASES)} passed")
for loc, exp, got in cls_fails:
    print(f"  FAIL {loc!r}: expected {exp}, got {got}")



print()
# Mirrors the sort in /api/search after the change.
def sort_jobs(jobs):
    for j in jobs:
        j["loc_class"], _ = classify_location(j["location"])
    return sorted(jobs, key=lambda j: (j.get("loc_class") == "NON_US", -j["match_pct"]))


# The exact complaint: a strong EU match outranking weaker US ones.
jobs = [
    {"co": "Growe (Poland)",   "location": "Poznań, Poland", "match_pct": 94},
    {"co": "EU agency",        "location": "Remote - Europe", "match_pct": 88},
    {"co": "MedWatch",         "location": "Remote, US",      "match_pct": 71},
    {"co": "Bengaluru shop",   "location": "Bengaluru, India","match_pct": 90},
    {"co": "AccuWeather",      "location": "State College, PA","match_pct": 62},
    {"co": "Standard Bots",    "location": "Remote",          "match_pct": 55},
    {"co": "Toronto co",       "location": "Toronto, ON",     "match_pct": 85},
    {"co": "iT1",              "location": "Tempe, AZ",       "match_pct": 48},
]

ordered = sort_jobs(jobs)
print("Order after the change:\n")
for i, j in enumerate(ordered, 1):
    print(f"{i}. {j['match_pct']:>3}%  {j['co']:<16} {j['location']:<22} [{j['loc_class']}]")

applyable = [j for j in ordered if j["loc_class"] != "NON_US"]
blocked   = [j for j in ordered if j["loc_class"] == "NON_US"]

sort_fails = []
# 1. Every applyable role outranks every non-US one.
split = len(applyable)
if [j["loc_class"] == "NON_US" for j in ordered] != [False] * split + [True] * len(blocked):
    sort_fails.append("non-US roles are not all parked at the bottom")
# 2. Within each group, score order is preserved.
for group, name in ((applyable, "applyable"), (blocked, "non-US")):
    pcts = [j["match_pct"] for j in group]
    if pcts != sorted(pcts, reverse=True):
        sort_fails.append(f"{name} group not sorted by score: {pcts}")
# 3. Nothing was dropped.
if len(ordered) != len(jobs):
    sort_fails.append("jobs were dropped by the sort")
# 4. An unknown location is NOT demoted.
if any(j["co"] == "Standard Bots" for j in blocked):
    sort_fails.append("bare 'Remote' was wrongly demoted")

print()
print("\n".join(f"  FAIL {f}" for f in sort_fails) if sort_fails else "All sort assertions passed")


raise SystemExit(1 if (cls_fails or sort_fails) else 0)
