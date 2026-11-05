"""Sync and match prior internship application confirmation emails from Gmail.

Ensures `agent pick` excludes roles the applicant already applied to, while preserving
distinct positions at the same company and roles posted at different times.
"""
from __future__ import annotations

import html
import json
import math
from html.parser import HTMLParser
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import logging
import re
from email.utils import parsedate_to_datetime
from typing import Any

from . import db, mail

log = logging.getLogger(__name__)

STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "in", "at", "to", "of", "with",
    "summer", "fall", "winter", "spring", "2024", "2025", "2026", "2027", "2028",
    "internship", "intern", "interns", "role", "position", "program", "university",
    "student", "job", "applicant", "application", "candidate", "early", "career",
    "bachelors", "masters", "phd", "undergraduate", "graduate",
}

SPECIALTY_TOKENS = {
    "embedded", "autonomy", "hardware", "firmware", "mobile", "ios", "android",
    "frontend", "backend", "fullstack", "full", "stack", "data", "science",
    "machine", "learning", "ml", "ai", "artificial", "intelligence", "security",
    "infra", "infrastructure", "systems", "enterprise", "cloud", "platform",
    "validation", "qa", "test", "testing", "atc", "route", "planning", "flight",
    "avionics", "vehicle", "perception", "robotics", "devops", "sre", "networks",
}


def normalize_company(name: str) -> str:
    s = re.sub(r"[^\w\s]", "", (name or "").lower())
    s = re.sub(
        r"\b(inc|llc|corp|corporation|technologies|early career|university|"
        r"hiring team|recruiting team|talent acquisition|team|solutions|us)\b",
        "",
        s,
    )
    return " ".join(s.split())


def core_title_tokens(title: str) -> set[str]:
    cleaned = re.sub(r"[^\w\s]", " ", (title or "").lower())
    tokens = {w for w in cleaned.split() if w not in STOPWORDS and len(w) > 1}
    # Normalize engineer / engineering, developer / development
    norm = set()
    for t in tokens:
        if t in ("engineering", "engineer"):
            norm.add("engineer")
        elif t in ("development", "developer"):
            norm.add("developer")
        else:
            norm.add(t)
    return norm


def extract_term(text: str) -> str | None:
    m = re.search(r"\b(summer|fall|spring|winter)\s*(202[4-8])\b", text, re.I)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}"
    m2 = re.search(r"\b202[4-8]\b", text)
    if m2:
        return m2.group(0)
    return None


def is_purely_same_role(
    job_title: str,
    email_title: str,
    job_posted_ts: float | None = None,
    email_received_ts: float | None = None,
    email_text: str = "",
) -> bool:
    """True only when email represents a prior application for the purely same role and posting cycle."""
    if not email_title or email_title.upper().startswith("UNKNOWN"):
        return False

    # Check term discrepancy
    e_term = extract_term(email_title + " " + email_text)
    j_term = extract_term(job_title)
    if e_term and j_term:
        if re.search(r"202[4-8]", e_term).group() != re.search(r"202[4-8]", j_term).group():
            return False
        if len(e_term) > 4 and len(j_term) > 4 and e_term != j_term:
            return False
    elif e_term and "2027" not in e_term and any(yr in e_term for yr in ("2024", "2025", "2026")):
        return False

    j_tokens = core_title_tokens(job_title)
    e_tokens = core_title_tokens(email_title)
    if not j_tokens or not e_tokens:
        return False

    # Specialty tokens check: if either has a specialty that the other lacks, it's a different position!
    j_specs = j_tokens & SPECIALTY_TOKENS
    e_specs = e_tokens & SPECIALTY_TOKENS
    if j_specs != e_specs:
        return False

    # Check token overlap
    overlap = j_tokens & e_tokens
    if not overlap:
        return False

    # Must share at least 75% of meaningful tokens
    similarity = len(overlap) / max(len(j_tokens), len(e_tokens))
    if similarity < 0.75 and j_tokens != e_tokens:
        return False

    # Check posting times if timestamps are provided:
    # "sometimes same name, but different times posted, can apply to both"
    if job_posted_ts and email_received_ts:
        # If the job was posted significantly after the confirmation email (>5 days later),
        # this is a new/re-posted opening that can be applied to!
        if job_posted_ts > email_received_ts + 86400 * 5:
            return False
        # If the email was received >120 days before the job was posted (different cycle)
        if email_received_ts < job_posted_ts - 86400 * 120:
            return False

    return True


def init_db() -> None:
    with db.connect() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS email_confirmations (
                message_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                title TEXT,
                term TEXT,
                received_at REAL NOT NULL,
                raw_subject TEXT,
                snippet TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_email_conf_company ON email_confirmations(company)")
        columns = {r[1] for r in c.execute("PRAGMA table_info(email_confirmations)")}
        if "job_urls" not in columns:
            c.execute("ALTER TABLE email_confirmations ADD COLUMN job_urls TEXT NOT NULL DEFAULT '[]'")
        if "identity_scanned" not in columns:
            c.execute("ALTER TABLE email_confirmations ADD COLUMN identity_scanned INTEGER NOT NULL DEFAULT 0")


def _posting_urls(text: str) -> list[str]:
    urls = set()
    identity_keys = {'gh_jid', 'ashby_jid', 'jobid', 'job_id', 'requisitionid', 'requisition_id'}
    for match in re.findall(r'https?://[^\s<>"\']+', html.unescape(text)):
        parsed = urlparse(match.rstrip('.,);'))
        if not parsed.hostname or parsed.username or parsed.password:
            continue
        if not (re.search(r'/(?:jobs?|careers)/[^/]+', parsed.path, re.I)
                or parsed.hostname.lower() in {'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com'}
                or any(k.lower() in identity_keys for k, _ in parse_qsl(parsed.query))):
            continue
        known_ats_path = any(parsed.hostname.lower().endswith(host) for host in
                             ('.greenhouse.io', '.lever.co', '.ashbyhq.com', '.myworkdayjobs.com'))
        tracking = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'ref', 'source', 'gh_src', 'lever-source'}
        if not known_ats_path and any(k.lower() not in identity_keys | tracking for k, _ in parse_qsl(parsed.query)):
            continue
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k.lower() in identity_keys])
        urls.add(urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, '', query, '')))
    return sorted(urls)


class _RoleLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.href, self.label, self.context, self.prefix = [], None, '', '', ''

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'div', 'li', 'br'):
            self.context = ''
        if tag == 'a':
            self.href, self.label, self.prefix = dict(attrs).get('href'), '', self.context

    def handle_data(self, data):
        self.context = (self.context + data)[-200:]
        if self.href:
            self.label += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            self.links.append((self.href, self.label, self.prefix))
            self.href = None


def _confirmed_urls(text: str, raw_html: str, role: str | None) -> list[str]:
    """Only one posting explicitly tied to the confirmed role can become exact evidence."""
    candidates = _posting_urls(text + ' ' + raw_html)
    if len(candidates) != 1 or not role:
        return []
    normalize = lambda value: re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()
    links = _RoleLinks()
    links.feed(raw_html)
    confirming_prefix = re.compile(r'(?:application for|applied (?:for|to)|applying (?:for|to))\s+(?:the\s+)?$', re.I)
    tied = {url for href, label, prefix in links.links
            if normalize(label) == normalize(role) and confirming_prefix.search(prefix)
            for url in _posting_urls(href)}
    # Plain-text confirmations may put the posting link directly after the confirmed role.
    relation = re.compile(r'(?:application for|applied for|applying for)\s+(?:the\s+)?'
                          + re.escape(role) + r'\s*(?:role|position)?[.: -]*\s*(https?://[^\s<>]+)', re.I)
    for match in relation.finditer(text):
        tied.update(_posting_urls(match.group(1)))
    return candidates if candidates[0] in tied else []


def _extract_email_info(m: dict) -> dict[str, Any] | None:
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    subj = headers.get("subject", "")
    sender = headers.get("from", "")
    date_str = headers.get("date", "")

    try:
        dt = parsedate_to_datetime(date_str)
        received_at = dt.timestamp()
    except Exception:
        received_at = float(m.get("internalDate", 0)) / 1000
    if not math.isfinite(received_at) or received_at <= 0:
        raise ValueError("confirmation has no valid received timestamp")

    parts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    mail._walk_parts(m.get("payload", {}), parts)
    text = "\n".join(parts["text/plain"]) or mail.html_to_text("\n".join(parts["text/html"]))
    text = html.unescape(text)

    # 1. Company
    co = None
    m_co = re.search(
        r"(?:applying to|thank you for applying to|application to|welcome to|thanks for applying to)\s+([A-Za-z0-9&.,\- ]+?)(?:!|\.|,|\'|\"|\n|\r|$)",
        subj,
        re.I,
    )
    if m_co:
        co = m_co.group(1).strip()
    if not co or len(co) > 40:
        m_subj_prefix = re.match(r"^([A-Za-z0-9&.,\- ]+?):\s*thank you", subj, re.I)
        if m_subj_prefix:
            co = m_subj_prefix.group(1).strip()
    if not co or len(co) > 40:
        m_sender = re.match(r"^\"?([A-Za-z0-9&.,\- ]+?)\"?\s*<", sender)
        if m_sender and "no-reply" not in m_sender.group(1).lower() and "greenhouse" not in m_sender.group(1).lower():
            co = m_sender.group(1).strip()

    # 2. Role
    role = None
    patterns = [
        r"(?:for the|to the|position of|role of|application for|applying for|interest in the)\s+([A-Za-z0-9&.,\-/() ]+?)\s+(?:position|role|program|internship|has been received|\.|!|\n|\r|,)",
        r"(?:applied for|applied to)\s+([A-Za-z0-9&.,\-/() ]+?)\s+(?:position|role|has been received|\.|!|\n|\r)",
    ]
    for p in patterns:
        m_r = re.search(p, text, re.I)
        if m_r:
            cand = m_r.group(1).strip()
            if 4 <= len(cand) <= 80 and not cand.lower().startswith("your interest"):
                role = cand
                break

    job_urls = _confirmed_urls(text, ' '.join(parts['text/html']), role)
    term = extract_term(subj + " " + (role or "") + " " + text[:300])

    return {
        "message_id": m["id"],
        "company": co or "",
        "job_urls": job_urls,
        "title": role,
        "term": term,
        "received_at": received_at,
        "raw_subject": subj,
        "snippet": m.get("snippet", "")[:200],
    }


def sync_email_confirmations(max_results: int | None = 100, *, raise_errors: bool = False) -> int:
    """Index recent confirmations, or all pages when max_results is None."""
    init_db()
    if max_results is not None and max_results <= 0:
        raise ValueError("max_results must be positive or None")
    if not mail.available():
        if raise_errors:
            raise RuntimeError("Gmail unavailable")
        return 0
    query = 'subject:("application received" OR "thank you for applying" OR "application submitted" OR "thanks for applying" OR "we received your application")'
    with db.connect() as c:
        existing = {r[0] for r in c.execute(
            "SELECT message_id FROM email_confirmations WHERE identity_scanned=1")}
    count, synced, token = 0, 0, None
    failures = 0
    seen_tokens = set()
    try:
        while True:
            kwargs = {"q": query, "maxResults": min(500, max_results - count) if max_results else 500}
            if token:
                kwargs["pageToken"] = token
            resp = mail._gapi("messages", **kwargs)
            refs = resp.get("messages", [])
            for ref in refs:
                count += 1
                if ref["id"] in existing:
                    continue
                try:
                    info = _extract_email_info(mail._gapi(f"messages/{ref['id']}", format="full"))
                except Exception:
                    failures += 1
                    continue
                if info is None:
                    continue
                with db.connect() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO email_confirmations "
                        "(message_id,company,title,term,received_at,raw_subject,snippet,job_urls,identity_scanned) "
                        "VALUES (?,?,?,?,?,?,?,?,1)",
                        (info['message_id'], info['company'], info['title'], info['term'],
                         info['received_at'], info['raw_subject'], info['snippet'], json.dumps(info['job_urls'])))
                existing.add(ref['id'])
                synced += 1
            token = resp.get('nextPageToken')
            if not token or token in seen_tokens or (max_results is not None and count >= max_results):
                break
            seen_tokens.add(token)
    except Exception:
        if raise_errors:
            raise
        log.warning("Could not finish Gmail confirmation sync; cached results remain available")
    if failures:
        if raise_errors:
            raise RuntimeError(f"{failures} confirmation messages could not be indexed")
        log.warning("Some confirmation messages could not be indexed; sync is incomplete")
    return synced


def find_prior_application(
    company: str,
    title: str,
    date_posted: float | None = None,
) -> dict[str, Any] | None:
    """Return prior application record if applicant already applied to this purely same role."""
    init_db()
    norm_c = normalize_company(company)
    if not norm_c:
        return None

    with db.connect() as c:
        rows = c.execute(
            "SELECT company, title, term, received_at, raw_subject FROM email_confirmations WHERE title IS NOT NULL"
        ).fetchall()

    for r in rows:
        conf_co_norm = normalize_company(r["company"])
        # Company match: exact or clean substring
        if conf_co_norm == norm_c or (len(norm_c) > 3 and norm_c in conf_co_norm) or (len(conf_co_norm) > 3 and conf_co_norm in norm_c):
            if is_purely_same_role(title, r["title"], date_posted, r["received_at"]):
                return dict(r)

    return None
