"""Lead List QA & Scoring Tool.

Usage:
    python lead_scorer.py <input.csv> [-o output.csv] [--no-enrich]

Pipeline: clean -> normalize -> dedupe -> validate -> enrich -> score -> tier.
Stdlib only (csv, urllib, re). Enrichment uses Cloudflare DNS-over-HTTPS
(https://cloudflare-dns.com/dns-query) to look up MX records and derive
the company's mail provider. Free, no API key, rate-limit aware.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable, Optional


# --- Reference data ----------------------------------------------------------

FREE_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "pm.me",
    "mail.com", "gmx.com", "gmx.net", "gmx.de",
    "yandex.com", "yandex.ru", "zoho.com", "fastmail.com", "tutanota.com",
    "qq.com", "163.com", "126.com", "naver.com",
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

CORE_INDUSTRY_PHRASES = ("software", "saas", "internet", "computer software",
                         "information technology")
CORE_INDUSTRY_WORDS = ("tech", "technology")
ADJACENT_INDUSTRY_PHRASES = (
    "it services", "telecom", "telecommunications", "fintech", "cybersecurity",
    "security", "analytics", "machine learning", "artificial intelligence",
    "e-commerce", "ecommerce", "hardware", "semiconductors", "biotech",
    "edtech", "healthtech", "cloud", "devops", "platform", "media", "digital",
)
ADJACENT_INDUSTRY_WORDS = ("ai", "data")

# Abbreviations matched as whole words; phrases matched as substrings.
C_LEVEL_ABBREV = ("ceo", "cto", "cfo", "coo", "cmo", "cro", "ciso", "cio", "cpo")
C_LEVEL_PHRASES = (
    "chief", "founder", "co-founder", "cofounder", "owner", "president",
    "managing partner", "managing director",
)
VP_ABBREV = ("vp", "svp", "evp", "v.p.")
VP_PHRASES = ("vice president",)
DIRECTOR_PHRASES = ("director", "head of", "head,")
DIRECTOR_ABBREV = ("head",)
MANAGER_PHRASES = ("manager", "principal")
MANAGER_ABBREV = ("lead",)

REVOPS_PHRASES = (
    "revenue operations", "revops", "rev ops", "sales operations", "sales ops",
    "go-to-market operations", "gtm operations", "gtm ops",
)
SALES_PHRASES = (
    "sales", "account executive", "business development",
    "biz dev", "account manager", "account director",
)
SALES_ABBREV = ("ae", "bdr", "sdr")
MARKETING_PHRASES = (
    "marketing", "growth", "demand generation", "demand gen", "brand",
    "communications", "public relations",
)

COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|co|co\.|corp|corp\.|corporation|"
    r"gmbh|s\.a\.|sa|sas|s\.a\.s\.|plc|limited|holdings|group|the)\b",
    re.IGNORECASE,
)


# --- Normalization helpers ---------------------------------------------------

def smart_title_case(s: str) -> str:
    """Title-case a name while preserving hyphens, apostrophes, and McX patterns."""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s.strip())
    out = []
    for word in s.split(" "):
        chunks = re.split(r"([-'])", word)
        rebuilt = []
        for chunk in chunks:
            if chunk in ("-", "'"):
                rebuilt.append(chunk)
            elif chunk:
                lower = chunk.lower()
                if lower.startswith("mc") and len(lower) > 2:
                    rebuilt.append("Mc" + lower[2:3].upper() + lower[3:])
                else:
                    rebuilt.append(lower[:1].upper() + lower[1:])
        out.append("".join(rebuilt))
    return " ".join(out)


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def email_domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def is_valid_email_format(email: str) -> bool:
    if not email or " " in email:
        return False
    return bool(EMAIL_RE.match(email))


def normalize_company(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip())


def company_key(raw: str) -> str:
    """Loose key for dedupe: lowercase, drop punctuation and legal suffixes."""
    s = (raw or "").lower()
    s = re.sub(r"[,\.\"']", " ", s)
    s = COMPANY_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_name(full: str) -> tuple[str, str]:
    full = re.sub(r"\s+", " ", (full or "").strip())
    if not full:
        return ("", "")
    parts = full.split(" ")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


# --- Classification ----------------------------------------------------------

def classify_industry(raw: str) -> tuple[str, int]:
    s = (raw or "").lower().strip()
    if not s:
        return ("Unknown", 0)
    # Adjacent-specific phrases (fintech, biotech, healthtech, edtech) must win
    # over the broader "tech" word match — check them first.
    if any(p in s for p in ADJACENT_INDUSTRY_PHRASES):
        return ("Adjacent Tech", 15)
    if any(p in s for p in CORE_INDUSTRY_PHRASES) or _has_word(s, CORE_INDUSTRY_WORDS):
        return ("Software/SaaS/Tech", 30)
    if _has_word(s, ADJACENT_INDUSTRY_WORDS):
        return ("Adjacent Tech", 15)
    return (smart_title_case(raw), 0)


def _has_phrase(text: str, phrases) -> bool:
    return any(p in text for p in phrases)


def _has_word(text: str, words) -> bool:
    """Whole-word match using non-alnum boundaries. Text should be lowercased."""
    for w in words:
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text):
            return True
    return False


def classify_seniority(title_raw: str) -> tuple[str, int]:
    s = (title_raw or "").lower().strip()
    if not s:
        return ("Unknown", 0)
    if _has_phrase(s, C_LEVEL_PHRASES) or _has_word(s, C_LEVEL_ABBREV):
        return ("C-Level", 25)
    if _has_phrase(s, VP_PHRASES) or _has_word(s, VP_ABBREV):
        return ("VP", 25)
    if _has_phrase(s, DIRECTOR_PHRASES) or _has_word(s, DIRECTOR_ABBREV):
        return ("Director", 15)
    if _has_phrase(s, MANAGER_PHRASES) or _has_word(s, MANAGER_ABBREV):
        return ("Manager", 10)
    return ("IC", 0)


def classify_department(title_raw: str, dept_raw: str) -> tuple[str, int]:
    combined = f"{(title_raw or '').lower()} {(dept_raw or '').lower()}".strip()
    if _has_phrase(combined, REVOPS_PHRASES):
        return ("RevOps", 15)
    if _has_phrase(combined, SALES_PHRASES) or _has_word(combined, SALES_ABBREV):
        return ("Sales", 15)
    if _has_phrase(combined, MARKETING_PHRASES):
        return ("Marketing", 15)
    cleaned = (dept_raw or "").strip()
    return (smart_title_case(cleaned) if cleaned else "Other", 0)


def score_employees(raw) -> tuple[Optional[int], int]:
    if raw is None or str(raw).strip() == "":
        return (None, 5)
    s = str(raw).replace(",", "").strip()
    m = re.search(r"\d[\d]*", s)
    if not m:
        return (None, 5)
    n = int(m.group(0))
    if 200 <= n <= 2000:
        return (n, 30)
    if (50 <= n <= 199) or (2001 <= n <= 5000):
        return (n, 15)
    return (n, 5)


def tier_for(score: int) -> str:
    if score >= 80:
        return "Tier 1"
    if score >= 55:
        return "Tier 2"
    return "Tier 3"


# --- Enrichment --------------------------------------------------------------

MX_PROVIDER_HINTS = (
    ("google", "Google Workspace"),
    ("googlemail", "Google Workspace"),
    ("outlook.com", "Microsoft 365"),
    ("protection.outlook", "Microsoft 365"),
    ("office365", "Microsoft 365"),
    ("proofpoint", "Proofpoint"),
    ("mimecast", "Mimecast"),
    ("barracuda", "Barracuda"),
    ("amazonses", "Amazon SES"),
    ("amazonaws", "Amazon SES"),
    ("zoho", "Zoho"),
    ("fastmail", "Fastmail"),
    ("yandex", "Yandex"),
    ("mailgun", "Mailgun"),
    ("sendgrid", "SendGrid"),
)


def _lookup_mx_uncached(domain: str, timeout: float = 4.0, retries: int = 3) -> dict:
    """One MX lookup with jittered exponential backoff. Never raises.

    Returns {'mx_provider': str, 'domain_has_mx': bool}.
    Treats 429 and 5xx as retriable; other HTTP codes and network errors
    are retried once before giving up with 'lookup_failed'.
    """
    url = f"https://cloudflare-dns.com/dns-query?name={domain}&type=MX"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
            answers = payload.get("Answer") or []
            mx_hosts = [str(a.get("data", "")).lower() for a in answers if a.get("type") == 15]
            if not mx_hosts:
                return {"mx_provider": "none", "domain_has_mx": False}
            joined = " ".join(mx_hosts)
            provider = "Other"
            for needle, label in MX_PROVIDER_HINTS:
                if needle in joined:
                    provider = label
                    break
            return {"mx_provider": provider, "domain_has_mx": True}
        except urllib.error.HTTPError as e:
            retriable = e.code == 429 or 500 <= e.code < 600
            if retriable and attempt < retries:
                backoff = (0.4 * (2 ** attempt)) + random.uniform(0, 0.3)
                time.sleep(backoff)
                continue
            return {"mx_provider": "lookup_failed", "domain_has_mx": False}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            if attempt < retries:
                backoff = (0.25 * (2 ** attempt)) + random.uniform(0, 0.2)
                time.sleep(backoff)
                continue
            return {"mx_provider": "lookup_failed", "domain_has_mx": False}

    return {"mx_provider": "lookup_failed", "domain_has_mx": False}


def lookup_mx_provider(domain: str, cache: dict) -> dict:
    """Cached single-shot MX lookup. Safe to call from any thread (no shared writes)."""
    if not domain:
        return {"mx_provider": "unknown", "domain_has_mx": False}
    if domain in cache:
        return cache[domain]
    result = _lookup_mx_uncached(domain)
    cache[domain] = result
    return result


def prefetch_mx_concurrent(domains: Iterable[str], max_workers: int = 10) -> dict:
    """Look up MX for many unique domains in parallel. Returns {domain: result}.

    Caller passes the deduped set of domains; this returns the populated cache.
    Cloudflare DoH happily handles thousands of qps from one IP, so a small
    thread pool gives a big wall-time win without tripping rate limits.
    """
    domains = [d for d in dict.fromkeys(domains) if d]  # dedupe, preserve order
    cache: dict = {}
    if not domains:
        return cache
    workers = max(1, min(max_workers, len(domains)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_lookup_mx_uncached, d): d for d in domains}
        for fut in as_completed(futures):
            d = futures[fut]
            cache[d] = fut.result()
    return cache


# --- Row processing ----------------------------------------------------------

@dataclass
class RunStats:
    rows_in: int = 0
    rows_kept: int = 0
    drops: Counter = field(default_factory=Counter)
    tiers: Counter = field(default_factory=Counter)
    enrichment_failures: int = 0
    unique_domains_enriched: int = 0
    enrich_seconds: float = 0.0


def pick(row: dict, *keys: str) -> str:
    """First non-empty value among the given keys (case-insensitive)."""
    lower = {k.lower(): v for k, v in row.items() if k}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None and str(v).strip() != "":
            return str(v)
    return ""


def _via_mapping(row: dict, mapping: dict, key: str) -> str:
    src = mapping.get(key) if mapping else None
    if not src:
        return ""
    v = row.get(src)
    return "" if v is None else str(v)


# Fields the pipeline understands. Each has a friendly label, the aliases
# we try when auto-detecting, and whether the user can leave it unmapped.
EXPECTED_FIELDS = [
    {"key": "first_name", "label": "First name",
     "aliases": ["first_name", "firstname", "first", "given_name"]},
    {"key": "last_name", "label": "Last name",
     "aliases": ["last_name", "lastname", "last", "surname", "family_name"]},
    {"key": "full_name", "label": "Full name (alt)",
     "aliases": ["full_name", "fullname", "name"]},
    {"key": "email", "label": "Email",
     "aliases": ["email", "email_address", "work_email", "e-mail", "mail"],
     "required": True},
    {"key": "company", "label": "Company",
     "aliases": ["company", "company_name", "organization", "organisation", "account", "org"]},
    {"key": "title", "label": "Job title",
     "aliases": ["title", "job_title", "position", "role"]},
    {"key": "department", "label": "Department",
     "aliases": ["department", "dept", "function"]},
    {"key": "industry", "label": "Industry",
     "aliases": ["industry", "sector", "vertical"]},
    {"key": "employees", "label": "Employee count",
     "aliases": ["employees", "employee_count", "company_size", "size",
                 "headcount", "num_employees", "staff"]},
]


def auto_detect_mapping(headers: list[str]) -> dict[str, str]:
    """Return {canonical_key: source_header_or_empty} using case-insensitive aliases."""
    lower = {(h or "").lower().strip(): h for h in headers if h}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for field_def in EXPECTED_FIELDS:
        chosen = ""
        for alias in field_def["aliases"]:
            src = lower.get(alias)
            if src and src not in used:
                chosen = src
                used.add(src)
                break
        mapping[field_def["key"]] = chosen
    return mapping


CLEAN_FIELDNAMES = [
    "first_name", "last_name", "email", "email_domain", "company", "title",
    "seniority", "department", "industry", "employees", "mx_provider",
    "domain_has_mx", "industry_pts", "employees_pts", "seniority_pts",
    "department_pts", "score", "score_breakdown", "tier",
]


def process_rows(
    rows: list[dict],
    enrich: bool = True,
    mapping: Optional[dict[str, str]] = None,
) -> tuple[RunStats, list[OrderedDict]]:
    """Pure pipeline: take raw row dicts, return (stats, kept rows). No I/O.

    When `mapping` is None, fields are auto-detected from the headers of the
    first row. With enrich=True, MX records for every unique kept-eligible
    domain are looked up in parallel before scoring (big wall-time win on
    large lists).
    """
    stats = RunStats()
    stats.rows_in = len(rows)

    if mapping is None:
        headers = list(rows[0].keys()) if rows else []
        mapping = auto_detect_mapping(headers)

    # Pre-collect every domain we'll plausibly need MX info for, then prefetch
    # them in parallel. We pre-filter out invalid / free-domain emails so we
    # don't waste lookups on rows we're going to drop anyway.
    mx_cache: dict = {}
    if enrich and rows:
        domains: set[str] = set()
        for row in rows:
            e = normalize_email(_via_mapping(row, mapping, "email"))
            if is_valid_email_format(e):
                d = email_domain(e)
                if d and d not in FREE_DOMAINS:
                    domains.add(d)
        if domains:
            t0 = time.monotonic()
            mx_cache = prefetch_mx_concurrent(domains, max_workers=10)
            stats.enrich_seconds = round(time.monotonic() - t0, 2)
            stats.unique_domains_enriched = len(mx_cache)
            stats.enrichment_failures = sum(
                1 for r in mx_cache.values() if r["mx_provider"] == "lookup_failed"
            )

    seen_emails: set[str] = set()
    seen_person_company: set[tuple[str, str, str]] = set()
    kept: list[OrderedDict] = []

    for row in rows:
        # ---- pull fields via the resolved mapping
        first = _via_mapping(row, mapping, "first_name")
        last = _via_mapping(row, mapping, "last_name")
        full = _via_mapping(row, mapping, "full_name")
        if (not first and not last) and full:
            first, last = split_name(full)
        first = smart_title_case(first)
        last = smart_title_case(last)

        email_raw = _via_mapping(row, mapping, "email")
        email = normalize_email(email_raw)

        company_raw = _via_mapping(row, mapping, "company")
        company = normalize_company(company_raw)

        title_raw = _via_mapping(row, mapping, "title")
        dept_raw = _via_mapping(row, mapping, "department")
        industry_raw = _via_mapping(row, mapping, "industry")
        employees_raw = _via_mapping(row, mapping, "employees")

        # ---- validation / dedupe
        if not email:
            stats.drops["missing_email"] += 1
            continue
        if not is_valid_email_format(email):
            stats.drops["invalid_email"] += 1
            continue
        domain = email_domain(email)
        if domain in FREE_DOMAINS:
            stats.drops["free_email_domain"] += 1
            continue
        if email in seen_emails:
            stats.drops["duplicate_email"] += 1
            continue
        person_key = (first.lower(), last.lower(), company_key(company))
        if person_key[0] and person_key[1] and person_key[2] and person_key in seen_person_company:
            stats.drops["duplicate_person_at_company"] += 1
            continue

        seen_emails.add(email)
        if all(person_key):
            seen_person_company.add(person_key)

        # ---- normalize & score
        industry_norm, industry_pts = classify_industry(industry_raw)
        seniority, seniority_pts = classify_seniority(title_raw)
        department_norm, department_pts = classify_department(title_raw, dept_raw)
        emp_count, emp_pts = score_employees(employees_raw)

        score = industry_pts + emp_pts + seniority_pts + department_pts
        score = max(0, min(100, score))
        tier = tier_for(score)
        stats.tiers[tier] += 1

        # ---- enrich (read from prefetched cache; lookup on miss only)
        if enrich:
            enrichment = lookup_mx_provider(domain, mx_cache)
            mx_provider = enrichment["mx_provider"]
            domain_has_mx = enrichment["domain_has_mx"]
        else:
            mx_provider = ""
            domain_has_mx = ""

        breakdown = (
            f"industry +{industry_pts} · employees +{emp_pts} · "
            f"title +{seniority_pts} · dept +{department_pts}"
        )
        kept.append(OrderedDict([
            ("first_name", first),
            ("last_name", last),
            ("email", email),
            ("email_domain", domain),
            ("company", company),
            ("title", title_raw.strip()),
            ("seniority", seniority),
            ("department", department_norm),
            ("industry", industry_norm),
            ("employees", emp_count if emp_count is not None else ""),
            ("mx_provider", mx_provider),
            ("domain_has_mx", domain_has_mx),
            ("industry_pts", industry_pts),
            ("employees_pts", emp_pts),
            ("seniority_pts", seniority_pts),
            ("department_pts", department_pts),
            ("score", score),
            ("score_breakdown", breakdown),
            ("tier", tier),
        ]))

    stats.rows_kept = len(kept)
    return stats, kept


def write_csv(path: str, rows: list[OrderedDict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CLEAN_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def process(input_path: str, output_path: str, enrich: bool) -> RunStats:
    """CLI entry: read CSV, run pipeline, write cleaned CSV."""
    with open(input_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    stats, kept = process_rows(rows, enrich=enrich)
    write_csv(output_path, kept)
    return stats


# --- Reporting ---------------------------------------------------------------

def print_summary(stats: RunStats, output_path: str) -> None:
    dropped = sum(stats.drops.values())
    print("\n=== RUN SUMMARY ===")
    print(f"Rows in:   {stats.rows_in}")
    print(f"Rows kept: {stats.rows_kept}")
    print(f"Dropped:   {dropped}")
    if stats.drops:
        print("Drop reasons:")
        for reason, count in stats.drops.most_common():
            print(f"  - {reason}: {count}")
    print("Tiers:")
    for tier in ("Tier 1", "Tier 2", "Tier 3"):
        print(f"  - {tier}: {stats.tiers.get(tier, 0)}")
    if stats.enrichment_failures:
        print(f"Enrichment lookups that failed: {stats.enrichment_failures}")
    print(f"Output: {output_path}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Clean, score, enrich, and tier a raw lead CSV.")
    p.add_argument("input", help="Path to raw lead CSV")
    p.add_argument("-o", "--output", default="leads_clean.csv", help="Output CSV path")
    p.add_argument("--no-enrich", action="store_true", help="Skip the HTTP enrichment step")
    args = p.parse_args(argv)

    stats = process(args.input, args.output, enrich=not args.no_enrich)
    print_summary(stats, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
