"""Decides which listings are in scope (SWE / AI internships, Summer 2027, US) using config['filters']."""
from __future__ import annotations

import re
import hashlib
import json
from functools import lru_cache

from .models import Job
from .sources.common import explicit_season_terms

FILTER_POLICY_REVISION = 1  # Bump when filter rule semantics change.

INTERN_RE = re.compile(r"\bintern(s|ship|ships)?\b", re.I)
SWE_WORDS = ["software", "swe", "developer", "backend", "back end", "back-end", "frontend", "front end",
             "front-end", "full stack", "full-stack", "fullstack", "platform", "infrastructure", "mobile",
             "ios", "android"]

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia", "PR": "puerto rico",
}
US_CITY_ALIASES = {"sf", "nyc", "south sf", "dc", "bay area", "sf bay area"}
US_CITIES = [
    "new york", "san francisco", "seattle", "boston", "austin", "chicago", "los angeles", "palo alto",
    "mountain view", "menlo park", "sunnyvale", "san jose", "santa clara", "redmond", "bellevue", "cupertino",
    "san mateo", "san diego", "denver", "atlanta", "miami", "dallas", "houston", "philadelphia", "pittsburgh",
    "washington d.c", "washington, d.c", "arlington", "reston", "raleigh", "durham", "salt lake city",
    "phoenix", "detroit", "minneapolis", "nashville", "boulder", "irvine", "oakland", "berkeley",
    "foster city", "redwood city", "brooklyn", "manhattan", "jersey city", "princeton", "ann arbor",
    "baltimore", "st. louis", "kansas city", "charlotte", "silicon valley",
]
US_COUNTRY_RE = re.compile(r"(?<![a-z])(usa|us|u\.s\.(a\.)?|united states)(?![a-z])", re.I)
NON_US_RE = re.compile(
    r"\b(canada|uk|u\.k\.|united kingdom|england|scotland|wales|ireland|india|germany|france|spain|"
    r"netherlands|switzerland|singapore|australia|japan|china|hong kong|israel|poland|brazil|sweden|"
    r"denmark|norway|finland|serbia|uae|united arab emirates|korea|taiwan|italy|portugal|belgium|austria|"
    r"romania|czech|philippines|vietnam|argentina|colombia|chile|new zealand|luxembourg|estonia|greece|"
    r"turkey|egypt|nigeria|kenya|south africa|emea|apac|latam|europe|toronto|montreal|ottawa|waterloo|"
    r"london|bengaluru|bangalore|hyderabad|pune|paris|berlin|munich|amsterdam|zurich|zürich|tokyo|"
    r"sydney|tel aviv|warsaw)\b", re.I)
CA_PROVINCE_RE = re.compile(r",\s*(on|bc|qc|ab|mb|sk|ns|nb|nl|pe)\b", re.I)
_STATE_CODES = "|".join(US_STATES)
STATE_ABBR_RE = re.compile(r",\s*(" + _STATE_CODES + r")\b(?=\s*(?:,|$|/))", re.I)
BARE_STATE_ABBR_RE = re.compile(
    r"^(" + "|".join(code for code in US_STATES if code not in {"IN", "OR", "ME", "AL"}) + r")$",
    re.I,
)
STATE_NAME_RE = re.compile(r"\b(" + "|".join(US_STATES.values()) + r")\b", re.I)
CITY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in US_CITIES) + r")\b", re.I)


@lru_cache(maxsize=512)
def _phrase_re(phrase: str) -> re.Pattern:
    """Word-bounded phrase. Config keywords use spaces as boundaries (' ml '), so strip and bound instead."""
    p = phrase.strip().lower()
    left = r"(?<![a-z0-9])" if p[:1].isalnum() else ""
    right = r"s?(?![a-z0-9])" if p[-1:].isalnum() else ""
    return re.compile(left + re.escape(p) + right)


def _matches_any(text: str, phrases: list[str]) -> str | None:
    t = text.lower()
    return next((p for p in phrases if p.strip() and _phrase_re(p).search(t)), None)


def looks_swe(title: str) -> bool:
    return _matches_any(title, SWE_WORDS) is not None


def looks_ai(title: str, config: dict) -> bool:
    return _matches_any(title, config["filters"].get("ai_title_keywords") or []) is not None


def is_us_location(loc: str) -> bool:
    s = loc.strip()
    low = s.lower()
    if US_COUNTRY_RE.search(s):
        return True
    if NON_US_RE.search(s) or CA_PROVINCE_RE.search(s):
        return False
    if (low in US_CITY_ALIASES or STATE_ABBR_RE.search(s) or BARE_STATE_ABBR_RE.fullmatch(s)
            or STATE_NAME_RE.search(s) or CITY_RE.search(s)):
        return True
    return False


def _location_evidence(locations: list[str], us_context: bool = False) -> bool | None:
    locs = [loc.strip() for loc in locations if loc and loc.strip()]
    evidence = [is_us_location(loc) for loc in locs]
    if any(evidence):
        return True
    has_non_us = any(NON_US_RE.search(loc) or CA_PROVINCE_RE.search(loc) for loc in locs)
    if us_context:
        # A list heading can establish a remote/unspecified US posting, but a conflicting
        # explicit foreign location should be reviewed instead of overridden.
        return None if has_non_us else True
    if has_non_us:
        return False
    return None


def is_us(locations: list[str], us_context: bool = False) -> bool:
    """Return true only when location text or explicit source context establishes US scope."""
    return _location_evidence(locations, us_context) is True


def _term_evidence(job: Job, wanted: list[str]) -> bool | None:
    wanted_terms = set(explicit_season_terms(*wanted))
    if not wanted_terms:
        wanted_terms = {" ".join(str(term).lower().split()) for term in wanted if str(term).strip()}
    declared = getattr(job, "terms", None)
    if isinstance(declared, str):
        declared = [declared]
    declared_terms = set(explicit_season_terms(*(declared or [])))
    title_terms = set(explicit_season_terms(job.title))

    if declared_terms:
        if declared_terms & wanted_terms:
            if title_terms and not title_terms & wanted_terms:
                return None
            return True
        if title_terms & wanted_terms:
            return None
        return False
    if title_terms:
        return bool(title_terms & wanted_terms)
    return None


def _term_ok(job: Job, wanted: list[str]) -> bool:
    return _term_evidence(job, wanted) is True


def keep(job: Job, config: dict) -> tuple[bool, str]:
    f = config["filters"]
    title = job.title or ""
    if not INTERN_RE.search(title):
        return False, "not an internship"
    if kw := _matches_any(title, f.get("exclude_title_keywords") or []):
        return False, f"excluded keyword: {kw.strip()}"
    software, ai = f.get("software_categories") or [], f.get("ai_categories") or []
    if job.category in software:
        pass
    elif job.category in ai:
        if not looks_ai(title, config):
            return False, f"{job.category} title is not AI/ML"
    elif not job.category:
        if not (looks_swe(title) or looks_ai(title, config)):
            return False, "title is not SWE/AI"
    elif not looks_swe(title):
        return False, f"category {job.category} out of scope"
    term_state = _term_evidence(job, f["terms"]) if f.get("terms") else True
    us_state = _location_evidence(job.locations, bool(getattr(job, "location_context_us", False))) \
        if f.get("us_only") else True
    if term_state is False:
        return False, f"term not in {f['terms']}"
    if us_state is False:
        return False, "not in the US"
    if term_state is None:
        return False, "term unknown"
    if us_state is None:
        return False, "US location unknown"
    return True, "ok"


def filter_jobs(jobs: list[Job], config: dict) -> list[Job]:
    return [j for j in jobs if keep(j, config)[0]]


def policy_fingerprint(config: dict) -> str:
    """Stable hash of the active filter policy for persisted evidence attestations."""
    policy = json.dumps({"revision": FILTER_POLICY_REVISION, "filters": config.get("filters") or {}},
                        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(policy.encode()).hexdigest()
