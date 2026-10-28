"""Per-tenant ATS accounts. Passwords are Fernet-encrypted with VAULT_KEY before they reach SQLite."""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import load_config

SYMBOLS = "!@#$%^&*"
_AMBIGUOUS = set("0O1lI|")
UPPER = "".join(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in _AMBIGUOUS)
LOWER = "".join(c for c in "abcdefghijklmnopqrstuvwxyz" if c not in _AMBIGUOUS)
DIGITS = "".join(c for c in "0123456789" if c not in _AMBIGUOUS)
_LOCALE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.I)


class VaultError(RuntimeError):
    pass


@dataclass
class Account:
    tenant_key: str
    email: str
    password: str = field(repr=False)
    company: str
    login_url: str
    verified: bool


def _fernet() -> Fernet:
    key = os.environ.get("VAULT_KEY", "").strip()
    if not key:
        raise VaultError("VAULT_KEY missing from .env; generate one with "
                         "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`")
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise VaultError("VAULT_KEY is not a valid Fernet key (32 url-safe base64 bytes)") from e


def _encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise VaultError("cannot decrypt stored password; VAULT_KEY changed?") from e


def tenant_key_for(url: str) -> str:
    """Workday: host + "/" + site (locale segment skipped). myworkdaysite.com also keeps the tenant.
    Everything else: just the host."""
    p = urlparse(url if "://" in url else "https://" + url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]
    if host.endswith("myworkdayjobs.com"):
        segs = [s for s in segs if not _LOCALE.match(s)]
        return f"{host}/{segs[0]}" if segs and segs[0] not in ("job", "login") else host
    if host.endswith("myworkdaysite.com"):
        segs = [s for s in segs if not _LOCALE.match(s)]
        if len(segs) >= 3 and segs[0] == "recruiting":
            return f"{host}/{segs[1]}/{segs[2]}"
    return host


def company_slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", company.lower())[:30] or "apply"


def base_email() -> str:
    email = os.environ.get("APPLY_EMAIL", "").strip()
    if "@" not in email:
        raise VaultError("APPLY_EMAIL missing from .env")
    return email


def alias_email(company: str) -> str:
    local, domain = base_email().split("@", 1)
    return f"{local.split('+', 1)[0]}+{company_slug(company)}@{domain}"


def canonical_email(email: str) -> str:
    """Normalize Gmail spelling without merging distinct plus aliases."""
    local, sep, domain = email.strip().casefold().rpartition("@")
    if not sep:
        return ""
    if domain in ("gmail.com", "googlemail.com"):
        base, plus, tag = local.partition("+")
        local = base.replace(".", "") + plus + tag
        domain = "gmail.com"
    return f"{local}@{domain}"


def email_search_addresses(email: str) -> tuple[str, ...]:
    """Gmail API searches do not expand aliases like the Gmail UI does."""
    original = email.strip()
    canonical = canonical_email(original)
    addresses = [original, canonical]
    if canonical.endswith("@gmail.com"):
        for local in (original.rsplit("@", 1)[0], canonical.rsplit("@", 1)[0]):
            addresses.extend(f"{local}@{domain}" for domain in ("gmail.com", "googlemail.com"))
    return tuple(dict.fromkeys(a for a in addresses if a))


def generate_password(length: int = 20) -> str:
    length = max(length, 12)
    pools = [UPPER, LOWER, DIGITS, SYMBOLS]
    chars = [secrets.choice(p) for p in pools]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _password_length() -> int:
    try:
        return int(load_config().get("accounts", {}).get("password_length", 20))
    except Exception:
        return 20


def get(tenant_key: str) -> Account | None:
    r = db.get_account_row(tenant_key)
    if r is None:
        return None
    return Account(tenant_key=r["tenant_key"], email=r["email"], password=_decrypt(r["password_enc"]),
                   company=r["company"], login_url=r["login_url"], verified=bool(r["verified"]))


def create(tenant_key: str, company: str, login_url: str, *, plus_alias: bool = True) -> Account:
    """Generate and store credentials. Call before typing them into the site so they are never lost."""
    try:
        plus_alias = plus_alias and bool(load_config().get("email", {}).get("plus_alias", True))
    except Exception:
        pass
    email = alias_email(company) if plus_alias else base_email()
    password = generate_password(_password_length())
    db.put_account_row(tenant_key, company, login_url, email, _encrypt(password))
    return Account(tenant_key, email, password, company, login_url, False)


def mark_verified(tenant_key: str) -> None:
    db.set_account_verified(tenant_key, True)


def update_email(tenant_key: str, email: str) -> None:
    r = db.get_account_row(tenant_key)
    if r is None:
        raise VaultError(f"no account for {tenant_key}")
    db.put_account_row(tenant_key, r["company"], r["login_url"], email, r["password_enc"],
                       verified=bool(r["verified"]), notes=r["notes"])


def delete(tenant_key: str) -> None:
    """Drop credentials that were never accepted by the site (e.g. the account already existed)."""
    with db.connect() as c:
        c.execute("DELETE FROM accounts WHERE tenant_key=?", (tenant_key,))
