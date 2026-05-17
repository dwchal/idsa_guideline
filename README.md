# IDSA Guidelines Scraper

A daily Python script that tracks [IDSA practice guidelines](https://www.idsociety.org/practice-guideline/), downloads freely available PDFs, and publishes an up-to-date markdown report to this repository.

## What it does

Each run:

1. Scrapes the full guidelines listing from `idsociety.org/practice-guideline/`
2. Visits each guideline's detail page to collect title, year, status, DOI, journal, and any freely hosted PDF links
3. Downloads PDFs served from IDSA's own CDN (`/globalassets/idsa/...`) — supplementary tables, figures, archived versions, and executive summaries
4. Skips paywalled journal PDFs (Oxford Academic / Clinical Infectious Diseases); records their DOIs instead
5. Writes `REPORT.md` with year-by-year statistics, status breakdown, and a full guidelines table
6. Commits and pushes `REPORT.md` only when it has changed

State is tracked in `state.json` (not committed) so subsequent runs only process new or changed items.

## Report

See [`REPORT.md`](REPORT.md) for the latest snapshot:

- Guidelines published per year (2003–present)
- Status breakdown: Current / Archived / Endorsed / In Development
- Recent changes since last run
- Full table with DOI links and PDF counts per guideline

## Setup

```bash
pip install -r requirements.txt
```

**Dependencies:** `requests`, `beautifulsoup4`, `lxml`

## Usage

```bash
python scraper.py
```

Run this from the root of the repository. It will create:

```
pdfs/          # Downloaded PDFs, organized by year (gitignored)
state.json     # Download tracking state (gitignored)
REPORT.md      # Updated report (committed)
```

### Run daily via cron

```bash
# Run at 6 AM every day
0 6 * * * cd /path/to/idsa_guideline && python scraper.py >> scraper.log 2>&1
```

### Run daily via GitHub Actions

```yaml
# .github/workflows/scrape.yml
on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python scraper.py
        env:
          GIT_AUTHOR_NAME: github-actions[bot]
          GIT_AUTHOR_EMAIL: github-actions[bot]@users.noreply.github.com
          GIT_COMMITTER_NAME: github-actions[bot]
          GIT_COMMITTER_EMAIL: github-actions[bot]@users.noreply.github.com
```

## Notes

- The script sleeps 1.5 seconds between requests to avoid overloading the IDSA server
- Failed requests are retried up to 3 times with exponential backoff
- Most full-text guidelines are paywalled on Oxford Academic / Clinical Infectious Diseases; the freely downloadable files are supplementary materials hosted directly on IDSA's site
