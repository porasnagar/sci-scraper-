# Supreme Court of India — Judgement Scraper

A robust, automated scraper that collects all publicly available judgements from the [Supreme Court of India's judgement portal](https://www.sci.gov.in/judgements-judgement-date/) — from the earliest available records (~1950) through the present day.

---

## The Problem

The SCI portal has **no public API or bulk download**. Every search:
- Requires a valid date range (max 30 days per query)
- Is protected by a **math CAPTCHA** image (e.g. `8 - 2 = ?`)
- Returns results as dynamic HTML injected by a WordPress AJAX call

Scraping the full archive naïvely (day-by-day) would require **~27,000 page loads**. Doing it wrong is slow, fragile, and gets blocked.

---

## Demo and Result

[Demo and Result](https://www.sci.gov.in/judgements-judgement-date/](https://htmlpreview.github.io/?https://raw.githubusercontent.com/porasnagar/sci-scraper-/main/results.html))

## Our Approach & Techniques

### 1. Reverse-Engineering the API

Using browser DevTools network inspection, we identified the hidden AJAX endpoint the site uses:

```
POST https://www.sci.gov.in/wp-admin/admin-ajax.php
    action=get_judgements_judgement_date
    from_date=DD-MM-YYYY
    to_date=DD-MM-YYYY
    siwp_captcha_value=<answer>
    language=en
```

Instead of scraping rendered HTML, we **intercept the raw JSON response** directly — far more reliable than DOM parsing.

### 2. 30-Day Window Batching

The API accepts date ranges up to 30 days. So instead of ~27,000 day-by-day requests, we use:
- **~900 requests** (one per 30-day window from 1950 → today)
- Estimated total time: **~3–5 hours** at ~12 seconds/window (network + CAPTCHA solve)

| Approach | Requests | Est. Time |
|---|---|---|
| Day-by-day | ~27,375 | ~76 hrs |
| **Month-by-month (our approach)** | **~912** | **~3–5 hrs** |

### 3. Math CAPTCHA Solving with OCR

The portal uses a **Securimage-WP** math CAPTCHA — an image rendering a simple arithmetic expression like `8 - 2` or `5 + 4` in a stylized, low-contrast gray-on-white font.

We solve it with a multi-stage pipeline:

```
Captcha Image
    │
    ├─ v1: Autocontrast + 4× upscale + Sharpen
    ├─ v2: 5× Contrast boost + Binarize (threshold 160)
    ├─ v3: Invert + Autocontrast + Binarize (threshold 128)
    └─ v4: 4× upscale + Binarize (threshold 220, catches very light gray)
          │
          └─► Tesseract OCR (PSM modes 7, 6, 13, 8)
                  │
                  └─► Smart math parser (_eval_math):
                        - Operator confidence scoring (+/- vs ambiguous)
                        - Handles misreads: '.' → '-', 'g9' → '9', '~' → '-'
                        - Returns confident answer or retries with fresh CAPTCHA
```

Up to **15 CAPTCHA retries per window** ensures eventual success without skipping data.

### 4. Playwright Browser Automation

- **Headed Chromium** (default): bypasses Akamai bot-detection that blocks headless mode
- JS-based form filling (`getElementById().value = ...`) to set hidden datepicker inputs
- Response interception (`page.on("response", ...)`) to capture AJAX JSON directly
- No fragile CSS/XPath selectors — works even if the page layout changes

### 5. Crash-Safe Incremental Output

Every successful window is **immediately appended** to `sci_judgements/metadata.jsonl`. If the script crashes or is interrupted, re-running it resumes from where it left off (deduplicated by date range).

---

## Requirements

- Python 3.11+
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) installed at `C:\Program Files\Tesseract-OCR\tesseract.exe` (Windows) or accessible via PATH
- Conda or virtualenv recommended

### Install

```bash
# Create environment
conda create -n sci-scraper python=3.11 -y
conda activate sci-scraper

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium
```

---

## Usage

```bash
# Demo: scrape last 30 days (for quick verification)
python sci_scraper_v4.py --mode demo

# Full historical scrape: 1950 → today
python sci_scraper_v4.py --mode full

# Custom year range
python sci_scraper_v4.py --start 2010 --end 2020

# Verbose debug logging (shows OCR output, CAPTCHA answers, etc.)
python sci_scraper_v4.py --mode demo --debug
```

> **Note:** A browser window will open — this is intentional. The site uses Akamai CDN bot-detection that blocks fully headless browsers.

---

## Demo Output (Last 30 Days)

Running `--mode demo` fetches the last 30 days of judgements (~1–2 minutes):

```
09:56:16  INFO   Date range: 2026-03-27 to 2026-04-26  |  2 requests (30-day windows)
09:56:18  INFO     [   1/2]  2026-03-27 -> 2026-04-25
09:56:46  INFO              -> 100 judgements found
09:56:47  INFO     [   2/2]  2026-04-26 -> 2026-04-26
09:57:04  INFO              -> 0 (no judgements or captcha failed)
09:57:06  INFO
Done: 100 records  |  Time: 0.8 min
Saved -> sci_judgements\metadata.jsonl

Sample (first 5):
  date=15-04-2026   title=STATE OF KERALA VS K.A. ABDUL RASHEED
  date=16-04-2026   title=PRIYANKA SARKARIYA VS THE UNION OF INDIA
  date=09-04-2026   title=CHANNAPPA SINCE DECEASED REP BY HIS LRS. VS PARVATI
  date=13-04-2026   title=ANOSH EKKA VS STATE THROUGH CENTRAL BUREAU OF INVESTIGATION
  date=06-04-2026   title=SAVE MON REGION FEDERATION VS THE STATE OF ARUNACHAL PRADESH
```

---

## Output Format

Results are saved to `sci_judgements/metadata.jsonl` — one JSON object per line:

```json
{
  "from_date": "2026-03-27",
  "to_date": "2026-04-25",
  "judgment_date": "15-04-2026",
  "case_no": "182/2026",
  "title": "STATE OF KERALA VS K.A. ABDUL RASHEED",
  "pdf_url": "https://api.sci.gov.in/...",
  "raw_cells": [
    "1",
    "182/2026",
    "Crl.A. No.-001956-001956 - 2026",
    "STATE OF KERALA VS K.A. ABDUL RASHEED",
    "HON'BLE MR. JUSTICE SANJAY KUMAR ...",
    "15-04-2026(English) 2026 INSC 365(English)"
  ]
}
```

### Converting to CSV

```python
import json, csv

records = [json.loads(l) for l in open("sci_judgements/metadata.jsonl") if l.strip()]
with open("judgements.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["judgment_date", "case_no", "title", "pdf_url"])
    writer.writeheader()
    writer.writerows(records)
```

---

## Full Run Estimate

| Year Range | Windows | Est. Time |
|---|---|---|
| Last 30 days (demo) | 2 | ~1 min |
| 2020 – 2026 | ~74 | ~15 min |
| 2000 – 2026 | ~312 | ~1 hr |
| **1950 – 2026 (full)** | **~912** | **~3–5 hrs** |

The scraper runs at roughly **12–15 seconds per 30-day window** (dominated by page load and CAPTCHA solving). With 15 retry attempts per window and a ~30% OCR success rate per attempt, the effective per-window success rate is >99%.

---

## Project Structure

```
sci_scraper_v4.py          # Main scraper
requirements.txt           # Python dependencies
sci_judgements/
    metadata.jsonl         # Scraped records (JSONL, one per line)
    captcha_debug.png      # Last CAPTCHA image seen (for debugging)
```

---

## Known Limitations

- **Max 100 records per 30-day window**: The SCI API paginates at 100. If a month has >100 judgements, only the first page is fetched. This can be addressed by implementing pagination via the `page` POST parameter.
- **Bot detection**: The site uses Akamai CDN. Headless Chromium is blocked — the scraper runs in headed (visible browser) mode by default.
- **CAPTCHA variability**: Rarely the captcha renders as unreadable noise. The 15-retry logic handles this by getting a fresh image each time.

---

## Tech Stack

| Tool | Purpose |
|---|---|
| `playwright` | Browser automation, form interaction, response interception |
| `pytesseract` + `Pillow` | CAPTCHA OCR with multi-pass image preprocessing |
| `beautifulsoup4` | HTML parsing of AJAX result tables |
| `Tesseract OCR` | OCR engine (external binary) |

---

*Built to fulfil a research task: bulk-download the complete archive of Supreme Court of India judgements for legal data analysis.*
