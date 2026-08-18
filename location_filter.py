import re

# ── US work-eligibility: location classification ──────────────────────────────
# George needs US work authorization. The Apify task filters for *remote*, but
# "remote" says nothing about WHICH country — the Poznań, Malta, Bucharest and
# Bengaluru roles that keep topping the digest are all genuinely remote and all
# unusable, and you only find that out at the application form.
#
# An earlier version of this file filtered non-US jobs out entirely with a
# US-state whitelist and it over-fired, dropping good listings whose location
# was a bare city name (see the note in /api/search where that block was
# removed). So this DEMOTES instead of dropping, and every tie-break favours
# keeping the job:
#
#   any US signal            -> "US"       even alongside a foreign one, because
#                                          "Remote — US/EU" is applyable
#   foreign signal, no US    -> "NON_US"   scoring penalty + a visible tag
#   neither                  -> "UNKNOWN"  no penalty. `loc` falls back to the
#                                          bare word "Remote" upstream, so this
#                                          bucket is large; penalizing it would
#                                          repeat the old over-filter bug.

_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    "florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    "louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    "missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    "virginia|washington|west virginia|wisconsin|wyoming"
)

# Lowercased-input patterns.
_US_SIGNAL_RE = re.compile(
    r"\bunited states\b|\bu\.?s\.?a\b|\bus[-\s]based\b|\bus[-\s]only\b|"
    r"\bus[-\s]remote\b|\bremote[-,\s]+us\b|\bnorth america\b|\bamericas\b|"
    r"\bpuerto rico\b|\bdistrict of columbia\b|\bwashington,?\s*d\.?c\.?\b|"
    # US territories — work authorization carries, so these are applyable.
    # "virgin islands" is guarded because the British ones are not.
    r"\bamerican samoa\b|\bguam\b|\bnorthern mariana\b|"
    r"(?<!british )\bvirgin islands\b|"
    r"\bnationwide\b|\bcontinental us\b|\blower 48\b|\bstateside\b|"
    r"\b(?:" + _US_STATES + r")\b"
)

# Case-SENSITIVE, on the original string: bare "US" / "U.S." only, never the
# English word "us". Same for state codes — lowercasing first would make "or",
# "in", "ok", "me", "hi" and "de" fire on ordinary prose.
_US_ABBR_RE = re.compile(r"\bU\.?S\.?(?![A-Za-z])")
_US_STATE_CODE_RE = re.compile(
    r"(?:^|[,(\-/]\s*)("
    r"AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|"
    r"MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|PR|RI|SC|SD|TN|TX|UT|VA|VT|"
    r"WA|WI|WV|WY"
    r")(?![A-Za-z])"
)

# Foreign markers. Value is the label shown in the UI.
_NON_US_COUNTRIES = {
    r"poland|polska": "Poland",
    r"malta": "Malta",
    r"romania": "Romania",
    r"india": "India",
    r"kazakhstan": "Kazakhstan",
    r"canada|ontario, canada|british columbia": "Canada",
    r"united kingdom|great britain|england|scotland|wales|northern ireland|uk": "UK",
    r"ireland|eire": "Ireland",
    r"germany|deutschland": "Germany",
    r"france": "France",
    r"spain|espa\w+a": "Spain",
    r"portugal": "Portugal",
    r"netherlands|holland": "Netherlands",
    r"belgium": "Belgium",
    r"italy|italia": "Italy",
    r"greece": "Greece",
    r"switzerland": "Switzerland",
    r"austria": "Austria",
    r"sweden": "Sweden",
    r"norway": "Norway",
    r"denmark": "Denmark",
    r"finland": "Finland",
    r"iceland": "Iceland",
    r"czech\w*|czechia": "Czechia",
    r"slovakia": "Slovakia",
    r"hungary": "Hungary",
    r"bulgaria": "Bulgaria",
    r"serbia": "Serbia",
    r"croatia": "Croatia",
    r"slovenia": "Slovenia",
    r"lithuania": "Lithuania",
    r"latvia": "Latvia",
    r"estonia": "Estonia",
    r"ukraine": "Ukraine",
    r"belarus": "Belarus",
    r"moldova": "Moldova",
    r"cyprus": "Cyprus",
    r"luxembourg": "Luxembourg",
    r"albania": "Albania",
    r"turkey|t\w*rkiye": "Turkey",
    r"armenia": "Armenia",
    r"azerbaijan": "Azerbaijan",
    r"uzbekistan": "Uzbekistan",
    r"israel": "Israel",
    r"united arab emirates|u\.a\.e|uae": "UAE",
    r"saudi arabia": "Saudi Arabia",
    r"qatar": "Qatar",
    r"egypt": "Egypt",
    r"morocco": "Morocco",
    r"tunisia": "Tunisia",
    r"nigeria": "Nigeria",
    r"kenya": "Kenya",
    r"ghana": "Ghana",
    r"south africa": "South Africa",
    r"ethiopia": "Ethiopia",
    r"uganda": "Uganda",
    r"tanzania": "Tanzania",
    r"rwanda": "Rwanda",
    r"pakistan": "Pakistan",
    r"bangladesh": "Bangladesh",
    r"sri lanka": "Sri Lanka",
    r"nepal": "Nepal",
    r"philippines": "Philippines",
    r"indonesia": "Indonesia",
    r"malaysia": "Malaysia",
    r"singapore": "Singapore",
    r"thailand": "Thailand",
    r"vietnam|viet nam": "Vietnam",
    r"cambodia": "Cambodia",
    r"japan": "Japan",
    r"china|hong kong": "China",
    r"taiwan": "Taiwan",
    r"south korea|republic of korea": "South Korea",
    r"mongolia": "Mongolia",
    r"australia": "Australia",
    r"new zealand": "New Zealand",
    # "New Mexico" is a US state — never let it read as Mexico.
    r"(?<!new )mexico": "Mexico",
    r"brazil|brasil": "Brazil",
    r"argentina": "Argentina",
    r"colombia": "Colombia",
    r"chile": "Chile",
    r"peru": "Peru",
    r"ecuador": "Ecuador",
    r"uruguay": "Uruguay",
    r"paraguay": "Paraguay",
    r"bolivia": "Bolivia",
    r"venezuela": "Venezuela",
    r"costa rica": "Costa Rica",
    r"panama": "Panama",
    r"guatemala": "Guatemala",
    r"honduras": "Honduras",
    r"nicaragua": "Nicaragua",
    r"el salvador": "El Salvador",
    r"dominican republic": "Dominican Republic",

    # ── Second pass (2026-08) ────────────────────────────────────────────────
    # An Amman, Jordan role topped the results at 22% match because "Jordan"
    # was missing here: unlisted countries fall through to UNKNOWN, which
    # carries no penalty, so they sort as if they were domestic. A sweep of 65
    # countries found 62 unlisted. Ambiguity with US place names is handled for
    # free — classify_location() checks US signals FIRST and returns on a hit,
    # so "Lebanon, PA", "Jordan, MN", "Angola, IN" and "Palestine, TX" all
    # resolve as US before these patterns are ever consulted.
    #
    # Deliberately NOT listed:
    #   georgia — a US state, already matched by _US_STATES, so an entry here
    #             would be unreachable. Its capital is caught by
    #             _FOREIGN_CITY_RE below, which runs ahead of the US check.
    #   jersey  — "Jersey City" is in New Jersey and often written without a
    #             state code; the island is rare enough not to be worth it.

    # Middle East
    r"jordan": "Jordan",
    r"lebanon": "Lebanon",
    r"kuwait": "Kuwait",
    r"bahrain": "Bahrain",
    r"oman": "Oman",
    r"iraq": "Iraq",
    r"iran": "Iran",
    r"syria": "Syria",
    r"yemen": "Yemen",
    r"palestine|west bank|gaza": "Palestine",

    # Caucasus, Russia and Central Asia
    r"russia|russian federation": "Russia",
    r"afghanistan": "Afghanistan",
    r"kyrgyzstan": "Kyrgyzstan",
    r"tajikistan": "Tajikistan",
    r"turkmenistan": "Turkmenistan",

    # Rest of Asia
    r"myanmar|burma": "Myanmar",
    r"laos": "Laos",
    r"brunei": "Brunei",
    r"maldives": "Maldives",
    r"bhutan": "Bhutan",

    # Africa
    r"senegal": "Senegal",
    r"cameroon": "Cameroon",
    r"ivory coast|c\w*te d'?ivoire": "Ivory Coast",
    r"zambia": "Zambia",
    r"zimbabwe": "Zimbabwe",
    r"botswana": "Botswana",
    r"namibia": "Namibia",
    r"mozambique": "Mozambique",
    r"angola": "Angola",
    r"algeria": "Algeria",
    r"libya": "Libya",
    r"sudan": "Sudan",
    r"somalia": "Somalia",
    r"mali": "Mali",
    r"malawi": "Malawi",
    r"madagascar": "Madagascar",
    r"mauritius": "Mauritius",
    r"congo": "Congo",
    r"gabon": "Gabon",
    r"benin": "Benin",
    r"togo": "Togo",

    # Balkans and European micro-states
    r"kosovo": "Kosovo",
    r"montenegro": "Montenegro",
    r"bosnia|herzegovina": "Bosnia",
    r"macedonia": "North Macedonia",
    r"andorra": "Andorra",
    r"monaco": "Monaco",
    r"liechtenstein": "Liechtenstein",
    r"san marino": "San Marino",
    r"gibraltar": "Gibraltar",
    r"guernsey": "Guernsey",
    r"isle of man": "Isle of Man",

    # Caribbean and the rest of the Americas
    r"jamaica": "Jamaica",
    r"trinidad|tobago": "Trinidad and Tobago",
    r"barbados": "Barbados",
    r"bahamas": "Bahamas",
    r"haiti": "Haiti",
    r"cuba": "Cuba",
    r"belize": "Belize",
    r"guyana": "Guyana",
    r"suriname": "Suriname",
    r"british virgin islands": "British Virgin Islands",
    r"cayman": "Cayman Islands",
    r"bermuda": "Bermuda",
    r"aruba": "Aruba",
    r"cura\w*ao": "Curaçao",

    # Oceania — "american samoa" is a US territory, so require a bare "samoa"
    r"fiji": "Fiji",
    r"papua new guinea": "Papua New Guinea",
    r"(?<!american )samoa": "Samoa",
    r"tonga": "Tonga",
    r"vanuatu": "Vanuatu",
}

_NON_US_REGIONS = {
    r"emea": "EMEA",
    r"apac|asia[-\s]pacific": "APAC",
    r"latam|latin america": "LATAM",
    r"europe|european union|eu": "Europe",
    r"middle east": "Middle East",
    r"south america": "South America",
    r"\bafrica\b": "Africa",
}

_NON_US_COMPILED = [
    (re.compile(r"\b(?:" + pat + r")\b"), label)
    for pat, label in list(_NON_US_COUNTRIES.items()) + list(_NON_US_REGIONS.items())
]

# Case-sensitive: Canadian provinces and the EU country code, in the same
# "City, XX" shape as US state codes.
_NON_US_CODE_RE = re.compile(
    r"(?:^|[,(\-/]\s*)(ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)(?![A-Za-z])"
)

# Cities whose country shares a name with a US state, so the country itself
# can't be listed above — "Tbilisi, Georgia" would match the state list and
# read as domestic. Checked BEFORE the US signals; keep this list to names
# that exist nowhere in the US.
_FOREIGN_CITY_COMPILED = [
    (re.compile(r"\b(?:" + pat + r")\b"), label)
    for pat, label in {
        r"tbilisi|batumi": "Georgia (country)",
    }.items()
]


def classify_location(loc):
    """Return ("US" | "NON_US" | "UNKNOWN", country_label_or_None).

    A US signal anywhere in the string wins outright — a posting open to both
    the US and somewhere else is one George can actually apply to. The one
    exception is _FOREIGN_CITY_RE, checked first: those cities sit in a country
    whose name IS a US state, so the US check would claim them every time.
    """
    if not loc:
        return "UNKNOWN", None
    raw = str(loc).strip()
    low = raw.lower()

    for rx, label in _FOREIGN_CITY_COMPILED:
        if rx.search(low):
            return "NON_US", label

    if _US_SIGNAL_RE.search(low) or _US_ABBR_RE.search(raw) or _US_STATE_CODE_RE.search(raw):
        return "US", None

    for rx, label in _NON_US_COMPILED:
        if rx.search(low):
            return "NON_US", label
    if _NON_US_CODE_RE.search(raw):
        return "NON_US", "Canada"

    return "UNKNOWN", None
