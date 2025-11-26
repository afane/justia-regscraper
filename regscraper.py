"""
Regulations scraper for Justia regulations website.
Scrapes administrative rules organized by department instead of title.
"""

import argparse
import json
import os
import queue
import re
import threading
import time
from io import TextIOWrapper
from typing import Optional

try:
    import cloudscraper
    USE_CLOUDSCRAPER = True
except ImportError:
    import requests
    USE_CLOUDSCRAPER = False
    print("WARNING: cloudscraper not installed. Install it with: pip install cloudscraper")
    print("WARNING: Falling back to requests (may not work with Cloudflare protection)\n")

from bs4 import BeautifulSoup, PageElement
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_not_result,
    retry_if_exception_type,
)
from tqdm import tqdm


# ============================================================================
# Constants and Configuration
# ============================================================================

# Base URLs
JUSTIA_BASE_URL = "https://law.justia.com"
CODES_BASE_URL = "https://codes.findlaw.com"
REGULATIONS_BASE_URL = "https://regulations.justia.com"

# Headers for requests - using more realistic browser headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0"
}

# State URL mappings
JUR_URL_MAP = {
    "AL": "alabama",
    "AK": "alaska",
    "AZ": "arizona",
    "AR": "arkansas",
    "CA": "california",
    "CO": "colorado",
    "CT": "connecticut",
    "DE": "delaware",
    "FL": "florida",
    "GA": "georgia",
    "HI": "hawaii",
    "ID": "idaho",
    "IL": "illinois",
    "IN": "indiana",
    "IA": "iowa",
    "KS": "kansas",
    "KY": "kentucky",
    "LA": "louisiana",
    "ME": "maine",
    "MD": "maryland",
    "MA": "massachusetts",
    "MI": "michigan",
    "MN": "minnesota",
    "MS": "mississippi",
    "MO": "missouri",
    "MT": "montana",
    "NE": "nebraska",
    "NV": "nevada",
    "NH": "new-hampshire",
    "NJ": "new-jersey",
    "NM": "new-mexico",
    "NY": "new-york",
    "NC": "north-carolina",
    "ND": "north-dakota",
    "OH": "ohio",
    "OK": "oklahoma",
    "OR": "oregon",
    "PA": "pennsylvania",
    "RI": "rhode-island",
    "SC": "south-carolina",
    "SD": "south-dakota",
    "TN": "tennessee",
    "TX": "texas",
    "UT": "utah",
    "VT": "vermont",
    "VA": "virginia",
    "WA": "washington",
    "WV": "west-virginia",
    "WI": "wisconsin",
    "WY": "wyoming",
    "DC": "district-of-columbia",
    "AS": "american-samoa",
    "GU": "guam",
    "MP": "northern-mariana-islands",
    "PR": "puerto-rico",
    "VI": "us-virgin-islands",
}


# ============================================================================
# Helper Functions
# ============================================================================


def _is_good_response(response):
    """Check if response is successful (status 200)."""
    return response is not None and response.status_code == 200


def is_reserved_or_repealed(text: str) -> bool:
    """
    Check if a section is RESERVED or REPEALED and should be skipped.

    Args:
        text (str): The text to check (usually chapter or section title)

    Returns:
        bool: True if the text contains RESERVED or REPEALED markers
    """
    text_upper = text.upper()
    return "(REPEALED)" in text_upper or "(RESERVED)" in text_upper or "RESERVED" in text_upper


def fetch_with_retry(url: str, max_retries: int = 3, delay: float = 1.0, request_delay: float = 0.1, scraper=None):
    """
    Fetch a URL with exponential backoff retry logic using Tenacity.

    Args:
        url (str): The URL to fetch
        max_retries (int): Maximum number of retry attempts (default: 3)
        delay (float): Initial delay between retries in seconds (default: 1.0)
        request_delay (float): Delay before each request in seconds (default: 1.0)
        scraper: Optional cloudscraper instance to reuse

    Returns:
        requests.Response or None: Response object if successful, None if all retries failed
    """
    # Create scraper instance if not provided
    if scraper is None:
        if USE_CLOUDSCRAPER:
            scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'darwin',
                    'desktop': True
                }
            )
        else:
            scraper = requests.Session()
            scraper.headers.update(HEADERS)

    @retry(
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential(multiplier=delay, min=delay, max=60),
        retry=retry_if_not_result(_is_good_response),
        reraise=False,
    )
    def _fetch():
        try:
            # Add a small delay before each request to avoid rate limiting
            time.sleep(request_delay)
            response = scraper.get(url, timeout=30)

            # Print all non-200 status codes
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code} error for {url}"
                if response.status_code == 403:
                    error_msg += " - FORBIDDEN: Access denied"
                elif response.status_code == 404:
                    error_msg += " - NOT FOUND: Page doesn't exist"
                elif response.status_code == 429:
                    error_msg += " - RATE LIMITED: Too many requests"
                elif response.status_code == 500:
                    error_msg += " - SERVER ERROR: Internal server error"
                elif response.status_code == 503:
                    error_msg += " - SERVICE UNAVAILABLE: Server temporarily unavailable"
                print(error_msg)

            # Handle Retry-After header for 429 rate limiting
            if response.status_code == 429:
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    try:
                        wait_time = int(retry_after)
                        print(f"Rate limited. Waiting {wait_time}s as requested by server...")
                        time.sleep(wait_time)
                    except ValueError:
                        print("Rate limited. Waiting 60s...")
                        time.sleep(60)

            return response
        except Exception as e:
            error_type = type(e).__name__
            if "Timeout" in error_type:
                print(f"TIMEOUT error for {url}: Request timed out after 30s")
            elif "Connection" in error_type:
                print(f"CONNECTION error for {url}: {e}")
            else:
                print(f"REQUEST error for {url}: {error_type}: {e}")
            return None

    try:
        return _fetch()
    except Exception as e:
        print(f"UNEXPECTED error for {url}: {e}")
        return None


def extract_links_from_content(content: PageElement) -> list:
    """
    Extract all links from the given BeautifulSoup PageElement.

    Args:
        content (PageElement): The HTML content (usually the result of soup.find()).

    Returns:
        List[Dict]: A list of dictionaries with link text and href.
    """
    links = []

    # Find all <a> tags in the content
    for a_tag in content.find_all("a", href=True):
        link_text = a_tag.get_text(strip=True)
        link_href = a_tag["href"]

        # Store the link text and URL in a dictionary
        links.append({"text": link_text, "href": link_href})

    return links


def process_regulation_leaf(
    state_name: str,
    state_abb: str,
    url: str,
    jsonl_fp: Optional[TextIOWrapper],
    lex_path: Optional[list[int]] = None,
    lock: Optional[threading.Lock] = None,
    pbar: Optional[tqdm] = None,
    max_retries: int = 3,
    scraper=None,
) -> dict:
    """
    Process the content of a leaf node (individual regulation).

    Args:
        state_name (str): Full state name
        state_abb (str): State abbreviation
        url (str): The URL of the leaf node
        jsonl_fp (TextIOWrapper): The file pointer to write the JSONL records to
        lex_path (list[int]): The lexicographical path to the leaf node
        lock (threading.Lock): A lock to make file writes thread-safe
        pbar (tqdm): A tqdm progress bar to update
        max_retries (int): Maximum number of retry attempts for failed requests
        scraper: Shared cloudscraper instance

    Returns:
        dict: A dictionary containing the regulation data
    """
    response = fetch_with_retry(url, max_retries=max_retries, scraper=scraper)
    if response and response.status_code == 200:
        soup: BeautifulSoup = BeautifulSoup(response.content, "html.parser")

        # Extract breadcrumb path
        sep = soup.find("span", class_="breadcrumb-sep").get_text(strip=True)
        assert ord(sep) == 8250, "Separator is not the right character."
        path_str = soup.find("nav", class_="breadcrumbs").get_text(strip=True)
        path_arr = path_str.split(sep)

        # Filter out the "Justia › U.S. Law › U.S. Regulations" prefix
        # Keep only from the state regulations code onwards (e.g., "Administrative Rules of Montana", "Code of Vermont Rules", etc.)
        filtered_path = []
        start_collecting = False
        for segment in path_arr:
            segment = segment.strip()
            # Start collecting when we find a segment that contains "Rules" or "Code" and isn't just "U.S. Regulations"
            if not start_collecting and ("Rules" in segment or "Code" in segment) and segment != "U.S. Regulations":
                start_collecting = True
            if start_collecting:
                filtered_path.append(segment)

        # Create path string with › separator like the example
        clean_path = "›".join(filtered_path)

        # Extract title - use › separator consistently
        title_str = soup.find("h1").get_text(" › ", strip=True)

        # Extract citation if available
        has_univ_cite = False
        citation = None
        if wrapper := soup.find("div", class_="has-margin-bottom-20"):
            has_univ_cite = (
                wrapper.find("b").get_text(strip=True) == "Universal Citation:"
            )
        if cite_tag := soup.find(href="/citations.html"):
            citation = cite_tag.get_text(strip=True)

        # Extract content - Justia's HTML structure is broken with content scattered across multiple divs
        # Strategy: Find main-content div, then collect all sibling content-indent divs until we hit disclaimer

        # First remove unwanted elements from the entire page
        for elem in soup.find_all(["header", "footer", "nav", "script", "style", "noscript"]):
            elem.decompose()

        main_content = soup.find(id="main-content")
        if main_content:
            # Remove disclaimer, newsletter signup, and other junk INSIDE main-content first
            # These are promotional/footer elements that Justia embeds in the content
            for elem in main_content.find_all("div", class_="disclaimer"):
                elem.decompose()

            # Remove any div containing footer/promotional keywords
            junk_keywords = [
                "Disclaimer", "reCAPTCHA", "Free Daily Summaries", "Newsletter",
                "Sign Up", "Enter Your Email", "Ask a Lawyer", "Find a Lawyer",
                "Get Listed", "Justia Legal Resources", "Justia Connect",
                "Privacy Policy", "Terms of Service", "Google", "CLE Credits",
                "Webinars", "Toggle button", "Lawyers - Get Listed",
                "Get free summaries", "Free Answers", "Our Suggestions"
            ]

            for elem in main_content.find_all("div"):
                text = elem.get_text(strip=True)
                # Remove if it contains junk keywords and is relatively short (footer elements)
                # Long divs might legitimately mention these terms in regulation text
                if len(text) < 2000 and any(keyword in text for keyword in junk_keywords):
                    elem.decompose()

            # Remove notification banners
            for elem in main_content.find_all("div", id=lambda x: x and "notification" in x.lower() if x else False):
                elem.decompose()

            # Collect main-content and all sibling divs containing regulation content
            # Stop when we hit disclaimer or non-content divs
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

                    # Collect content-indent divs (these contain continuation of regulation text)
                    if "content-indent" in classes:
                        collected_divs.append(next_sibling)
                        current = next_sibling
                    # Also collect divs that look like regulation content
                    elif any(keyword in text_preview for keyword in ["Section", "subsection", "State Treasurer", "taxpayer"]):
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

            # Clean up unwanted elements
            for elem in container.find_all("h1"):
                elem.decompose()
            for elem in container.find_all("div", class_="has-margin-bottom-20"):
                elem.decompose()
            for elem in container.find_all(class_="breadcrumbs"):
                elem.decompose()

            content = container.get_text(separator="\n", strip=False)

            # Clean up whitespace
            lines = content.split('\n')
            cleaned_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped:
                    cleaned_lines.append(stripped)
                elif cleaned_lines and cleaned_lines[-1] != '':
                    cleaned_lines.append('')

            while cleaned_lines and cleaned_lines[-1] == '':
                cleaned_lines.pop()

            content = '\n'.join(cleaned_lines)
        else:
            content = ""

        # Create record matching the required format
        record = {
            "url": url,
            "state": state_abb,
            "path": clean_path,
            "title": title_str,
            "univ_cite": has_univ_cite,
            "citation": citation,
            "content": content,
            "lex_path": lex_path,
        }

        if jsonl_fp:
            with lock:
                jsonl_fp.write(json.dumps(record, ensure_ascii=False))
                jsonl_fp.write("\n")
        if pbar is not None:
            try:
                with lock:
                    pbar.update(1)
            except Exception as pbar_error:
                # Handle tqdm errors gracefully (e.g., version compatibility issues)
                pass
    else:
        status_code = response.status_code if response else "No response"
        print(f"Failed to retrieve content for {url}, Status Code: {status_code}")
        with lock:
            failed_file = f"failed_{state_abb}.txt"
            with open(failed_file, "a") as f:
                f.write(f"{url}\n")


def get_last_lex_path(state_abb: str) -> Optional[list[int]]:
    """
    Get the lexicographical path of the last successfully scraped entry.

    Args:
        state_abb (str): The state abbreviation to check.

    Returns:
        list[int] | None: The last lex_path, or None if the file doesn't exist/is empty.
    """
    save_dir = "regs"
    save_path = f"{save_dir}/{state_abb}.jsonl"
    if not os.path.exists(save_path) or os.stat(save_path).st_size == 0:
        return None

    with open(save_path, "rb") as f:
        try:  # catch OSError in case of a one line file
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b"\n":
                f.seek(-2, os.SEEK_CUR)
        except OSError:
            f.seek(0)
        last_line = f.readline().decode()

    return json.loads(last_line).get("lex_path")


def scrape_branch(
    url: str,
    path: list[int],
    continue_from: Optional[list[int]],
    state_name: str,
    state_abb: str,
    jsonl_fp: TextIOWrapper,
    site_url: str,
    internal_class: str,
    lock: threading.Lock,
    visited_urls: set,
    pbar: Optional[tqdm] = None,
    dept_pbar: Optional[tqdm] = None,
    max_retries: int = 3,
    scraper=None,
):
    """
    Recursively scrapes a branch of the regulations website.
    Skips RESERVED and REPEALED sections.
    Tracks visited URLs to prevent infinite loops.
    """
    # Check if we've already visited this URL (prevents circular references)
    with lock:
        if url in visited_urls:
            return
        visited_urls.add(url)

    response = fetch_with_retry(url, max_retries=max_retries, scraper=scraper)
    if response and response.status_code == 200:
        soup: BeautifulSoup = BeautifulSoup(response.content, "html.parser")
        internal_links_element = soup.find(
            class_=internal_class
        )  # these will be URLs relative to the base_url
        if internal_links_element:  # This is a branch node
            links = extract_links_from_content(internal_links_element)

            start_idx = 0
            # If resuming and current path is a prefix of the target resume path
            if continue_from and path == continue_from[: len(path)]:
                # Set the starting index for links at this level
                if len(path) < len(continue_from):
                    start_idx = continue_from[len(path)]

            for i, link in enumerate(links):
                if i < start_idx:
                    continue

                # Skip RESERVED and REPEALED sections
                if is_reserved_or_repealed(link["text"]):
                    continue

                href = link["href"]

                # Skip malformed URLs (double slashes, empty paths, circular references)
                # These cause infinite recursion loops
                if href.endswith("//") or "//" in href.replace("://", ""):
                    print(f"WARNING: Skipping malformed URL: {site_url}{href}")
                    continue

                # Prevent infinite recursion by checking if we've exceeded reasonable path depth
                # Most regulations are 5-6 levels deep; 20 is a safe upper limit
                if len(path) >= 20:
                    print(f"WARNING: Excessive path depth ({len(path)}) at {url}, stopping recursion")
                    break

                new_path = path + [i]

                # If we move past the resume index, disable resume logic for subsequent branches
                new_continue_from = continue_from
                if continue_from and i > start_idx:
                    new_continue_from = None

                try:
                    scrape_branch(
                        f"{site_url}{href}",
                        new_path,
                        new_continue_from,
                        state_name,
                        state_abb,
                        jsonl_fp,
                        site_url,
                        internal_class,
                        lock,
                        visited_urls,
                        pbar,
                        dept_pbar,
                        max_retries,
                        scraper,
                    )
                except Exception as e:
                    print(f"ERROR: Failed to process {site_url}{href}: {e}")
                    # Log the failed URL, lex_path, and error for recovery
                    try:
                        with lock:
                            failed_file = f"failed_{state_abb}.txt"
                            with open(failed_file, "a") as f:
                                # Save URL and lex_path in JSON format for accurate recovery
                                fail_record = {
                                    "url": f"{site_url}{href}",
                                    "lex_path": new_path,
                                    "error": str(e)
                                }
                                f.write(json.dumps(fail_record) + "\n")
                    except Exception as log_error:
                        print(f"ERROR: Could not log failed URL: {log_error}")
        else:  # This is a leaf node
            # Skip the exact leaf node we are resuming from
            if continue_from and path == continue_from:
                return

            try:
                process_regulation_leaf(
                    state_name, state_abb, url, jsonl_fp, lex_path=path, lock=lock, pbar=pbar, max_retries=max_retries, scraper=scraper
                )
            except Exception as e:
                # any other error other than status code, e.g. html element doesn't exist
                print(f"ERROR: Failed to process leaf {url}: {e}")
                # Log the failed URL and error
                try:
                    with lock:
                        failed_file = f"failed_{state_abb}.txt"
                        with open(failed_file, "a") as f:
                            f.write(f"{url} | Error: {e}\n")
                except Exception as log_error:
                    print(f"ERROR: Could not log failed URL: {log_error}")
    else:
        status_code = response.status_code if response else "No response"
        print(f"Failed to retrieve content for {url}, Status Code: {status_code}")
        try:
            with lock:
                failed_file = f"failed_{state_abb}.txt"
                with open(failed_file, "a") as f:
                    f.write(f"{url} | Status Code: {status_code}\n")
        except Exception as log_error:
            print(f"ERROR: Could not log failed URL: {log_error}")


def worker(
    work_queue: queue.Queue,
    state_name: str,
    state_abb: str,
    jsonl_fp: TextIOWrapper,
    site_url: str,
    internal_class: str,
    lock: threading.Lock,
    visited_urls: set,
    pbar: Optional[tqdm] = None,
    dept_pbar: Optional[tqdm] = None,
    max_retries: int = 3,
    scraper=None,
):
    """
    Worker thread function to process tasks from the queue.
    """
    while True:
        try:
            href, path, continue_from, dept_name = work_queue.get_nowait()
        except queue.Empty:
            break

        # Update department progress bar description
        if dept_pbar:
            dept_pbar.set_description(f"Department: {dept_name}")

        scrape_branch(
            url=f"{site_url}{href}",
            path=path,
            continue_from=continue_from,
            state_name=state_name,
            state_abb=state_abb,
            jsonl_fp=jsonl_fp,
            site_url=site_url,
            internal_class=internal_class,
            lock=lock,
            visited_urls=visited_urls,
            pbar=pbar,
            dept_pbar=dept_pbar,
            max_retries=max_retries,
            scraper=scraper,
        )

        # Update department progress bar when department is complete
        if dept_pbar:
            with lock:
                dept_pbar.update(1)

        work_queue.task_done()


def collect_regulations_for_state(
    state_abb: str,
    resume: bool = False,
    num_threads: int = 1,
    max_retries: int = 3,
) -> None:
    """
    Collect all regulations for the given state in parallel.

    Args:
        state_abb (str): State abbreviation (e.g., 'MT')
        resume (bool): Whether to resume from last scraped position
        num_threads (int): Number of worker threads
        max_retries (int): Maximum retry attempts for failed requests
    """
    if state_abb not in JUR_URL_MAP:
        print(f"ERROR: State '{state_abb}' not found in JUR_URL_MAP")
        return

    state_name = JUR_URL_MAP[state_abb]
    state_init_url = f"{REGULATIONS_BASE_URL}/states/{state_name}/"
    save_dir = "regs"
    site_base_url = REGULATIONS_BASE_URL
    internal_class = "codes-listing"

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    continue_from = None
    mode = "w"
    if resume:
        continue_from = get_last_lex_path(state_abb)
        if continue_from is not None:
            mode = "a"
            print(f"Resuming from lex_path: {continue_from}")

    print(f"\nStarting scraper for {state_abb} ({state_name})")
    print(f"Base URL: {state_init_url}")
    print(f"Using {num_threads} threads")
    print(f"Max retries: {max_retries}")

    # Create a shared scraper instance
    if USE_CLOUDSCRAPER:
        print(f"Using cloudscraper to bypass Cloudflare protection\n")
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'darwin',
                'desktop': True
            }
        )
    else:
        print(f"WARNING: Using basic requests (may not work with Cloudflare)\n")
        scraper = None

    with open(f"{save_dir}/{state_abb}.jsonl", mode) as f:
        response = fetch_with_retry(state_init_url, max_retries=max_retries, scraper=scraper)
        if not response or response.status_code != 200:
            print(f"Failed to get initial page for {state_abb}")
            return

        soup = BeautifulSoup(response.content, "html.parser")
        internal_links_element = soup.find(class_=internal_class)
        if not internal_links_element:
            print(f"No departments found for {state_abb}")
            return

        links = extract_links_from_content(internal_links_element)

        # Filter out RESERVED and REPEALED departments
        original_count = len(links)
        links = [link for link in links if not is_reserved_or_repealed(link["text"])]
        filtered_count = original_count - len(links)

        print(f"Found {len(links)} departments to scrape for {state_abb}")
        if filtered_count > 0:
            print(f"Skipped {filtered_count} RESERVED/REPEALED departments")
        print()

        work_queue = queue.Queue()
        file_lock = threading.Lock()
        visited_urls = set()  # Track visited URLs to prevent circular references

        start_branch_idx = 0
        if continue_from:
            start_branch_idx = continue_from[0]

        for i, link in enumerate(links):
            if i < start_branch_idx:
                continue

            branch_continue_from = None
            if i == start_branch_idx and continue_from:
                branch_continue_from = continue_from

            work_queue.put((link["href"], [i], branch_continue_from, link["text"]))

        threads = []

        # Create two progress bars: pages and departments
        pbar_pages = tqdm(desc=f"Scraping pages", unit=" pages", position=0)
        pbar_depts = tqdm(total=len(links), desc=f"Departments", unit=" dept", position=1)

        for _ in range(num_threads):
            thread = threading.Thread(
                target=worker,
                args=(
                    work_queue,
                    state_name,
                    state_abb,
                    f,
                    site_base_url,
                    internal_class,
                    file_lock,
                    visited_urls,
                    pbar_pages,
                    pbar_depts,
                    max_retries,
                    scraper,
                ),
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        pbar_pages.close()
        pbar_depts.close()

        # Summary
        print(f"\nScraping completed for {state_abb}!")
        print(f"Output saved to: {save_dir}/{state_abb}.jsonl")
        failed_file = f"failed_{state_abb}.txt"
        if os.path.exists(failed_file):
            print(f"Check {failed_file} for any URLs that couldn't be processed")
        else:
            print(f"No failed URLs!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="regscraper.py",
        description="Scrape regulations from Justia regulations website."
    )
    parser.add_argument(
        "state",
        type=str,
        help="The state abbreviation to scrape (e.g., MT for Montana).",
        choices=JUR_URL_MAP.keys()
    )
    parser.add_argument(
        "-c",
        "--resume",
        action="store_true",
        help="Resume an interrupted scrape instead of starting over.",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=1,
        help="The number of threads to use (default: 2).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum number of retry attempts for failed requests (default: 3).",
    )
    args_ = parser.parse_args()

    collect_regulations_for_state(
        args_.state,
        resume=args_.resume,
        num_threads=args_.threads,
        max_retries=args_.max_retries,
    )
