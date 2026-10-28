"""Helpers shared by the listing sources."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

import httpx

from ..models import normalize_url

USER_AGENT = "apply-agent/1.0 (+personal internship tracker)"

_SEASON_RE = re.compile(
    r"\b(?:(summer|fall|autumn|spring|winter)\s*'?(20\d\d)|(20\d\d)\s*"
    r"(summer|fall|autumn|spring|winter))\b",
    re.I,
)
_GRAD_YEAR_RE = re.compile(
    r"\b(?:grad(?:uation)?|graduat(?:e|ed|ing))\s*(?:year|date|class)?\s*:?[ \t]*(?:in\s*)?"
    r"(?:(?:summer|fall|autumn|spring|winter)\s*'?)?20\d\d",
    re.I,
)
_GRADUATION_CONTEXT_RE = re.compile(r"\b(?:grad(?:uation)?|graduat(?:e|ed|ing)|degree completion)\b", re.I)
_INTERNSHIP_CONTEXT_RE = re.compile(r"\bintern(?:s|ship|ships)\b", re.I)


def explicit_season_terms(*texts: str | None) -> list[str]:
    """Extract explicit season/year pairs, excluding graduation-year references."""
    terms = []
    for text in texts:
        if not text:
            continue
        for match in _SEASON_RE.finditer(_GRAD_YEAR_RE.sub("", text)):
            season, year = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
            term = f"{season.title()} {year}"
            if term not in terms:
                terms.append(term)
    return terms


def explicit_internship_terms(text: str | None, wanted_terms: list[str]) -> list[str]:
    """Accept a target season from description text only when the same sentence names an internship."""
    if not text:
        return []
    wanted = set(explicit_season_terms(*wanted_terms))
    if not wanted:
        return []
    terms = []
    for match in _SEASON_RE.finditer(text):
        season, year = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
        term = f"{season.title()} {year}"
        if term not in wanted:
            continue
        start = max((text.rfind(mark, 0, match.start()) for mark in ".!?;\n"), default=-1) + 1
        stops = [pos for mark in ".!?;\n" if (pos := text.find(mark, match.end())) >= 0]
        end = min(stops, default=len(text))
        sentence = text[start:end]
        if _GRADUATION_CONTEXT_RE.search(sentence) or not _INTERNSHIP_CONTEXT_RE.search(sentence):
            continue
        if term not in terms:
            terms.append(term)
    return terms


def client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def url_id(source: str, url: str) -> str:
    return f"{source}:" + hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def iso_to_ts(text: str | None) -> int:
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0
