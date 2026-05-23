# Lead List QA & Scoring Tool

A simple Python tool to clean, normalize, score, and tier lead CSVs.

## Features

- Deduplicates by email and same person/company
- Validates email format and drops malformed or free-domain emails
- Normalizes name casing, job titles, departments, and industry values
- Scores each lead using the exact ICP rubric
- Tiers leads into Tier 1 / Tier 2 / Tier 3
- Enriches domain data via a free public API and handles failures gracefully
- Outputs a cleaned CSV plus a summary report

## Usage

```powershell
python lead_score_tool.py --input raw_leads.csv --output cleaned_leads.csv
```

## Output

- `cleaned_leads.csv`: cleaned leads with `score` and `tier`
- Summary printed to stdout

## Notes

- Invalid or free-domain emails are dropped immediately
- Industry buckets are normalized into consistent values such as `Software`, `Financial Services`, `Healthcare`, etc.
- The tool performs one API lookup per distinct email domain and caches results to avoid repeated calls
