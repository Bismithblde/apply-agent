"""Interactive RAG interview module.

Extracts unanswered and ungrounded application questions across failed/needs_review jobs,
deduplicates and clusters them (general vs company-specific), prompts the user interactively
with numbered option choices, atomically updates profile.yaml and SQLite answers_bank,
clears RAG caches, and unlocks resolved applications for automated re-run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import DB_PATH, ROOT

log = logging.getLogger(__name__)

# Common company suffixes to strip when checking if a company is referenced in a question
_CORP_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|corp|corporation|group|technologies|technology|security|solutions|solution|company|co|co\.|ltd|ltd\.)\b",
    re.I,
)

# Company-specific phrases
_COMPANY_SPECIFIC_PATTERNS = [
    re.compile(r"\b(?:at|with|for)\s+(?:our|this|the)\s+(?:company|organization|firm|client|employer|institution|agency)\b", re.I),
    re.compile(r"\b(?:our|this|the)\s+(?:company's|organization's|client's|employer's)\b", re.I),
    re.compile(r"\b(?:previous|prior|former)\s+(?:employee|intern|worker|contractor|employment)\b", re.I),
    re.compile(r"\b(?:previously|formerly|ever)\s+(?:employed|worked|contracted|interned)\s+(?:here|at|for|with)\b", re.I),
    re.compile(r"\b(?:relatives?|family|close\s+relationship)\s+.*(?:company|employer|organization)\b", re.I),
    re.compile(r"\b(?:terms\s+and\s+conditions|privacy\s+policy|consent\s+to\s+the\s+terms)\b", re.I),
    re.compile(r"\b(?:code\s+of\s+conduct|conflict\s+of\s+interest)\b", re.I),
    re.compile(r"\bon-?site\b.*\b(?:in|at)\s+[A-Za-z]+", re.I),
]

# Patterns for general preference questions
_PREFERENCE_MAP = [
    (re.compile(r"\b(?:willing\s+to\s+relocate|relocation)\b", re.I), "willing_to_relocate"),
    (re.compile(r"\b(?:open\s+to\s+remote|remote\s+work|work\s+remotely)\b", re.I), "open_to_remote"),
    (re.compile(r"\b(?:open\s+to\s+hybrid|hybrid\s+work)\b", re.I), "open_to_hybrid"),
    (re.compile(r"\b(?:open\s+to\s+onsite|onsite\s+work)\b", re.I), "open_to_onsite"),
    (re.compile(r"\b(?:willing\s+to\s+travel|travel\s+percentage)\b", re.I), "willing_to_travel"),
    (re.compile(r"\b(?:over\s+18|18\s+years\s+of\s+age|at\s+least\s+18)\b", re.I), "over_18"),
    (re.compile(r"\bbackground\s+check\b", re.I), "background_check_ok"),
    (re.compile(r"\bdrug\s+(?:test|screen)\b", re.I), "drug_test_ok"),
    (re.compile(r"\bsecurity\s+clearance\b", re.I), "security_clearance"),
]

_BINARY_QUESTION = re.compile(
    r"^\s*(?:are\s+you|do\s+you|have\s+you|will\s+you|can\s+you|would\s+you|is\s+there|did\s+you|"
    r"were\s+you|should\s+you|could\s+you|please\s+confirm|i\s+consent|i\s+agree)\b",
    re.I,
)

_TRANSIENT_PATTERNS = (
    re.compile(r"\b(?:date\s*signed|signature\s*date|signed\s*on)\b", re.I),
    re.compile(r"^(?:date|today'?s?\s*date)$", re.I),
    re.compile(r"\b(?:re-?type|confirm)\s+email\b", re.I),
    re.compile(r"\b(?:captcha|recaptcha|turnstile|hcaptcha|verification\s*code)\b", re.I),
    re.compile(r"\b(?:full\s*name|legal\s*name|applicant\s*name|candidate\s*name)\b", re.I),
    re.compile(r"^(?:name|first\s*name|last\s*name)$", re.I),
)


def is_transient_question(question: str) -> bool:
    q = (question or "").strip()
    return any(p.search(q) for p in _TRANSIENT_PATTERNS)


@dataclass
class UnansweredQuestion:
    job_id: str
    company: str
    title: str
    question: str
    key: str = ""
    kind: str = "text"
    options: list[str] = field(default_factory=list)
    required: bool = True
    application_id: int | None = None


@dataclass
class QuestionCluster:
    cluster_id: str
    question: str
    category: str  # "company" or "general"
    company: str | None  # company name if company-specific, else None
    target_section: str  # "company_answers", "custom_answers", or "preferences"
    target_key: str  # key inside the target section
    options: list[str] = field(default_factory=list)
    kind: str = "text"
    job_ids: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    job_titles: list[str] = field(default_factory=list)


@dataclass
class ProfileUpdate:
    target_section: str  # "company_answers", "custom_answers", "preferences"
    subsection: str | None  # company name for company_answers, None for others
    target_key: str
    answer: Any
    question_text: str = ""


class InterviewQuit(Exception):
    """Raised when the user chooses to exit the interview session."""


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return " ".join(cleaned.split())


def _clean_company_name(company: str) -> str:
    cleaned = _CORP_SUFFIXES.sub("", (company or "").lower())
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    return " ".join(cleaned.split())


def classify_question(
    question: str,
    company: str,
    options: list[str] | None = None,
) -> tuple[str, str, str | None, str]:
    """Classify a question as general or company-specific.

    Returns:
        (category, target_section, subsection, target_key)
        e.g.:
        ("company", "company_answers", "Alstom", "Prior Employment?")
        ("general", "custom_answers", None, "What are your salary expectations?")
        ("general", "preferences", None, "willing_to_relocate")
    """
    clean_comp = _clean_company_name(company)
    norm_q = _normalize_text(question)

    # 1. Company name appears in question text
    if clean_comp and len(clean_comp) >= 3 and clean_comp in norm_q:
        return "company", "company_answers", company, question

    # 2. Company-specific patterns
    for pat in _COMPANY_SPECIFIC_PATTERNS:
        if pat.search(question):
            return "company", "company_answers", company, question

    # 3. Known direct preferences
    for pat, pref_key in _PREFERENCE_MAP:
        if pat.search(question):
            return "general", "preferences", None, pref_key

    # 4. General question (skills, salary, education, etc.)
    return "general", "custom_answers", None, question


def cluster_questions(questions: list[UnansweredQuestion]) -> list[QuestionCluster]:
    """Deduplicate and cluster unanswered questions across applications.

    General questions with identical or equivalent text are clustered together into a single
    prompt so the user only answers once for all companies.
    Company-specific questions are clustered by (company, question).
    """
    clusters_by_key: dict[tuple, QuestionCluster] = {}

    for q in questions:
        if is_transient_question(q.question):
            continue
        category, section, subsection, target_key = classify_question(q.question, q.company, q.options)

        if category == "company":
            company_key = (subsection or q.company).strip().casefold()
            cluster_key = ("company", company_key, _normalize_text(q.question))
        else:
            cluster_key = ("general", section, target_key, _normalize_text(q.question))

        if cluster_key not in clusters_by_key:
            opts = list(q.options)
            if not opts and _BINARY_QUESTION.search(q.question):
                opts = ["Yes", "No"]

            cluster_id = f"cl_{len(clusters_by_key) + 1}"
            clusters_by_key[cluster_key] = QuestionCluster(
                cluster_id=cluster_id,
                question=q.question,
                category=category,
                company=subsection if category == "company" else None,
                target_section=section,
                target_key=target_key,
                options=opts,
                kind=q.kind or ("select" if opts else "text"),
                job_ids=[q.job_id] if q.job_id else [],
                companies=[q.company] if q.company else [],
                job_titles=[f"{q.company}: {q.title}"] if q.company and q.title else [],
            )
        else:
            cluster = clusters_by_key[cluster_key]
            if q.job_id and q.job_id not in cluster.job_ids:
                cluster.job_ids.append(q.job_id)
            if q.company and q.company not in cluster.companies:
                cluster.companies.append(q.company)
            if q.company and q.title:
                desc = f"{q.company}: {q.title}"
                if desc not in cluster.job_titles:
                    cluster.job_titles.append(desc)
            if not cluster.options and q.options:
                cluster.options = list(q.options)
            elif q.options:
                for opt in q.options:
                    if opt not in cluster.options:
                        cluster.options.append(opt)
            if len(q.question) > len(cluster.question):
                cluster.question = q.question

    return list(clusters_by_key.values())


def read_unanswered_questions(
    job_ids: list[str] | None = None,
    db_path: Path | str | None = None,
    items: list[Any] | None = None,
) -> list[UnansweredQuestion]:
    """Read unanswered questions from SQLite DB or directly passed in from a batch run."""
    questions: list[UnansweredQuestion] = []

    # If items are passed directly
    if items is not None:
        for item in items:
            if isinstance(item, UnansweredQuestion):
                questions.append(item)
            elif isinstance(item, dict):
                questions.append(
                    UnansweredQuestion(
                        job_id=str(item.get("job_id", "")),
                        company=str(item.get("company", "")),
                        title=str(item.get("title", "")),
                        question=str(item.get("question") or item.get("label", "")),
                        key=str(item.get("key", "")),
                        kind=str(item.get("kind", "text")),
                        options=list(item.get("options") or []),
                        required=bool(item.get("required", True)),
                    )
                )
            elif hasattr(item, "unanswered_required") and hasattr(item, "job"):
                for u in item.unanswered_required:
                    if isinstance(u, dict):
                        questions.append(
                            UnansweredQuestion(
                                job_id=item.job.id,
                                company=item.job.company,
                                title=item.job.title,
                                question=u.get("label") or u.get("key", ""),
                                key=u.get("key", ""),
                                kind=u.get("kind", "text"),
                                options=list(u.get("options") or []),
                                required=bool(u.get("required", True)),
                            )
                        )
                    else:
                        questions.append(
                            UnansweredQuestion(
                                job_id=item.job.id,
                                company=item.job.company,
                                title=item.job.title,
                                question=str(u),
                            )
                        )
        if job_ids:
            job_set = set(job_ids)
            questions = [q for q in questions if q.job_id in job_set]
        return questions

    # Read from SQLite database
    target_db = Path(db_path or DB_PATH)
    if not target_db.exists():
        log.warning("database %s does not exist", target_db)
        return []

    conn = sqlite3.connect(target_db, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT a.id, a.job_id, a.status, a.reason, a.unanswered_json, a.answers_json, "
            "       j.company, j.title, j.status as job_status "
            "FROM applications a "
            "JOIN jobs j ON j.id = a.job_id "
            "WHERE a.status = 'needs_review' OR j.status IN ('needs_review', 'failed') "
            "ORDER BY a.created_at DESC"
        )
        rows = conn.execute(sql).fetchall()

        seen_jobs: set[str] = set()
        for row in rows:
            job_id = row["job_id"]
            if job_ids and job_id not in job_ids:
                continue
            if job_id in seen_jobs:
                continue
            seen_jobs.add(job_id)

            company = row["company"]
            title = row["title"]
            app_id = row["id"]

            raw_unanswered = row["unanswered_json"] or "[]"
            parsed_questions = []
            try:
                data = json.loads(raw_unanswered)
                if isinstance(data, list):
                    parsed_questions = data
            except Exception:
                pass

            if parsed_questions:
                for item in parsed_questions:
                    if isinstance(item, dict):
                        q_text = item.get("label") or item.get("key", "")
                        if is_transient_question(q_text):
                            continue
                        questions.append(
                            UnansweredQuestion(
                                job_id=job_id,
                                company=company,
                                title=title,
                                question=q_text,
                                key=item.get("key", ""),
                                kind=item.get("kind", "text"),
                                options=list(item.get("options") or []),
                                required=bool(item.get("required", True)),
                                application_id=app_id,
                            )
                        )
                    elif isinstance(item, str) and item.strip():
                        q_text = item.strip()
                        if is_transient_question(q_text):
                            continue
                        questions.append(
                            UnansweredQuestion(
                                job_id=job_id,
                                company=company,
                                title=title,
                                question=q_text,
                                application_id=app_id,
                            )
                        )
            else:
                reason = row["reason"] or ""
                if "unanswered required:" in reason:
                    part = reason.split("unanswered required:", 1)[1].strip()
                    for q_text in part.split(";"):
                        q_text = q_text.strip()
                        if q_text and not is_transient_question(q_text):
                            questions.append(
                                UnansweredQuestion(
                                    job_id=job_id,
                                    company=company,
                                    title=title,
                                    question=q_text,
                                    application_id=app_id,
                                )
                            )
    finally:
        conn.close()

    return questions


def format_prompt(cluster: QuestionCluster, index: int, total: int) -> str:
    """Format an interactive interview question clearly with context and numbered choices."""
    lines: list[str] = ["=" * 80]
    if cluster.category == "company":
        scope_str = f"Company-specific: {cluster.company}"
        target_str = f"profile.yaml -> company_answers.{cluster.company}['{cluster.target_key}']"
    elif cluster.target_section == "preferences":
        scope_str = "General Preference"
        target_str = f"profile.yaml -> preferences.{cluster.target_key}"
    else:
        scope_str = "General Custom Answer"
        target_str = f"profile.yaml -> custom_answers['{cluster.target_key}']"

    lines.append(f"Question [{index}/{total}] ({scope_str})")
    lines.append(f"Target: {target_str}")

    if cluster.job_titles:
        lines.append(f"Related posting(s) [{len(cluster.job_titles)}]:")
        for title in cluster.job_titles[:5]:
            lines.append(f"  * {title}")
        if len(cluster.job_titles) > 5:
            lines.append(f"  ... and {len(cluster.job_titles) - 5} more")

    lines.append("")
    lines.append(f'"{cluster.question}"')
    lines.append("")

    if cluster.options:
        lines.append("Choices:")
        for i, opt in enumerate(cluster.options, 1):
            lines.append(f"  [{i}] {opt}")
        lines.append("  [s] Skip this question")
        lines.append("  [q] Quit interview and save current answers")
    else:
        lines.append("Choices:")
        lines.append("  (Enter your answer as text)")
        lines.append("  [s] Skip this question")
        lines.append("  [q] Quit interview and save current answers")

    lines.append("=" * 80)
    return "\n".join(lines)


def prompt_user(
    cluster: QuestionCluster,
    index: int,
    total: int,
    input_fn: Callable[[str], str] = input,
    output_stream: Any = sys.stdout,
) -> str | None:
    """Prompt the user for an answer to a question cluster."""
    print(format_prompt(cluster, index, total), file=output_stream)
    prompt_msg = "Your answer: "
    if cluster.options:
        prompt_msg = f"Your choice [1-{len(cluster.options)}, custom text, s, q]: "

    raw = input_fn(prompt_msg).strip()
    if raw.lower() in ("q", "quit"):
        raise InterviewQuit()
    if raw.lower() in ("s", "skip") or not raw:
        print("Skipped.\n", file=output_stream)
        return None

    if cluster.options:
        # Check if numeric choice
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(cluster.options):
                chosen = cluster.options[idx]
                print(f"Selected: {chosen}\n", file=output_stream)
                return chosen
        # Check direct match with option
        for opt in cluster.options:
            if opt.casefold().strip() == raw.casefold().strip():
                print(f"Selected: {opt}\n", file=output_stream)
                return opt

    print(f"Answer recorded: {raw}\n", file=output_stream)
    return raw


def update_profile_yaml(profile_path: Path | str, updates: list[ProfileUpdate]) -> None:
    """Atomically update profile.yaml while preserving comments and structure.

    Uses ruamel.yaml if available; falls back to PyYAML.
    """
    profile_path = Path(profile_path)
    if not profile_path.exists():
        profile_path.write_text("# Applicant Profile\n", encoding="utf-8")

    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap

        yaml_loader = YAML()
        yaml_loader.preserve_quotes = True
        yaml_loader.indent(mapping=2, sequence=4, offset=2)

        with open(profile_path, "r", encoding="utf-8") as f:
            doc = yaml_loader.load(f)
        if doc is None:
            doc = CommentedMap()

        for u in updates:
            sec = u.target_section
            if sec not in doc or not isinstance(doc[sec], dict):
                doc[sec] = CommentedMap()

            if u.subsection:
                sub = u.subsection
                if sub not in doc[sec] or not isinstance(doc[sec][sub], dict):
                    doc[sec][sub] = CommentedMap()
                doc[sec][sub][u.target_key] = u.answer
            else:
                doc[sec][u.target_key] = u.answer

        tmp_path = profile_path.with_name(f"{profile_path.name}.tmp.{os.getpid()}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml_loader.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, profile_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    except ImportError:
        # Fallback to PyYAML safe dump
        import yaml

        with open(profile_path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}

        for u in updates:
            sec = u.target_section
            if sec not in doc or not isinstance(doc[sec], dict):
                doc[sec] = {}
            if u.subsection:
                sub = u.subsection
                if sub not in doc[sec] or not isinstance(doc[sec][sub], dict):
                    doc[sec][sub] = {}
                doc[sec][sub][u.target_key] = u.answer
            else:
                doc[sec][u.target_key] = u.answer

        tmp_path = profile_path.with_name(f"{profile_path.name}.tmp.{os.getpid()}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(doc, f, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, profile_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise


def update_answers_bank(updates: list[ProfileUpdate], db_path: Path | str | None = None) -> None:
    """Save interview answers into SQLite answers_bank table."""
    from . import db

    target_db = Path(db_path or DB_PATH)
    if not target_db.exists():
        db.init()

    for u in updates:
        company = u.subsection or ""
        question = u.question_text or u.target_key
        db.save_answers_bank(question=question, answer=str(u.answer), company=company, source="user_interview")


def invalidate_caches() -> None:
    """Invalidate RAG passage and resume text caches so new answers are immediately searchable."""
    try:
        from . import answers

        if hasattr(answers, "_resume_text_cached"):
            answers._resume_text_cached.cache_clear()
    except Exception as exc:
        log.debug("could not invalidate answers cache: %s", exc)


def _resolve_unlocked_jobs(
    questions: list[UnansweredQuestion],
    answered_cluster_ids: set[str],
    clusters: list[QuestionCluster],
    db_path: Path | str | None = None,
) -> list[str]:
    """Find jobs whose required unanswered questions are now all resolved."""
    # Map cluster to job_ids
    cluster_by_id = {c.cluster_id: c for c in clusters}

    # Track unanswered question count per job
    job_questions: dict[str, set[str]] = {}
    for q in questions:
        norm = _normalize_text(q.question)
        job_questions.setdefault(q.job_id, set()).add(norm)

    # For each answered cluster, remove its questions from job_questions
    for cid in answered_cluster_ids:
        c = cluster_by_id.get(cid)
        if not c:
            continue
        c_norm = _normalize_text(c.question)
        for jid in c.job_ids:
            if jid in job_questions:
                job_questions[jid].discard(c_norm)

    unlocked: list[str] = []
    for jid, remaining in job_questions.items():
        if not remaining:
            unlocked.append(jid)

    # Update job statuses in SQLite
    if unlocked:
        from . import db

        with db.connect() as conn:
            for jid in unlocked:
                conn.execute(
                    "UPDATE jobs SET status='new', active_run_id=NULL, active_run_previous_status=NULL "
                    "WHERE id=? AND status IN ('needs_review', 'failed')",
                    (jid,),
                )

    return unlocked


def run_interview(
    config: dict | None = None,
    profile: dict | None = None,
    profile_path: Path | str | None = None,
    db_path: Path | str | None = None,
    job_ids: list[str] | None = None,
    items: list[Any] | None = None,
    input_fn: Callable[[str], str] = input,
    auto_reapply: bool = False,
    output_stream: Any = sys.stdout,
) -> list[str]:
    """Run interactive CLI interview session.

    Returns:
        List of job IDs that have been unlocked.
    """
    target_profile_path = Path(profile_path or ROOT / "profile.yaml")
    questions = read_unanswered_questions(job_ids=job_ids, db_path=db_path, items=items)

    if not questions:
        print("No unanswered questions found needing review.", file=output_stream)
        return []

    clusters = cluster_questions(questions)
    affected_jobs = {q.job_id for q in questions if q.job_id}
    print(
        f"\n[!] Interactive RAG Interview: Found {len(clusters)} question(s) across {len(affected_jobs)} application(s).\n",
        file=output_stream,
    )

    updates: list[ProfileUpdate] = []
    answered_cluster_ids: set[str] = set()

    for idx, cluster in enumerate(clusters, 1):
        try:
            answer = prompt_user(cluster, idx, len(clusters), input_fn=input_fn, output_stream=output_stream)
            if answer is not None:
                update = ProfileUpdate(
                    target_section=cluster.target_section,
                    subsection=cluster.company if cluster.category == "company" else None,
                    target_key=cluster.target_key,
                    answer=answer,
                    question_text=cluster.question,
                )
                updates.append(update)
                answered_cluster_ids.add(cluster.cluster_id)
        except InterviewQuit:
            print("\nInterview stopped by user. Saving responses collected so far...", file=output_stream)
            break

    if updates:
        print(f"Persisting {len(updates)} answer(s) to profile and SQLite answer bank...", file=output_stream)
        update_profile_yaml(target_profile_path, updates)
        update_answers_bank(updates, db_path=db_path)
        invalidate_caches()

        # Update in-memory profile if provided
        if profile is not None:
            for u in updates:
                sec = profile.setdefault(u.target_section, {})
                if u.subsection:
                    sec.setdefault(u.subsection, {})[u.target_key] = u.answer
                else:
                    sec[u.target_key] = u.answer

    unlocked = _resolve_unlocked_jobs(questions, answered_cluster_ids, clusters, db_path=db_path)
    if unlocked:
        print(f"\n[+] Unlocked {len(unlocked)} job(s) ready for re-application:", file=output_stream)
        for jid in unlocked:
            print(f"  * {jid}", file=output_stream)

        if auto_reapply:
            from .runner import apply_batch

            print(f"\n[+] Re-applying to {len(unlocked)} unlocked job(s)...", file=output_stream)
            apply_batch(config or {}, profile or {}, job_ids=unlocked)

    return unlocked
