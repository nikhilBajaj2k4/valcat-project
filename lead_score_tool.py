import argparse
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
    "msn.com",
    "live.com",
    "mail.com",
    "protonmail.com",
    "yandex.com",
    "zoho.com",
    "gmx.com",
    "mail.ru",
    "fastmail.com",
    "btinternet.com",
    "mailinator.com",
}

INDUSTRY_BUCKETS = [
    ("Software", ["software", "saas", "technology", "tech", "information technology", "it"]),
    ("Financial Services", ["finance", "fintech", "bank", "insurance", "financial"]),
    ("Healthcare", ["health", "medical", "pharma", "biotech", "hospital"]),
    ("Education", ["education", "edtech", "school", "university", "academic"]),
    ("Media", ["media", "advertising", "adtech", "publishing", "broadcast"]),
    ("Manufacturing", ["manufactur", "industrial", "production", "engineer"]),
    ("Professional Services", ["consult", "agency", "service", "law", "accounting", "audit"]),
]

DEPARTMENT_BUCKETS = {
    "sales": ["sales", "business development", "bd", "account executive", "account management", "revenue"],
    "marketing": ["marketing", "growth", "demand generation", "field marketing", "brand", "communications"],
    "revops": ["revops", "revenue operations", "revenue operations", "revenue ops"],
    "customer success": ["customer success", "cs", "client success", "support"],
    "product": ["product", "growth product", "product management"],
    "engineering": ["engineering", "developer", "dev", "software engineer", "tech lead"],
    "operations": ["operations", "ops", "business operations"],
}

TITLE_PATTERNS = [
    ("C-Level", ["chief", "cfo", "ceo", "coo", "cto", "cmo", "cio", "cxo", "chief .* officer"]),
    ("VP", ["vice president", "vp\b", "svp\b", "avp\b", "vp of"]),
    ("Director/Head", ["director", "head of", "head\b", "senior director", "principal", "lead\b"]),
    ("Manager", ["manager", "mgr\b"]),
]

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_RE = re.compile(r"@([^@\s]+)$")
EMPLOYEE_RANGE_RE = re.compile(r"(\d+(?:[\.,]\d+)?)(?:\s*[kK])?\s*(?:[-–to]+\s*(\d+(?:[\.,]\d+)?)(?:\s*[kK])?)?")

API_BASE = "https://api.domainsdb.info/v1/domains/search?domain="


def normalize_text(value):
    if value is None:
        return ""
    normalized = str(value).strip()
    return normalized


def titlecase_name(value):
    if not value:
        return ""
    parts = re.split(r"\s+", value.strip())
    formatted = " ".join(part.capitalize() if part.islower() else part for part in parts)
    return formatted


def normalize_email(email):
    email = normalize_text(email).lower()
    return email


def extract_domain(email):
    match = DOMAIN_RE.search(email)
    return match.group(1).lower() if match else ""


def is_valid_email(email):
    return bool(EMAIL_REGEX.match(email))


def is_free_email(email):
    domain = extract_domain(email)
    if not domain:
        return False
    return domain in FREE_EMAIL_DOMAINS or domain.endswith(".gmail.com") or domain.endswith(".yahoo.com")


def normalize_industry(raw_value):
    value = normalize_text(raw_value).lower()
    if not value:
        return "Other"
    for bucket, keywords in INDUSTRY_BUCKETS:
        if any(keyword in value for keyword in keywords):
            return bucket
    if "software" in value or "saas" in value or "tech" in value:
        return "Software"
    return "Other"


def normalize_department(raw_department, raw_title):
    department = normalize_text(raw_department).lower()
    title = normalize_text(raw_title).lower()
    for bucket, keywords in DEPARTMENT_BUCKETS.items():
        if any(keyword in department for keyword in keywords) or any(keyword in title for keyword in keywords):
            return bucket.title()
    return normalize_text(raw_department).title() or "Other"


def normalize_title(raw_title):
    title = normalize_text(raw_title).lower()
    if not title:
        return ""
    for normalized, patterns in TITLE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, title):
                return normalized
    return "IC"


def parse_employee_count(raw_value):
    raw = normalize_text(raw_value).lower().replace(",", "")
    if not raw:
        return None

    raw = raw.replace("employees", "").replace("people", "").strip()
    raw = raw.replace("k", "000") if re.search(r"\d+[\.,]?\d*\s*k", raw) else raw
    match = EMPLOYEE_RANGE_RE.search(raw)
    if not match:
        try:
            return int(re.sub(r"[^0-9]", "", raw))
        except ValueError:
            return None

    low_text, high_text = match.groups()
    try:
        low = int(float(low_text))
    except ValueError:
        low = None
    high = None
    if high_text:
        try:
            high = int(float(high_text))
        except ValueError:
            high = None
    if low is None:
        return None
    return int((low + (high or low)) / 2)


def score_industry(bucket):
    if bucket == "Software":
        return 30
    if bucket in {"Financial Services", "Healthcare", "Education", "Media", "Professional Services"}:
        return 15
    return 0


def score_employee_count(count):
    if count is None:
        return 5
    if 200 <= count <= 2000:
        return 30
    if 50 <= count <= 199 or 2001 <= count <= 5000:
        return 15
    return 5


def score_seniority(title_bucket):
    if title_bucket in {"C-Level", "VP"}:
        return 25
    if title_bucket == "Director/Head":
        return 15
    if title_bucket == "Manager":
        return 10
    return 0


def score_department(department_bucket):
    return 15 if department_bucket.lower() in {"sales", "marketing", "revops"} else 0


def assign_tier(score):
    if score >= 80:
        return "Tier 1"
    if score >= 55:
        return "Tier 2"
    return "Tier 3"


def lookup_domain_info(domain):
    if not domain:
        return {}
    query = quote_plus(domain)
    url = API_BASE + query
    request = Request(url, headers={"User-Agent": "lead-score-tool/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            domains = data.get("domains") or []
            if not domains:
                return {"enriched_domain_info": "no-data"}
            first = domains[0]
            return {
                "enriched_domain_name": first.get("domain", ""),
                "enriched_country": first.get("country", ""),
                "enriched_create_date": first.get("create_date", ""),
                "enriched_update_date": first.get("update_date", ""),
            }
    except HTTPError as exc:
        if exc.code == 429:
            return {"enriched_domain_info": "rate-limit"}
        return {"enriched_domain_info": "http-error-%s" % exc.code}
    except URLError as exc:
        return {"enriched_domain_info": "url-error"}
    except Exception:
        return {"enriched_domain_info": "enrichment-failed"}


def build_full_name(first, last, raw_name):
    first = normalize_text(first)
    last = normalize_text(last)
    if first or last:
        return titlecase_name((first + " " + last).strip())
    return titlecase_name(normalize_text(raw_name))


def process_leads(rows):
    seen_email = set()
    seen_person_company = set()
    output_rows = []
    summary = {
        "rows_in": 0,
        "rows_kept": 0,
        "rows_dropped": 0,
        "drop_reasons": Counter(),
        "tier_counts": Counter(),
        "api_calls": 0,
        "domains_enriched": 0,
    }
    domain_cache = {}

    for row in rows:
        summary["rows_in"] += 1
        normalized = {k: normalize_text(v) for k, v in row.items()}
        email = normalize_email(normalized.get("email", ""))
        if not email or not is_valid_email(email):
            summary["drop_reasons"]["invalid_email"] += 1
            summary["rows_dropped"] += 1
            continue
        if is_free_email(email):
            summary["drop_reasons"]["free_email"] += 1
            summary["rows_dropped"] += 1
            continue
        if email in seen_email:
            summary["drop_reasons"]["duplicate_email"] += 1
            summary["rows_dropped"] += 1
            continue

        company = normalize_text(normalized.get("company", ""))
        full_name = build_full_name(normalized.get("first_name", ""), normalized.get("last_name", ""), normalized.get("name", ""))
        person_company_key = (full_name.lower(), company.lower())
        if person_company_key in seen_person_company:
            summary["drop_reasons"]["duplicate_person_company"] += 1
            summary["rows_dropped"] += 1
            continue

        seen_email.add(email)
        seen_person_company.add(person_company_key)

        name_parts = full_name.split()
        normalized_first = name_parts[0] if name_parts else ""
        normalized_last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        industry_bucket = normalize_industry(normalized.get("industry", normalized.get("industry_name", "")))
        title_bucket = normalize_title(normalized.get("title", ""))
        department_bucket = normalize_department(normalized.get("department", ""), normalized.get("title", ""))
        employee_count = parse_employee_count(normalized.get("employees", normalized.get("employee_count", "")))

        score = (
            score_industry(industry_bucket)
            + score_employee_count(employee_count)
            + score_seniority(title_bucket)
            + score_department(department_bucket)
        )
        score = min(max(score, 0), 100)
        tier = assign_tier(score)

        email_domain = extract_domain(email)
        domain_info = domain_cache.get(email_domain)
        if domain_info is None:
            domain_info = lookup_domain_info(email_domain)
            summary["api_calls"] += 1
            if any(k.startswith("enriched_") and v for k, v in domain_info.items() if k != "enriched_domain_info"):
                summary["domains_enriched"] += 1
            domain_cache[email_domain] = domain_info
            time.sleep(0.2)

        enriched = {
            "email": email,
            "first_name": normalized_first,
            "last_name": normalized_last,
            "name": full_name,
            "company": company.title(),
            "industry": industry_bucket,
            "title": normalize_text(normalized.get("title", "")),
            "title_bucket": title_bucket,
            "department": department_bucket,
            "employees": employee_count if employee_count is not None else normalize_text(normalized.get("employees", "")),
            "score": score,
            "tier": tier,
            "email_domain": email_domain,
        }
        enriched.update(domain_info)
        enriched.update({
            f"raw_{k}": normalize_text(v)
            for k, v in row.items()
            if k.lower() not in {"email", "first_name", "last_name", "name", "company", "industry", "title", "department", "employees", "employee_count"}
        })

        output_rows.append(enriched)
        summary["rows_kept"] += 1
        summary["tier_counts"][tier] += 1

    return output_rows, summary


def write_csv(output_file, rows):
    if not rows:
        print("No rows to write.")
        return
    fieldnames = list(rows[0].keys())
    try:
        with open(output_file, "w", newline="", encoding="utf-8") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    except IOError as exc:
        print(f"Unable to write output file: {exc}")
        sys.exit(1)


def read_csv(input_file):
    try:
        with open(input_file, newline="", encoding="utf-8") as in_file:
            reader = csv.DictReader(in_file)
            return list(reader)
    except FileNotFoundError:
        print(f"Input file not found: {input_file}")
        sys.exit(1)
    except IOError as exc:
        print(f"Unable to read input file: {exc}")
        sys.exit(1)


def print_summary(summary):
    print("\nRun Summary")
    print("-----------")
    print(f"Rows in: {summary['rows_in']}")
    print(f"Rows kept: {summary['rows_kept']}")
    print(f"Rows dropped: {summary['rows_dropped']}")
    print("Drop reasons:")
    for reason, count in summary["drop_reasons"].items():
        print(f"  - {reason}: {count}")
    print("Counts per tier:")
    for tier in ["Tier 1", "Tier 2", "Tier 3"]:
        print(f"  - {tier}: {summary['tier_counts'].get(tier, 0)}")
    print(f"API calls performed: {summary['api_calls']}")
    print(f"Domains enriched: {summary['domains_enriched']}")


def main():
    parser = argparse.ArgumentParser(description="Lead list QA and scoring tool")
    parser.add_argument("--input", default="raw_leads.csv", help="Path to raw input CSV")
    parser.add_argument("--output", default="cleaned_leads.csv", help="Path to cleaned output CSV")
    args = parser.parse_args()

    rows = read_csv(args.input)
    output_rows, summary = process_leads(rows)
    write_csv(args.output, output_rows)
    print_summary(summary)


if __name__ == "__main__":
    main()
