# Justia Regulations Scraper

Scrapes state administrative regulations from regulations.justia.com for all 50 US states plus territories.

## Features

- **Complete scraping**: Recursively scrapes all regulation pages for any state
- **Resume capability**: Interruption recovery with automatic resume from last scraped page
- **Circular reference protection**: Prevents infinite loops from malformed navigation
- **Content validation**: Built-in validator to verify completeness and accuracy
- **Multi-threaded**: Parallel scraping with configurable thread count
- **Clean output**: Removes promotional content, footers, and junk from regulation text
- **Progress tracking**: Real-time progress bars for pages and departments
- **Error logging**: Failed URLs logged to `failed_{STATE}.txt` for recovery

## Requirements

```bash
pip install cloudscraper beautifulsoup4 tenacity tqdm
```

## Usage

### Scrape a state

```bash
python3 regscraper.py MT
```

Supports all 50 states plus DC and territories (AL, AK, AZ, AR, CA, CO, CT, DE, FL, GA, HI, ID, IL, IN, IA, KS, KY, LA, ME, MD, MA, MI, MN, MS, MO, MT, NE, NV, NH, NJ, NM, NY, NC, ND, OH, OK, OR, PA, RI, SC, SD, TN, TX, UT, VT, VA, WA, WV, WI, WY, DC, AS, GU, MP, PR, VI).

### Options

```bash
# Resume interrupted scrape
python3 regscraper.py MT --resume

# Use multiple threads (faster)
python3 regscraper.py MT --threads 4

# Increase retry attempts for unreliable connections
python3 regscraper.py MT --max-retries 5
```

### Validate scraped data

```bash
python3 validate_regs.py MT
```

Validates:
1. **Completeness**: All expected records present (compares against live website)
2. **Order**: Records in correct lexicographical path order
3. **Content**: Spot-checks random records to verify content matches live pages

## Output Format

Each regulation is saved as a JSON line in `regs/{STATE}.jsonl`:

```json
{
  "url": "https://regulations.justia.com/states/montana/department-2/chapter-2-1/subchapter-2-1-1/rule-2-1-101/",
  "state": "MT",
  "path": "Administrative Rules of Montana›Department 2›Chapter 2.1›Subchapter 2.1.1›Rule 2.1.101",
  "title": "Administrative Rules of Montana › Department 2 › Chapter 2.1 › Subchapter 2.1.1 › Rule 2.1.101",
  "univ_cite": true,
  "citation": "Mont. Admin. R. 2.1.101",
  "content": "Current through December 31, 2024\n\n2.1.101 DEFINITIONS\n\n(1) The following definitions apply...",
  "lex_path": [1, 0, 0, 0]
}
```

## Key Fields

- `url`: Full URL to the regulation page
- `state`: State abbreviation (e.g., "MT", "CA", "NY")
- `path`: Hierarchical path with › separators
- `title`: Regulation title
- `univ_cite`: Whether page has universal citation
- `citation`: Official citation (e.g., "Mont. Admin. R. 2.1.101")
- `content`: Full regulation text (cleaned of promotional content)
- `lex_path`: Navigation tree position as array of indices (for ordering)

## How It Works

### Scraper

1. **Fetches state regulations page** (e.g., `/states/montana/`)
2. **Extracts all top-level sections** (departments, titles, agencies)
3. **Recursively navigates** through chapters, subchapters, rules
4. **Detects leaf nodes** (actual regulation pages) vs branch nodes (navigation pages)
5. **Extracts clean content**:
   - Finds `main-content` div
   - Collects sibling `content-indent` divs (Justia splits content across multiple divs)
   - Removes footers, disclaimers, newsletter signups
   - Cleans whitespace while preserving structure
6. **Tracks visited URLs** to prevent circular reference loops
7. **Logs failures** for later recovery

### Validator

1. **Fetches navigation tree** from live website
2. **Compares expected vs actual records** section-by-section
3. **Validates order** by checking lex_path sequence
4. **Spot-checks content** by:
   - Fetching random regulation pages
   - Collecting main-content + sibling divs (same as scraper)
   - Normalizing both (remove whitespace, lowercase)
   - Sampling 20 chunks from different positions
   - Requiring 60% chunk match (lenient for HTML variations)

## Common Issues

### Cloudflare blocking

Install cloudscraper:
```bash
pip install cloudscraper
```

The scraper automatically uses cloudscraper when available.

### Failed URLs

Check `failed_{STATE}.txt` for any URLs that couldn't be scraped. Common causes:
- Network timeouts (increase `--max-retries`)
- Rate limiting (reduce `--threads`)
- Malformed pages (manual inspection needed)

### Validation failures

If validation reports missing records:
1. Check if pages were genuinely missed
2. Re-run scraper with `--resume` to fill gaps
3. Some pages may be navigation loops (circular references) - these are intentionally skipped

## Performance

- **Single-threaded**: ~2-5 pages/second (safe, recommended)
- **Multi-threaded (4 threads)**: ~8-15 pages/second (faster but may hit rate limits)
- **Average state**: 1,000-5,000 regulations, takes 10-40 minutes
- **Large states** (CA, TX, NY): 10,000+ regulations, takes 2-4 hours

