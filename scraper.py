#!/usr/bin/env python3
"""
IDSA Guidelines Scraper
Daily script to download new IDSA practice guideline PDFs and update a GitHub
markdown report with statistics and metadata.
"""

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.idsociety.org"
LISTING_URL = f"{BASE_URL}/practice-guideline/"
STATE_FILE = Path("state.json")
REPORT_FILE = Path("REPORT.md")
PDFS_DIR = Path("pdfs")
REQUEST_DELAY = 1.5  # seconds between requests

HEADERS = {
    "User-Agent": (
        "IDSAGuidelineScraper/1.0 "
        "(automated research tool; https://github.com/dwchal/idsa_guideline)"
    )
}


def make_request(url: str, retries: int = 3) -> requests.Response | None:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"  Request failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Failed after {retries} attempts: {url} — {e}")
    return None


PMC_IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
PMC_OA_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
PMC_HEADERS = {
    "User-Agent": (
        "IDSAGuidelineScraper/1.0 "
        "(automated research tool; https://github.com/dwchal/idsa_guideline)"
    )
}


def doi_to_pmcid(doi: str) -> str | None:
    """Convert a DOI to a PubMed Central ID using the NCBI ID Converter API."""
    # Strip URL prefix if present
    doi_clean = re.sub(r"^https?://doi\.org/", "", doi)
    try:
        resp = requests.get(
            PMC_IDCONV_URL,
            params={"ids": doi_clean, "format": "json", "tool": "IDSAScraper"},
            headers=PMC_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        records = data.get("records", [])
        if records and "pmcid" in records[0]:
            return records[0]["pmcid"]
    except Exception:
        pass
    return None


def pmc_pdf_url(pmcid: str) -> str | None:
    """Check PMC Open Access API for a free PDF download URL."""
    try:
        resp = requests.get(
            PMC_OA_URL,
            params={"id": pmcid},
            headers=PMC_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        for link in root.iter("link"):
            if link.attrib.get("format") == "pdf":
                ftp_url = link.attrib.get("href", "")
                # Convert FTP to HTTPS
                https_url = ftp_url.replace(
                    "ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov"
                )
                return https_url
    except Exception:
        pass
    return None


def fetch_guidelines_list() -> list[dict]:
    """Scrape the main IDSA guidelines listing page."""
    print(f"Fetching guidelines list from {LISTING_URL}")
    resp = make_request(LISTING_URL)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    guidelines = []

    # Guidelines are listed with anchor tags; each entry has title, year, status badges
    # Look for guideline entries — they appear as links within list items or divs
    # that contain year text and status badges
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not href.startswith("/practice-guideline/"):
            continue
        # Skip the root listing page itself
        if href.strip("/") == "practice-guideline":
            continue
        # Skip anchors / non-page links
        if "#" in href or href == LISTING_URL:
            continue

        title = link.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # Walk up to find the container with year and status info
        parent = link.parent
        for _ in range(4):
            if parent is None:
                break
            text = parent.get_text(" ", strip=True)
            year_match = re.search(r"\b(19|20)\d{2}\b", text)
            if year_match:
                break
            parent = parent.parent

        year = None
        status_tags = []
        container_text = ""

        if parent:
            container_text = parent.get_text(" ", strip=True)
            year_match = re.search(r"\b(19|20)\d{2}\b", container_text)
            if year_match:
                year = int(year_match.group())

            # Status badges appear as spans or divs with class names or text
            for badge in parent.find_all(["span", "div", "li"]):
                badge_text = badge.get_text(strip=True)
                for status in ("Current", "Archived", "In Development", "Endorsed"):
                    if status in badge_text and status not in status_tags:
                        status_tags.append(status)

        # Filter out nav/utility links that aren't actual guidelines
        nav_titles = {
            "guidelines", "search all guidelines", "practice guidelines library",
            "a-z guideline listing", "view all practice guidelines",
            "all guidelines", "practice guidelines",
            "looking for practice guidelines?",
        }
        if title.lower() in nav_titles:
            continue

        # Deduplicate by href (same page linked multiple times)
        if any(g["detail_url"] == href for g in guidelines):
            continue

        guidelines.append(
            {
                "title": title,
                "year": year,
                "status": ", ".join(status_tags) if status_tags else "Unknown",
                "detail_url": href,
                "full_url": urljoin(BASE_URL, href),
            }
        )

    print(f"Found {len(guidelines)} guidelines on listing page")
    return guidelines


def fetch_guideline_detail(guideline: dict) -> dict:
    """Fetch a guideline detail page and extract metadata and PDF links."""
    url = guideline["full_url"]
    resp = make_request(url)
    if not resp:
        return guideline

    soup = BeautifulSoup(resp.text, "lxml")
    detail = dict(guideline)

    # DOI links
    doi = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "doi.org" in href:
            doi = href
            break
    detail["doi"] = doi

    # Journal name — often appears near DOI or in citation text
    journal = None
    for pattern in [
        r"Clinical Infectious Diseases",
        r"Journal of Infectious Diseases",
        r"American Journal of",
        r"Clinical Microbiology",
        r"Pediatric Infectious Disease",
        r"Open Forum Infectious",
    ]:
        if re.search(pattern, resp.text, re.IGNORECASE):
            m = re.search(pattern, resp.text, re.IGNORECASE)
            journal = m.group() if m else None
            break
    detail["journal"] = journal

    # Publication date — look for date patterns in page text
    pub_date = None
    date_patterns = [
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+20\d{2}\b",
        r"\b20\d{2}-\d{2}-\d{2}\b",
    ]
    for pat in date_patterns:
        m = re.search(pat, resp.text)
        if m:
            pub_date = m.group()
            break
    detail["publication_date"] = pub_date

    # If year wasn't found on listing page, extract it from detail page date
    if not detail.get("year") and pub_date:
        year_m = re.search(r"\b(20\d{2})\b", pub_date)
        if year_m:
            detail["year"] = int(year_m.group(1))

    # PDF links — IDSA-hosted (freely downloadable)
    idsa_pdfs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/globalassets/" in href and href.endswith(".pdf"):
            full_pdf_url = urljoin(BASE_URL, href) if href.startswith("/") else href
            if full_pdf_url not in idsa_pdfs:
                idsa_pdfs.append(full_pdf_url)
    detail["idsa_pdf_urls"] = idsa_pdfs

    # Oxford Academic / journal URLs
    journal_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(
            domain in href
            for domain in ["academic.oup.com", "pubmed.ncbi", "pmc.ncbi"]
        ):
            if href not in journal_urls:
                journal_urls.append(href)
    detail["journal_urls"] = journal_urls

    # PMC open-access PDF — resolve DOI → PMCID → free PDF URL
    detail["pmc_pdf_url"] = None
    if doi:
        pmcid = doi_to_pmcid(doi)
        if pmcid:
            detail["pmcid"] = pmcid
            detail["pmc_pdf_url"] = pmc_pdf_url(pmcid)
            if detail["pmc_pdf_url"]:
                print(f"  PMC full-text available: {pmcid}")

    # Lead authors — look for common patterns
    authors = None
    for tag in soup.find_all(["p", "div", "span"]):
        text = tag.get_text(strip=True)
        # Author lines often contain "et al." or list names with commas
        if re.search(r"\bet al\.\b", text) and len(text) < 300:
            authors = text
            break
    detail["authors"] = authors

    return detail


def slugify(text: str) -> str:
    """Convert a string to a filesystem-safe slug."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80]


def download_pdf(
    url: str, year: int | None, slug: str, already_downloaded_urls: set[str]
) -> Path | None:
    """Download a PDF to pdfs/<year>/<filename>. Returns path or None if skipped."""
    if url in already_downloaded_urls:
        return None  # already tracked in state

    year_dir = PDFS_DIR / str(year if year else "unknown")
    year_dir.mkdir(parents=True, exist_ok=True)

    url_path = urlparse(url).path
    filename = Path(url_path).name or f"{slug}.pdf"
    if not filename.endswith(".pdf"):
        filename += ".pdf"

    dest = year_dir / filename
    if dest.exists():
        return dest  # file exists but URL not in state — re-register it

    print(f"  Downloading PDF: {filename}")
    resp = make_request(url)
    if not resp:
        return None

    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type and not url.endswith(".pdf"):
        print(f"  Skipping — not a PDF (Content-Type: {content_type})")
        return None

    dest.write_bytes(resp.content)
    print(f"  Saved to {dest}")
    return dest


def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"last_run": None, "guidelines": {}}


def save_state(state: dict):
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2, default=str)


def generate_report(
    guidelines: list[dict],
    new_items: list[str],
    updated_items: list[str],
    last_run: str | None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Statistics ---
    years = [g["year"] for g in guidelines if g.get("year")]
    year_counts = Counter(years)
    sorted_years = sorted(year_counts.keys(), reverse=True)

    # Status breakdown
    status_counter: Counter = Counter()
    for g in guidelines:
        for s in g.get("status", "Unknown").split(", "):
            status_counter[s.strip()] += 1

    total = len(guidelines)
    # pdfs_downloaded stores URLs of downloaded files
    total_pdfs = sum(len(g.get("pdfs_downloaded", [])) for g in guidelines)

    # --- Build report ---
    lines = [
        "# IDSA Practice Guidelines Report",
        "",
        f"_Last updated: {now} — auto-generated daily by [scraper.py](scraper.py)_",
        "",
        "---",
        "",
        "## Summary Statistics",
        "",
        f"**Total guidelines tracked:** {total}  ",
        f"**Total PDFs downloaded:** {total_pdfs}  ",
        f"**Previous run:** {last_run or 'N/A'}  ",
        "",
        "### Guidelines Published per Year",
        "",
        "| Year | Count |",
        "|------|-------|",
    ]

    for year in sorted_years:
        lines.append(f"| {year} | {year_counts[year]} |")

    unknown_count = sum(1 for g in guidelines if not g.get("year"))
    if unknown_count:
        lines.append(f"| Unknown | {unknown_count} |")

    lines += [
        "",
        "### Status Breakdown",
        "",
    ]
    for status in ("Current", "Archived", "Endorsed", "In Development", "Unknown"):
        count = status_counter.get(status, 0)
        if count:
            lines.append(f"- **{status}:** {count}")

    # --- Recent changes ---
    lines += ["", "---", "", "## Recent Changes", ""]
    if new_items:
        lines.append("### New Guidelines")
        for item in new_items:
            lines.append(f"- {item}")
        lines.append("")
    if updated_items:
        lines.append("### Updated (new PDFs downloaded)")
        for item in updated_items:
            lines.append(f"- {item}")
        lines.append("")
    if not new_items and not updated_items:
        lines.append("_No changes since last run._")
        lines.append("")

    # --- Full table ---
    lines += [
        "---",
        "",
        "## All Guidelines",
        "",
        "| Title | Year | Status | DOI | Full Text | PDFs |",
        "|-------|------|--------|-----|-----------|------|",
    ]

    sorted_guidelines = sorted(
        guidelines,
        key=lambda g: (-(g.get("year") or 0), g.get("title", "")),
    )

    for g in sorted_guidelines:
        title = g.get("title", "").replace("|", "\\|")
        year = g.get("year", "—")
        status = g.get("status", "Unknown")
        doi = g.get("doi", "")
        doi_cell = f"[DOI]({doi})" if doi else "—"
        pmcid = g.get("pmcid", "")
        pmc_cell = f"[PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/)" if pmcid else "—"
        pdf_count = len(g.get("pdfs_downloaded", []))
        pdf_cell = str(pdf_count) if pdf_count else "—"
        lines.append(f"| {title} | {year} | {status} | {doi_cell} | {pmc_cell} | {pdf_cell} |")

    lines.append("")
    return "\n".join(lines)


def git_commit_and_push(message: str):
    """Commit REPORT.md if changed and push to remote."""
    # Check if there are changes to REPORT.md
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "REPORT.md"],
        capture_output=True,
    )
    # exit code 1 = there are differences; also handle untracked files
    status = subprocess.run(
        ["git", "status", "--porcelain", "REPORT.md"],
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        print("REPORT.md unchanged — skipping commit")
        return

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    cmds = [
        ["git", "add", "REPORT.md", ".gitignore", "requirements.txt", "scraper.py"],
        ["git", "commit", "-m", message],
        ["git", "push", "-u", "origin", branch],
    ]

    for cmd in cmds:
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  stderr: {result.stderr.strip()}")
            # Retry push up to 4 times with backoff
            if cmd[1] == "push":
                for wait in (2, 4, 8, 16):
                    print(f"  Push failed, retrying in {wait}s...")
                    time.sleep(wait)
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        break
                else:
                    print("  Push failed after retries.")
                    return
        if result.stdout:
            print(f"  {result.stdout.strip()}")


def main():
    print(f"=== IDSA Guideline Scraper — {datetime.now().isoformat()} ===\n")

    state = load_state()
    last_run = state.get("last_run")
    known = state.get("guidelines", {})

    # 1. Fetch guidelines listing
    guidelines = fetch_guidelines_list()
    if not guidelines:
        print("No guidelines found — aborting.")
        sys.exit(1)

    new_items = []
    updated_items = []
    all_enriched = []

    # 2. Process each guideline
    for i, g in enumerate(guidelines, 1):
        key = g["detail_url"]
        is_new = key not in known
        print(f"\n[{i}/{len(guidelines)}] {g['title']} ({g.get('year', '?')})")

        time.sleep(REQUEST_DELAY)
        detail = fetch_guideline_detail(g)

        # Merge with existing state
        existing = known.get(key, {})
        if is_new:
            detail["first_seen"] = datetime.now().date().isoformat()
            new_items.append(f"**{detail['title']}** ({detail.get('year', '?')})")
        else:
            detail["first_seen"] = existing.get("first_seen")

        detail["last_updated"] = datetime.now().date().isoformat()
        # pdfs_downloaded tracks URLs (not paths) to avoid re-downloading on year change
        detail["pdfs_downloaded"] = existing.get("pdfs_downloaded", [])
        already_downloaded_urls = set(detail["pdfs_downloaded"])

        # 3. Download freely available PDFs
        newly_downloaded = []
        slug = slugify(detail["title"])

        # IDSA-hosted supplementary PDFs
        for pdf_url in detail.get("idsa_pdf_urls", []):
            dest = download_pdf(pdf_url, detail.get("year"), slug, already_downloaded_urls)
            if dest:
                newly_downloaded.append(pdf_url)
                already_downloaded_urls.add(pdf_url)
                time.sleep(REQUEST_DELAY)

        # PMC open-access full-text PDF
        pmc_url = detail.get("pmc_pdf_url")
        if pmc_url:
            dest = download_pdf(pmc_url, detail.get("year"), f"{slug}-fulltext", already_downloaded_urls)
            if dest:
                newly_downloaded.append(pmc_url)
                already_downloaded_urls.add(pmc_url)
                time.sleep(REQUEST_DELAY)

        if newly_downloaded:
            detail["pdfs_downloaded"] = detail["pdfs_downloaded"] + newly_downloaded
            if not is_new:
                updated_items.append(
                    f"**{detail['title']}** — {len(newly_downloaded)} new PDF(s)"
                )

        known[key] = detail
        all_enriched.append(detail)

    # 4. Generate report
    print("\n=== Generating report ===")
    report_md = generate_report(all_enriched, new_items, updated_items, last_run)
    REPORT_FILE.write_text(report_md, encoding="utf-8")
    print(f"Wrote {REPORT_FILE}")

    # 5. Save state
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["guidelines"] = known
    save_state(state)
    print(f"Saved state to {STATE_FILE}")

    # 6. Commit and push
    total_new = len(new_items)
    total_updated = len(updated_items)
    commit_msg = (
        f"chore: update IDSA guidelines report ({datetime.now().strftime('%Y-%m-%d')})"
    )
    if total_new or total_updated:
        commit_msg = (
            f"feat: add {total_new} new, {total_updated} updated IDSA guidelines "
            f"({datetime.now().strftime('%Y-%m-%d')})"
        )

    print("\n=== Committing and pushing ===")
    git_commit_and_push(commit_msg)

    print("\n=== Done ===")
    print(f"  Guidelines tracked: {len(all_enriched)}")
    print(f"  New this run: {total_new}")
    print(f"  Updated this run: {total_updated}")
    total_pdfs = sum(len(g.get("pdfs_downloaded", [])) for g in all_enriched)
    print(f"  Total PDFs downloaded: {total_pdfs}")


if __name__ == "__main__":
    main()
