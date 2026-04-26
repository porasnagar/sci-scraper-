"""
Supreme Court of India — Judgement Scraper  v4.1
=================================================
Scrapes all publicly available judgements from the SCI portal:
  https://www.sci.gov.in/judgements-judgement-date/

Architecture:
  - Playwright (headed Chromium) drives the date-range search form
  - Tesseract OCR + multi-pass image preprocessing solves the math CAPTCHA
  - Intercepted AJAX response (admin-ajax.php JSON) parsed for structured data
  - 30-day windows -> ~900 requests for full 1950-present history
  - Incremental JSONL output for crash-safe long runs

Usage:
  python sci_scraper_v4.py --mode demo               # last 30 days
  python sci_scraper_v4.py --mode full               # 1950 to today
  python sci_scraper_v4.py --start 2020 --end 2025   # custom year range
  python sci_scraper_v4.py --mode demo --debug       # verbose logging
  python sci_scraper_v4.py --mode demo --headless    # headless (may be blocked by Akamai)
"""

import argparse
import io
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sci")

OUTPUT_DIR = Path("sci_judgements")
META_FILE  = OUTPUT_DIR / "metadata.jsonl"
PAGE_URL   = "https://www.sci.gov.in/judgements-judgement-date/"

TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


# =============================================================================
#  CAPTCHA SOLVER
# =============================================================================

def _eval_math(text):
    """
    Parse OCR output from a math captcha like '8 - 2', '9 - 5', '5 + 4'.
    Handles common Tesseract misreads:
      '-' -> '.', '~', ' '
      '9' -> 'g9' (cursive top of 9 looks like g)
      digit2 sometimes missing/merged
    """
    if not text:
        return None

    s = text.strip()

    # Step 1: Replace ~ with - (~ is common misread of -)
    s = s.replace("~", "-")

    # Step 2: Remove letter that immediately precedes the SAME digit it misrepresents
    # e.g. 'g9' -> '9' (g is misread top of 9), 'S5' -> '5'
    # Pattern: letter immediately before a digit, where letter looks like that digit
    misread_pairs = [("g", "9"), ("G", "9"), ("q", "9"), ("Q", "9"),
                     ("s", "5"), ("S", "5"),
                     ("o", "0"), ("O", "0"),
                     ("l", "1"), ("I", "1"), ("L", "1"),
                     ("b", "8"), ("B", "8")]
    for letter, digit in misread_pairs:
        # Remove letter when it directly precedes the same digit
        # 'g9' -> '9', but not in middle of a word
        s = re.sub(r"(?<![A-Za-z0-9])" + re.escape(letter) + re.escape(digit), digit, s)
        # Also: letter alone where no digit follows (at start before operator)
        # e.g. 'g~5' -> '9-5'
        s = re.sub(r"(?<![A-Za-z0-9])" + re.escape(letter) + r"(?=[\s\-\+\.\~])", digit, s)

    # Direct match: digit(s) OP digit(s)
    m = re.search(r"(\d+)\s*([+\-])\s*(\d+)", s)
    if m:
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        return (a + b if op == "+" else a - b, True)

    # '-' often read as '.', '~', or space — find two digit groups
    nums = re.findall(r"\d+", s)
    if len(nums) >= 2:
        a, b = int(nums[0]), int(nums[1])
        try:
            idx_a_end = s.index(nums[0]) + len(nums[0])
            idx_b_start = s.index(nums[1], idx_a_end)
            between = s[idx_a_end:idx_b_start].lower()
        except ValueError:
            between = ""
        log.debug("  between=%r, a=%d, b=%d", between, a, b)

        # Explicit addition misreads
        if "+" in between or "t" in between or "f" in between or "&" in between or "*" in between:
            return (a + b, True)
        
        # Explicit subtraction misreads
        if "-" in between or "." in between or "," in between or "~" in between:
            return (a - b, True)
            
        # Ambiguous (just space or empty): no clear operator
        # prefer subtraction if result in expected range, but mark as low confidence
        if 0 <= a - b <= 20:
            return (a - b, False)
        return (a + b, False)
        
    # If it reads exactly 3 digits (e.g., '944', '871'), the middle digit is probably a misread operator
    if len(nums) == 1 and len(nums[0]) == 3:
        a = int(nums[0][0])
        b = int(nums[0][2])
        mid = nums[0][1]
        
        # '4' or '1' is often a misread of '+'
        if mid in ("4", "1", "7"):
            return (a + b, False)
        
        # middle char was likely + or -
        if 0 <= a - b <= 20:
            return (a - b, False)
        return (a + b, False)

    return None


def ocr_image(work_img):
    """Run Tesseract on a PIL image, return raw string."""
    try:
        import pytesseract
        if os.path.exists(TESSERACT_PATH):
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
        # No whitelist -- let tesseract read freely, we'll parse it
        for psm in [7, 6, 13, 8]:
            raw = pytesseract.image_to_string(
                work_img, config="--psm %d" % psm
            ).strip()
            if raw:
                return raw
    except Exception as e:
        log.debug("  tesseract error: %s", e)
    return ""


def solve_captcha_ocr(image_bytes):
    """
    Solve the SCI math captcha.
    The image shows a math expression like '8 - 2' in gray-on-white.
    Returns the string answer (e.g. '6'), or '' on failure.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps, ImageEnhance

        img = Image.open(io.BytesIO(image_bytes))
        debug_path = OUTPUT_DIR / "captcha_debug.png"
        img.save(debug_path)
        log.debug("  Captcha saved: %s", debug_path)

        base = img.convert("L")
        variants = []

        # v1: autocontrast (boosts gray->dark) + 4x upscale + sharpen
        w = ImageOps.autocontrast(base.copy(), cutoff=2)
        w = w.resize((w.width * 4, w.height * 4), Image.LANCZOS)
        w = w.filter(ImageFilter.SHARPEN)
        variants.append(("ac_4x", w))

        # v2: strong contrast + binarize at 160
        w = ImageEnhance.Contrast(base.copy()).enhance(5.0)
        w = w.resize((w.width * 4, w.height * 4), Image.LANCZOS)
        w = w.point(lambda x: 0 if x < 160 else 255)
        variants.append(("c5_b160", w))

        # v3: invert + autocontrast + binarize
        w = ImageOps.invert(base.copy())
        w = ImageOps.autocontrast(w, cutoff=2)
        w = w.resize((w.width * 4, w.height * 4), Image.LANCZOS)
        w = w.point(lambda x: 0 if x < 128 else 255)
        variants.append(("inv_b128", w))

        # v4: simple upscale + low threshold (220) for very light gray
        w = base.copy()
        w = w.resize((w.width * 4, w.height * 4), Image.LANCZOS)
        w = w.point(lambda x: 0 if x < 220 else 255)
        variants.append(("b220_4x", w))

        all_raws = []
        for name, work in variants:
            raw = ocr_image(work)
            if raw:
                all_raws.append(raw)
                log.debug("  OCR [%s]: %r", name, raw)

        log.debug("  All raws: %s", all_raws)

        # Try to evaluate each as math
        answers = []
        for raw in all_raws:
            res = _eval_math(raw)
            if res is not None:
                val, conf = res
                if 0 <= val <= 25:
                    if conf:
                        log.debug("  Answer (confident): %s (from %r)", val, raw)
                        return str(val)
                    answers.append((val, raw))
        
        # If no explicit operator was found, fallback to the first valid ambiguous guess
        if answers:
            val, raw = answers[0]
            log.debug("  Answer (ambiguous fallback): %s (from %r)", val, raw)
            return str(val)

        # Digit-only fallback
        for raw in all_raws:
            d = re.sub(r"\D", "", raw)
            if d:
                log.debug("  Digit fallback: %r", d)
                return d

    except ImportError as e:
        log.error("  pytesseract/Pillow missing: %s", e)
    except Exception as e:
        log.warning("  OCR error: %s", e)

    return ""


# =============================================================================
#  JS HELPER: set input value and fire events
# =============================================================================

SET_INPUT_JS = """
(args) => {
    const [selector, value] = args;
    const el = document.querySelector(selector);
    if (!el) return null;
    const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
    return el.value;
}
"""


# =============================================================================
#  SINGLE RANGE FETCH
# =============================================================================

def fetch_one_range(page, from_date, to_date, max_captcha_tries=15):
    """
    Load page, fill dates, solve captcha (with retry), submit, parse results.
    """
    captured = {}

    def on_response(resp):
        try:
            if "admin-ajax.php" in resp.url:
                body = resp.text()
                if '"success"' in body:
                    captured["json"] = body
                    captured["url"]  = resp.url
        except Exception:
            pass

    page.on("response", on_response)

    fd = from_date.strftime("%d-%m-%Y")
    td = to_date.strftime("%d-%m-%Y")

    def fill_dates():
        """Fill date fields using JavaScript (works in both headed and headless)."""
        # Poll for #from_date to appear (WordPress datepicker plugin may be slow)
        for _ in range(20):  # up to 10s in 0.5s steps
            exists = page.evaluate("() => !!document.getElementById('from_date')")
            if exists:
                break
            page.wait_for_timeout(500)

        # Pure JS set — doesn't depend on element visibility
        result = page.evaluate("""(args) => {
            var fd = args[0], td = args[1];
            var f = document.getElementById('from_date');
            var t = document.getElementById('to_date');
            if (f) {
                f.value = fd;
                f.dispatchEvent(new Event('input', {bubbles:true}));
                f.dispatchEvent(new Event('change', {bubbles:true}));
            }
            if (t) {
                t.value = td;
                t.dispatchEvent(new Event('input', {bubbles:true}));
                t.dispatchEvent(new Event('change', {bubbles:true}));
            }
            return {from: f ? f.value : null, to: t ? t.value : null};
        }""", [fd, td])
        log.debug("  JS fill result: %s", result)

    def get_captcha_src():
        """Get captcha image src via JS (doesn't require selector visibility)."""
        try:
            src = page.evaluate("""() => {
                var img = document.getElementById('siwp_captcha_image_0');
                return img ? img.src : null;
            }""")
            return src or ""
        except Exception:
            return ""



    try:
        log.debug("  Loading page...")
        page.goto(PAGE_URL, timeout=90_000)
        page.wait_for_load_state("networkidle", timeout=30_000)
        page.wait_for_timeout(4000)  # extra wait for WordPress plugins to init

        fill_dates()

        # Captcha solve + submit loop (refresh captcha on wrong answer)
        for attempt in range(max_captcha_tries):
            captured.clear()

            # If not first attempt, reload page (fresh captcha)
            if attempt > 0:
                log.debug("  Retry %d: reloading page for fresh captcha", attempt + 1)
                page.goto(PAGE_URL, timeout=90_000)
                page.wait_for_load_state("networkidle", timeout=30_000)
                page.wait_for_timeout(2500)
                fill_dates()

            # Get captcha image src via JS (no visibility check needed)
            page.wait_for_timeout(2000)  # let captcha image load
            captcha_src = get_captcha_src()
            answer = ""

            if captcha_src:
                if not captcha_src.startswith("http"):
                    captcha_src = "https://www.sci.gov.in" + captcha_src
                log.debug("  Captcha URL: %s", captcha_src)
                try:
                    img_resp = page.request.get(captcha_src, timeout=15_000)
                    if img_resp.ok:
                        answer = solve_captcha_ocr(img_resp.body())
                    else:
                        log.warning("  Captcha fetch failed: %s", img_resp.status)
                except Exception as e:
                    log.warning("  Captcha download error: %s", e)

            if answer:
                # Fill captcha via JS (bypasses visibility checks)
                page.evaluate("(v) => { var el = document.getElementById('siwp_captcha_value_0'); if(el) el.value = v; }", answer)
                log.debug("  Filled captcha: %r (attempt %d)", answer, attempt + 1)
            else:
                log.warning("  Could not solve captcha on attempt %d", attempt + 1)
                continue

            # Submit via JS click (more reliable in headless)
            page.evaluate("() => { var btn = document.querySelector('input[name=submit][value=Search]'); if(btn) btn.click(); }")
            try:
                page.wait_for_selector("#cnrResults:not(.hide)", timeout=20_000)
            except Exception:
                pass
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(500)

            # Check if captcha was rejected
            if "json" in captured:
                data = {}
                try:
                    data = json.loads(captured["json"])
                except Exception:
                    pass
                if data.get("success") == False:
                    # Check if it's a captcha error
                    raw_msg = ""
                    try:
                        raw_msg = str(data.get("data", ""))
                    except Exception:
                        pass
                    if "captcha" in raw_msg.lower():
                        log.debug("  Captcha rejected, retrying...")
                        continue
                # Either success or other error
                break
            else:
                break  # No AJAX response captured -- parse page content

    except Exception as e:
        log.warning("  Fetch error [%s->%s]: %s", from_date, to_date, e)
    finally:
        page.remove_listener("response", on_response)

    # Parse results
    if "json" in captured:
        records = parse_ajax_json(captured["json"], from_date, to_date)
        if records is not None:
            return records

    return parse_html_table(page.content(), from_date, to_date)


# =============================================================================
#  PARSE AJAX JSON
# =============================================================================

def parse_ajax_json(body, from_date, to_date):
    try:
        data = json.loads(body)
    except Exception:
        return None

    if not data.get("success"):
        msg = ""
        try:
            raw_data = data.get("data", "")
            if isinstance(raw_data, str):
                msg = json.loads(raw_data).get("message", "")
            elif isinstance(raw_data, dict):
                msg = raw_data.get("message", "")
        except Exception:
            pass
        log.debug("  API success=false: %r", msg)
        return []

    inner = data.get("data", {})
    if isinstance(inner, dict):
        html = inner.get("resultsHtml") or inner.get("html") or ""
    else:
        html = str(inner)

    return parse_html_table(html, from_date, to_date)


# =============================================================================
#  HTML TABLE PARSER
# =============================================================================

def parse_html_table(html, from_date, to_date):
    records = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        tables = soup.find_all("table")
        if not tables:
            nf = soup.find(class_="notfound")
            if nf:
                log.debug("  Server msg: %r", nf.get_text(strip=True))
            return []

        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                texts = [c.get_text(" ", strip=True) for c in cells]
                if all(len(t) < 3 for t in texts):
                    continue

                # Extract PDF / document viewer link
                pdf_url = ""
                all_links = []
                for a in row.find_all("a", href=True):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = "https://www.sci.gov.in" + href
                    all_links.append(href)
                    # Prefer direct PDF or the api.sci.gov.in document handle
                    if ".pdf" in href.lower():
                        pdf_url = href
                        break
                    if "api.sci.gov.in" in href and "/handle/" in href:
                        pdf_url = href  # keep looking for a .pdf but accept this
                if not pdf_url and all_links:
                    pdf_url = all_links[0]

                rec = {
                    "from_date":  str(from_date),
                    "to_date":    str(to_date),
                    "pdf_url":    pdf_url,
                    "filename":   pdf_url.rstrip("/").split("/")[-1].split("?")[0] if pdf_url else "",
                    "raw_cells":  texts,
                }

                for t in texts:
                    if re.match(r"\d{2}[/\-]\d{2}[/\-]\d{4}", t):
                        rec["judgment_date"] = t
                    if re.search(r"(SLP|Civil Appeal|Criminal Appeal|Writ|Petition|Transfer)", t, re.I):
                        rec.setdefault("case_type", t)
                    if re.match(r"\d+[/\-]\d{4}", t) or re.match(r"\d+\s+of\s+\d{4}", t, re.I):
                        rec.setdefault("case_no", t)
                for t in texts:
                    if len(t) > 5:
                        rec.setdefault("title", t)
                        break

                records.append(rec)

    except ImportError:
        for m in re.finditer(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.I):
            url = m.group(1)
            if not url.startswith("http"):
                url = "https://www.sci.gov.in" + url
            records.append({
                "from_date": str(from_date),
                "to_date":   str(to_date),
                "pdf_url":   url,
                "filename":  url.split("/")[-1].split("?")[0],
            })
    except Exception as e:
        log.warning("  Parse error: %s", e)

    return records


# =============================================================================
#  PDF DOWNLOADER  (optional, --download-pdfs flag)
# =============================================================================

def download_pdf(record, page, pdf_dir):
    """
    Download the PDF for a single record.
    Saves to pdf_dir/<case_no>_<date>.pdf
    Returns the local path string, or '' on failure.
    """
    url = record.get("pdf_url", "")
    if not url or url in ("https://www.sci.gov.in", "https://api.sci.gov.in/"):
        return ""

    # Build a safe filename
    case_no = re.sub(r"[^\w\-]", "_", record.get("case_no", "unknown"))
    jdate   = re.sub(r"[^\d\-]", "", record.get("judgment_date", "")[:10])
    fname   = "%s_%s.pdf" % (case_no, jdate) if jdate else "%s.pdf" % case_no
    dest    = pdf_dir / fname

    if dest.exists():
        return str(dest)  # already downloaded

    try:
        # Use Playwright's request context to inherit session cookies
        resp = page.request.get(url, timeout=30_000)
        if resp.ok:
            content_type = resp.headers.get("content-type", "")
            if "pdf" in content_type or url.endswith(".pdf"):
                dest.write_bytes(resp.body())
                log.debug("  PDF saved: %s", dest.name)
                return str(dest)
            else:
                # Try to find a direct .pdf link in the response (document viewer redirect)
                body_text = resp.text()
                m = re.search(r'(https?://[^"\s]+\.pdf)', body_text)
                if m:
                    pdf_resp = page.request.get(m.group(1), timeout=30_000)
                    if pdf_resp.ok:
                        dest.write_bytes(pdf_resp.body())
                        log.debug("  PDF saved (resolved): %s", dest.name)
                        return str(dest)
        else:
            log.debug("  PDF fetch failed: HTTP %s for %s", resp.status, url)
    except Exception as e:
        log.debug("  PDF download error: %s", e)

    return ""


# =============================================================================
#  DATE WINDOW GENERATOR
# =============================================================================

def date_windows(start, end, window_days=30):
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=window_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


# =============================================================================
#  ORCHESTRATOR
# =============================================================================

def run_scraper(start_year=1950, end_year=None, mode="demo", headed=True, download_pdfs=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Run: pip install playwright && playwright install chromium")
        return [], 0

    if end_year is None:
        end_year = date.today().year

    OUTPUT_DIR.mkdir(exist_ok=True)

    if mode == "demo":
        start_dt = date.today() - timedelta(days=30)
        end_dt   = date.today()
    else:
        start_dt = date(start_year, 1, 1)
        end_dt   = date(end_year, 12, 31)

    end_dt = min(end_dt, date.today())
    windows = list(date_windows(start_dt, end_dt, window_days=30))
    log.info("Date range: %s to %s  |  %d requests (30-day windows)",
             start_dt, end_dt, len(windows))

    all_records = []
    t0 = time.time()
    failed = 0
    total = len(windows)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        # Apply stealth to bypass bot detection (Akamai blocks plain headless)
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(ctx.new_page())  # warm up
        except ImportError:
            pass

        page = ctx.new_page()
        # Re-apply stealth to the actual page
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            log.debug("playwright-stealth not available; using headed mode is recommended")

        pdf_dir = OUTPUT_DIR / "pdfs"
        if download_pdfs:
            pdf_dir.mkdir(exist_ok=True)
            log.info("PDF download mode ON  ->  %s", pdf_dir)

        for i, (fd, td) in enumerate(windows, 1):
            log.info("  [%4d/%d]  %s -> %s", i, total, fd, td)
            try:
                records = fetch_one_range(page, fd, td)
            except Exception as e:
                log.warning("  Unexpected error: %s", e)
                records = []

            # Optional PDF download
            if download_pdfs and records:
                log.info("           Downloading %d PDFs...", len(records))
                for r in records:
                    local = download_pdf(r, page, pdf_dir)
                    if local:
                        r["local_pdf"] = local

            all_records.extend(records)

            if records:
                log.info("           -> %d judgements found", len(records))
            else:
                log.info("           -> 0 (no judgements or captcha failed)")
                failed += 1

            if records:
                with open(META_FILE, "a", encoding="utf-8") as f:
                    for r in records:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")

            time.sleep(1.5)

        browser.close()

    elapsed = time.time() - t0
    log.info("\nDone: %d records  |  %d/%d windows had 0 results",
             len(all_records), failed, total)
    log.info("Time: %.1f min", elapsed / 60)
    log.info("Saved -> %s", META_FILE)
    return all_records, elapsed


# =============================================================================
#  CLI
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="SCI Judgement Scraper v4.1")
    ap.add_argument("--mode",   choices=["demo", "full"], default="demo")
    ap.add_argument("--start",  type=int, default=1950)
    ap.add_argument("--end",    type=int, default=date.today().year)
    ap.add_argument("--headless", action="store_true",
                    help="Run headless (WARNING: site may block headless; headed is recommended)")
    ap.add_argument("--download-pdfs", action="store_true",
                    help="Download actual PDF files to sci_judgements/pdfs/ (slow, large disk usage)")
    ap.add_argument("--debug",  action="store_true", help="Verbose logging")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger("sci").setLevel(logging.DEBUG)

    if os.path.exists(TESSERACT_PATH):
        print("[OK] Tesseract:", TESSERACT_PATH)
    else:
        print("[!!] Tesseract NOT found at:", TESSERACT_PATH)

    if args.mode == "full":
        total_days = (date(args.end, 12, 31) - date(args.start, 1, 1)).days
        n = total_days // 30 + 1
        est_hrs = n * 7 / 3600
        if args.download_pdfs:
            est_hrs *= 4  # PDF downloads add significant time
        print("\nEstimate %d-%d: %d windows, ~%.1f hrs%s" % (
            args.start, args.end, n, est_hrs,
            " (with PDF download)" if args.download_pdfs else ""
        ))
    print()

    records, elapsed = run_scraper(
        start_year=args.start,
        end_year=args.end,
        mode=args.mode,
        headed=not args.headless,
        download_pdfs=args.download_pdfs,
    )

    if records:
        print("\nSample (first 5):")
        for r in records[:5]:
            print("  date=%-12s  title=%-40s" % (
                r.get("judgment_date", r.get("from_date", "?")),
                r.get("title", "")[:40],
            ))
        print("\nAll results saved to:", META_FILE)
    else:
        print("\nNo records fetched (for the demo period, this may mean no judgements were issued).")
        print("  Check: sci_judgements/captcha_debug.png (to see if OCR saw the captcha)")
        print("  Run:   python sci_scraper_v4.py --mode demo --debug")



if __name__ == "__main__":
    main()
