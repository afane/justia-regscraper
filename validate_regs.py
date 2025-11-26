#!/usr/bin/env python3
"""
Comprehensive validation script for scraped regulation files.

This script validates by TOP-LEVEL sections (Title/Department) so you see results progressively.

Validates:
1. Completeness - All expected records present
2. Order - Records in correct lex_path order
3. Content - Spot-checks random records for actual content

IMPORTANT: This is READ-ONLY. It NEVER modifies the original JSONL files.

Usage:
    python3 validate_regs.py <STATE>
    Example: python3 validate_regs.py MT
"""

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from typing import List, Dict, Optional, Set, Tuple

import cloudscraper
from bs4 import BeautifulSoup

# Import from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from regscraper import (
    extract_links_from_content,
    fetch_with_retry,
    is_reserved_or_repealed,
    JUR_URL_MAP,
)


# ============================================================================
# Section-by-Section Validation
# ============================================================================

def get_top_level_sections(base_url: str, internal_class: str, scraper) -> List[Dict]:
    """
    Get all top-level sections (titles/departments) from the website.

    Returns:
        List of dicts with {name, url, index}
    """
    response = fetch_with_retry(base_url, max_retries=3, scraper=scraper)
    if not response or response.status_code != 200:
        print(f"✗ Failed to fetch base URL: {base_url}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    nav_element = soup.find(class_=internal_class)

    if not nav_element:
        print(f"✗ No navigation found at {base_url}")
        return []

    links = extract_links_from_content(nav_element)
    sections = []

    for i, link in enumerate(links):
        if is_reserved_or_repealed(link["text"]):
            continue

        href = link["href"]
        if href.endswith("//") or "//" in href.replace("://", ""):
            continue

        section_url = f"https://regulations.justia.com{href}"
        if not section_url.endswith('/'):
            section_url += '/'

        sections.append({
            "name": link["text"],
            "url": section_url,
            "index": i
        })

    return sections


def walk_section(section_url: str, section_path: List[int], internal_class: str, scraper, max_depth: int = 20) -> List[str]:
    """
    Walk a single top-level section to get all regulation URLs.

    Returns:
        List of regulation URLs in this section
    """
    urls = []

    def _walk(url: str, path: List[int], depth: int = 0):
        if depth >= max_depth:
            return

        response = fetch_with_retry(url, max_retries=3, scraper=scraper)
        if not response or response.status_code != 200:
            return

        soup = BeautifulSoup(response.content, "html.parser")
        nav_element = soup.find(class_=internal_class)

        if nav_element:
            # Branch node - recurse
            links = extract_links_from_content(nav_element)

            for i, link in enumerate(links):
                if is_reserved_or_repealed(link["text"]):
                    continue

                href = link["href"]
                if href.endswith("//") or "//" in href.replace("://", ""):
                    continue

                child_url = f"https://regulations.justia.com{href}"
                if not child_url.endswith('/'):
                    child_url += '/'

                _walk(child_url, path + [i], depth + 1)
                time.sleep(0.05)  # Faster delay
        else:
            # Leaf node - actual regulation
            urls.append(url)

    _walk(section_url, section_path)
    return urls


def validate_section_completeness(section_name: str, expected_urls: Set[str], actual_urls: Set[str]) -> Tuple[bool, List[str], List[str]]:
    """
    Validate completeness for a single section.

    Returns:
        (is_complete, missing_urls, extra_urls)
    """
    missing = sorted(expected_urls - actual_urls)
    extra = sorted(actual_urls - expected_urls)
    is_complete = len(missing) == 0

    return is_complete, missing, extra


def validate_section_order(section_records: List[Dict]) -> Tuple[bool, List[str]]:
    """
    Validate that records in a section are in correct lex_path order.

    Returns:
        (is_ordered, issues)
    """
    issues = []
    prev_path = None

    for i, record in enumerate(section_records):
        current_path = record["lex_path"]

        if prev_path is not None:
            if prev_path > current_path:
                issues.append(f"  Out of order at record {i}: {prev_path} > {current_path}")

        prev_path = current_path

    return len(issues) == 0, issues


def spot_check_section_content(section_records: List[Dict], scraper, num_samples: int = 10) -> Tuple[int, int, List[str]]:
    """
    Spot-check a few records in this section for content.

    Returns:
        (passed, failed, failure_details)
    """
    if not section_records:
        return 0, 0, []

    # Sample up to num_samples records
    sample_size = min(num_samples, len(section_records))
    samples = random.sample(section_records, sample_size)

    passed = 0
    failed = 0
    failure_details = []

    for record in samples:
        url = record["url"]
        stored_content = record.get("content", "")

        # Quick check: Is there substantial content stored?
        if len(stored_content) < 50:
            failed += 1
            failure_details.append(f"     {url} - Content too short ({len(stored_content)} chars)")
            continue

        # Fetch and compare
        response = fetch_with_retry(url, max_retries=2, scraper=scraper)
        if not response or response.status_code != 200:
            failed += 1
            failure_details.append(f"     {url} - Failed to fetch (HTTP {response.status_code if response else 'timeout'})")
            continue

        soup = BeautifulSoup(response.content, "html.parser")
        main_content = soup.find(id="main-content")

        if not main_content:
            failed += 1
            failure_details.append(f"     {url} - No main-content found on page")
            continue

        # Collect main-content AND sibling divs (same logic as scraper)
        collected_divs = [main_content]
        current = main_content
        while True:
            next_sibling = current.find_next_sibling()
            if not next_sibling:
                break
            if next_sibling.name == "div":
                classes = next_sibling.get("class", [])
                text_preview = next_sibling.get_text(strip=True)[:100]

                # Stop at disclaimer or footer
                if "disclaimer" in classes or "Disclaimer" in text_preview:
                    break
                if "notification" in str(classes).lower() or "footer" in str(classes).lower():
                    break

                # Collect content-indent divs
                if "content-indent" in classes:
                    collected_divs.append(next_sibling)
                    current = next_sibling
                elif any(keyword in text_preview for keyword in ["Section", "subsection", "Rule", "Chapter"]):
                    collected_divs.append(next_sibling)
                    current = next_sibling
                else:
                    break
            else:
                current = next_sibling

        # Combine all collected divs
        combined_html = BeautifulSoup("<div></div>", "html.parser")
        container = combined_html.div
        for div in collected_divs:
            container.append(div)

        # Normalize both for comparison - remove all whitespace
        def normalize(text):
            import re
            text = re.sub(r'\s+', '', text)
            text = text.lower()
            return text

        page_normalized = normalize(container.get_text())
        stored_normalized = normalize(stored_content)

        # Strategy: Check if multiple chunks of stored content exist ANYWHERE on page
        # Page may have promotional junk interspersed that we removed during scraping
        # We sample chunks from different positions and check if they all appear on page

        chunks_to_check = []

        if len(stored_normalized) <= 500:
            # Short content - just check if 80% of it exists on page
            if stored_normalized in page_normalized:
                passed += 1
                continue
            # Try checking if most of the stored content appears
            match_len = 0
            chunk_size = 50
            for i in range(0, len(stored_normalized) - chunk_size, chunk_size):
                if stored_normalized[i:i+chunk_size] in page_normalized:
                    match_len += chunk_size
            if match_len >= len(stored_normalized) * 0.8:
                passed += 1
                continue
            else:
                failed += 1
                failure_details.append(f"     {url} - Content mismatch (only {match_len}/{len(stored_normalized)} chars matched)")
                continue

        # For longer content, sample many small chunks from different positions
        # Smaller chunks are more likely to match despite minor HTML differences
        num_chunks = 20
        chunk_size = 100  # Smaller chunks more forgiving
        step = (len(stored_normalized) - chunk_size) // (num_chunks - 1) if num_chunks > 1 else 0

        for i in range(num_chunks):
            start = i * step
            if start + chunk_size <= len(stored_normalized):
                chunks_to_check.append(stored_normalized[start:start + chunk_size])

        # Count how many chunks are found on the page
        found = sum(1 for chunk in chunks_to_check if chunk in page_normalized)

        # Require at least 60% of chunks to pass (lenient - accounts for page variations)
        required = max(1, int(len(chunks_to_check) * 0.6))
        if found >= required:
            passed += 1
        else:
            failed += 1
            failure_details.append(f"     {url} - Content mismatch (only {found}/{len(chunks_to_check)} chunks found, need {required})")

        time.sleep(0.1)

    return passed, failed, failure_details


# ============================================================================
# Main Validation
# ============================================================================

def validate_state(state_abb: str, jsonl_path: str):
    """
    Validate a state file section by section.
    """
    print(f"\n{'='*80}")
    print(f"Validating {state_abb} Regulations (Section by Section)")
    print(f"{'='*80}\n")

    # Get state configuration
    if state_abb not in JUR_URL_MAP:
        print(f"✗ ERROR: Unknown state '{state_abb}'")
        sys.exit(1)

    state_name_lower = JUR_URL_MAP[state_abb]
    base_url = f"https://regulations.justia.com/states/{state_name_lower}/"
    internal_class = "codes-listing"

    # Create scraper
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'darwin',
            'desktop': True
        }
    )

    # Load actual records from file
    print(f"📂 Loading {jsonl_path}...")
    if not os.path.exists(jsonl_path):
        print(f"✗ ERROR: File not found")
        sys.exit(1)

    all_records = []
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                all_records.append(json.loads(line))

    print(f"  ✓ Loaded {len(all_records)} records\n")

    # Group records by top-level section (first element of lex_path)
    records_by_section = defaultdict(list)
    for record in all_records:
        if record["lex_path"]:
            section_idx = record["lex_path"][0]
            records_by_section[section_idx].append(record)

    # Get top-level sections from website
    print(f"🌐 Getting top-level sections from website...")
    sections = get_top_level_sections(base_url, internal_class, scraper)
    print(f"  ✓ Found {len(sections)} top-level sections\n")

    # Track overall results
    total_complete = 0
    total_incomplete = 0
    total_ordered = 0
    total_unordered = 0
    total_missing = 0
    total_extra = 0

    # Validate each section
    print(f"{'='*80}")
    print(f"VALIDATING SECTIONS")
    print(f"{'='*80}\n")

    for section in sections:
        section_name = section["name"]
        section_idx = section["index"]

        print(f"\n📁 {section_name}")
        print(f"   {'-'*76}")

        # Get expected URLs for this section
        print(f"   Walking section tree...", end=" ", flush=True)
        expected_urls = walk_section(section["url"], [section_idx], internal_class, scraper)
        print(f"found {len(expected_urls)} expected records")

        # Get actual records for this section
        actual_section_records = records_by_section.get(section_idx, [])
        actual_urls = {r["url"] for r in actual_section_records}

        # Validate completeness
        is_complete, missing, extra = validate_section_completeness(
            section_name,
            set(expected_urls),
            actual_urls
        )

        if is_complete:
            print(f"   ✓ Completeness: All {len(expected_urls)} records present")
            total_complete += 1
        else:
            print(f"   ✗ INCOMPLETE: Missing {len(missing)}, Extra {len(extra)} - SCRAPE FAILED")
            total_incomplete += 1
            total_missing += len(missing)
            total_extra += len(extra)

            if missing and len(missing) <= 10:
                for url in missing[:10]:
                    print(f"     MISSING: {url}")
            elif missing:
                print(f"     MISSING: {missing[0]}")
                print(f"     ... and {len(missing)-1} more")

            if extra:
                print(f"   ⚠ WARNING: {len(extra)} extra records not in navigation")

        # Validate order
        is_ordered, order_issues = validate_section_order(actual_section_records)

        if is_ordered:
            print(f"   ✓ Order: All {len(actual_section_records)} records in correct order")
            total_ordered += 1
        else:
            print(f"   ✗ Order: {len(order_issues)} issues found")
            total_unordered += 1
            for issue in order_issues[:3]:
                print(f"     {issue}")
            if len(order_issues) > 3:
                print(f"     ... and {len(order_issues)-3} more")

        # Spot-check content
        if actual_section_records:
            num_checks = min(10, len(actual_section_records))
            print(f"   Spot-checking {num_checks} records...", end=" ", flush=True)
            passed, failed, failure_details = spot_check_section_content(actual_section_records, scraper, num_checks)
            if failed == 0:
                print(f"✓ All {passed} passed")
            else:
                print(f"✗ {passed} passed, {failed} failed")
                for detail in failure_details:
                    print(detail)

    # Final summary
    print(f"\n{'='*80}")
    print(f"FINAL SUMMARY")
    print(f"{'='*80}\n")

    all_good = (total_incomplete == 0 and total_unordered == 0)

    if all_good:
        print(f"✅ {state_abb}.jsonl is VALID - All sections complete and ordered")
    else:
        print(f"❌ {state_abb}.jsonl VALIDATION FAILED")
        if total_incomplete > 0:
            print(f"   ✗ {total_incomplete} sections INCOMPLETE")
        if total_unordered > 0:
            print(f"   ✗ {total_unordered} sections have ORDER issues")

    print(f"\nSections:")
    print(f"  Total: {len(sections)}")
    print(f"  ✓ Complete: {total_complete}")
    if total_incomplete > 0:
        print(f"  ✗ INCOMPLETE: {total_incomplete}")
    print(f"  ✓ Ordered: {total_ordered}")
    if total_unordered > 0:
        print(f"  ✗ UNORDERED: {total_unordered}")

    if total_missing > 0 or total_extra > 0:
        print(f"\nRecords:")
        if total_missing > 0:
            print(f"  ✗ MISSING: {total_missing} records not scraped")
        if total_extra > 0:
            print(f"  ⚠ EXTRA: {total_extra} records not in navigation")

    print()


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Validate scraped regulation files section by section"
    )
    parser.add_argument(
        "state",
        type=str,
        help="State abbreviation (e.g., MT, VT)"
    )
    args = parser.parse_args()

    state_abb = args.state.upper()
    jsonl_path = f"{state_abb}.jsonl"

    if not os.path.exists(jsonl_path):
        print(f"ERROR: Could not find {jsonl_path} in current directory")
        sys.exit(1)

    validate_state(state_abb, jsonl_path)


if __name__ == "__main__":
    main()
