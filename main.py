#!/usr/bin/env python3
import os
import json
import time
import random
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore


import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, WebDriverException
from threading import Thread
from flask import Flask, jsonify

app = Flask(__name__)

# ─── CONFIG ─────────────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
LOG = os.getenv("LOG", "true").lower() in ("1", "true", "yes")
CHROMEDRIVER = os.getenv("CHROMEDRIVER_PATH", "/usr/local/bin/chromedriver")
# ───────────────────────────────────────────────────────


def log(msg: str):
    if LOG:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


# ─── Firebase init ─────────────────────────────────────
svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
if not svc_json:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON must be set")
cred_dict = json.loads(svc_json)
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()


def load_config():
    doc = db.collection("config").document("settings").get()
    return doc.to_dict() if doc.exists else {}


def load_links():
    return [(doc.id, doc.to_dict()) for doc in db.collection("links").stream()]


def save_link_state(doc_id: str, fields: dict):
    # if we're marking it available now, and no timestamp was provided, add one
    if fields.get("available") is True and "available_since" not in fields:
        fields["available_since"] = time.time()
    # allow callers to explicitly delete available_since by passing firestore.DELETE_FIELD
    db.collection("links").document(doc_id).update(fields)


# ─── Selenium driver ───────────────────────────────────
_driver = None


def init_driver():
    global _driver
    if _driver:
        return _driver

    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    ua = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{random.randint(100,115)}.0.{random.randint(1000,5000)}.100 Safari/537.36"
    )
    opts.add_argument(f"user-agent={ua}")

    try:
        service = Service(CHROMEDRIVER)
        _driver = webdriver.Chrome(service=service, options=opts)
        _driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
            },
        )
        log("Initialized headless ChromeDriver.")
        return _driver
    except WebDriverException as e:
        log(f"WebDriver init failed: {e}")
        raise


# ─── Telegram ───────────────────────────────────────────
def send_telegram(token: str, chat_id: str, text: str):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data=payload, timeout=10)
        resp.raise_for_status()
        log(f"Telegram sent: {text}")
    except Exception as e:
        log(f"Telegram error: {e}")


def set_italy_delivery_once(drv, wait):
    try:
        drv.refresh()
        time.sleep(4)
        log("→ Setting delivery to Italy (00049)…")
        wait.until(
            EC.element_to_be_clickable((By.ID, "nav-global-location-popover-link"))
        ).click()
        time.sleep(5)
        zip_in = wait.until(
            EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput"))
        )
        zip_in.clear()
        time.sleep(2)
        zip_in.send_keys("00049", Keys.ENTER)
        time.sleep(4)
        pop = wait.until(
            EC.presence_of_element_located((By.CLASS_NAME, "a-popover-footer"))
        )
        pop.find_element(By.XPATH, "./*").click()
        time.sleep(4)
        log("→ Delivery set to Italy 00049")
    except Exception:
        log("→ Could not set Italy delivery (already set?)")


# ─── Core Logic ─────────────────────────────────────────
def check_once():
    cfg = load_config()
    token = cfg.get("token")
    chat_id = cfg.get("chat_id")
    cool = cfg.get("cool_time", 300)
    if not token or not chat_id:
        raise RuntimeError("Missing token/chat_id in Firestore config")

    drv = init_driver()
    wait = WebDriverWait(drv, 5)
    
    links = list(load_links())
    if links:
        log(f"→ Found {len(links)} link(s) in Firestore, setting delivery…")
        drv.get("https://www.amazon.it/-/en/ref=nav_logo")
        time.sleep(8)
        set_italy_delivery_once(drv, wait)
    else:
        log("→ No links in Firestore, skipping delivery setup")

    try:
        for doc_id, item in load_links():
            url = item["url"]

            # ─── Cool-down logic for already-available links ─────────────
            if item.get("available"):
                now = time.time()
                since = item.get("available_since")
                # First time we see available=true: record timestamp and skip
                if since is None:
                    log(f"→ {url} marked available; starting cool-down of {cool}s")
                    save_link_state(doc_id, {"available_since": now})
                    continue

                elapsed = now - since
                if elapsed < cool:
                    log(f"→ {url} still in cool-down ({elapsed:.0f}/{cool}s), skipping")
                    continue
                # Cool-down expired: reset and re-check
                log(f"→ Cool-down expired for {url}; re-checking availability")
                save_link_state(
                    doc_id,
                    {"available": False, "available_since": firestore.DELETE_FIELD},
                )
                item["available"] = False
            # ─────────────────────────────────────────────────────────────
            if item.get("available"):
                continue

            log(f"Loading page: {url}")
            try:
                drv.get(url)
                time.sleep(4)

                # ─── Out of stock? ────────────────────────────────
                try:
                    wait.until(EC.presence_of_element_located((By.ID, "outOfStock")))
                    log("→ Still out of stock, skipping")
                    continue
                except:
                    log("→ Not marked out of stock")

                # ─── Dismiss cookies ───────────────────────────────
                try:
                    cookie = wait.until(
                        EC.element_to_be_clickable((By.ID, "sp-cc-rejectall-link"))
                    )
                    cookie.click()
                    log("→ Cookies dismissed")
                except:
                    log("→ No cookie banner to dismiss")

                # ─── Core PDP offer ────────────────────────────────
                try:
                    if _check_core_offer(drv, wait, item):
                        save_link_state(doc_id, {"available": True})
                        msg = (
                            f"✅ {item['name']} is back in stock!\n"
                            f"✅ AMAZON OFFER FOUND!\n{url}\n"
                            f"💰 €{_CORE_PRICE:.2f} (≤ €{item['target_price']:.2f})\n"
                            f"🚚 Ships from: {_CORE_SHIPS}\n"
                            f"🏷️ Sold by: {_CORE_SOLD}"
                        )
                        send_telegram(token, chat_id, msg)
                        log("→ Notifying core-offer match")
                        continue
                    else:
                        log("→ Core PDP offer did not meet criteria")
                except Exception as e:
                    log(f"→ Core PDP check failed: {e}")

                # ─── Open all buying choices ────────────────────────
                try:
                    # try primary button
                    aoc = wait.until(
                        EC.element_to_be_clickable(
                            (By.ID, "buybox-see-all-buying-choices")
                        )
                    )
                    log("→ Found buybox-see-all-buying-choices")
                except TimeoutException:
                    try:
                        aoc = wait.until(
                            EC.element_to_be_clickable((By.ID, "aod-ingress-link"))
                        )
                        log("→ Found aod-ingress-link fallback")
                    except Exception:
                        log(
                            "→ No 'see all buying choices' link found, skipping full-list checks"
                        )
                        continue

                try:
                    drv.execute_script("arguments[0].scrollIntoView(true);", aoc)
                    aoc.click()
                    time.sleep(4)
                    log("→ Offers list opened")
                except Exception as e:
                    log(f"→ Failed to open offers list: {e}")
                    continue

                # ─── Check pinned offer ─────────────────────────────
                try:
                    pinned = wait.until(
                        EC.presence_of_element_located((By.ID, "aod-pinned-offer"))
                    )
                    log("→ Found pinned-offer container")

                    # 1) Price
                    try:
                        price_span = pinned.find_element(By.ID, "aod-price-0")
                        # try offscreen
                        raw = price_span.find_element(
                            By.CSS_SELECTOR, "span.aok-offscreen"
                        ).text.strip()
                        if not raw:
                            # fallback to whole + fraction
                            whole = price_span.find_element(
                                By.CSS_SELECTOR, "span.a-price-whole"
                            ).text
                            frac = price_span.find_element(
                                By.CSS_SELECTOR, "span.a-price-fraction"
                            ).text
                            raw = f"{whole}.{frac}"
                            log(
                                f"→ Pinned-offer: offscreen empty, fallback raw='{raw}'"
                            )
                        else:
                            log(f"→ Pinned-offer: offscreen raw='{raw}'")
                        pinned_price = float(raw.replace("€", "").replace(",", ""))
                        log(f"→ Parsed pinned price: €{pinned_price:.2f}")
                    except:
                        log(f"→ Pinned-offer: price missing or parse failed")
                        raise  # stop pinned-check if we can’t get a price

                    # 2) Ships from
                    try:
                        # look under the right-hand grid for the “aod-offer-shipsFrom” entry
                        ships = pinned.find_elements(
                            By.CSS_SELECTOR,
                            "#aod-offer-shipsFrom .a-fixed-left-grid .a-fixed-left-grid-inner .a-fixed-left-grid-col.a-col-right .a-size-small.a-color-base",
                        )
                        if ships:
                            sf = ships[0].text.strip()
                            log(f"→ Parsed pinned ships-from: {sf}")
                        else:
                            sf = ""
                            log("→ Pinned-offer: no ships-from element found")
                    except Exception as e:
                        sf = ""
                        log(f"→ Pinned-offer: ships-from lookup failed: {e}")

                    # 3) Sold by
                    try:
                        sellers = pinned.find_elements(
                            By.CSS_SELECTOR,
                            "#aod-offer-soldBy .a-fixed-left-grid .a-fixed-left-grid-inner .a-fixed-left-grid-col.a-col-right a.a-size-small.a-link-normal",
                        )
                        if sellers:
                            sb = sellers[0].text.strip()
                            log(f"→ Parsed pinned sold-by: {sb}")
                        else:
                            sb = ""
                            log("→ Pinned-offer: no sold-by element found")
                    except Exception as e:
                        sb = ""
                        log(f"→ Pinned-offer: sold-by lookup failed: {e}")

                    # 4) Apply filters
                    if (
                        pinned_price <= item["target_price"]
                        and (not item.get("check_shipped") or "amazon" in sf.lower())
                        and (not item.get("check_sold") or "amazon" in sb.lower())
                    ):
                        msg = (
                            f"✅ {item['name']} is back in stock!\n"
                            f"✅ AMAZON OFFER FOUND!\n{url}\n"
                            f"💰 €{pinned_price:.2f} (≤ €{item['target_price']:.2f})\n"
                            f"🚚 Ships from: {sf}\n"
                            f"🏷️ Sold by: {sb}"
                        )
                        save_link_state(doc_id, {"available": True})
                        send_telegram(token, chat_id, msg)
                        log("→ Notifying pinned-offer match")
                        continue
                    else:
                        log("→ Pinned offer did not meet criteria")

                except:
                    log(f"→ Skipping pinned-offer")

                # ─── Scroll to load offers for up to 20 s ─────────────────────
                try:
                    scroller = wait.until(
                        EC.presence_of_element_located(
                            (By.ID, "all-offers-display-scroller")
                        )
                    )
                    start = time.time()

                    while time.time() - start < 10:
                        drv.execute_script(
                            "arguments[0].scrollTo(0, arguments[0].scrollHeight);",
                            scroller,
                        )
                        time.sleep(1)

                    elapsed = time.time() - start
                    log(f"→ Finished scrolling after {elapsed:.1f}s")

                except Exception as e:
                    log(f"→ Scrolling container failed or not present: {e}")

                # ─── Iterate full offer list ─────────────────────────
                try:
                    container = wait.until(
                        EC.presence_of_element_located((By.ID, "aod-offer-list"))
                    )
                    sections = container.find_elements(By.CSS_SELECTOR, "div.a-section")
                    log(f"→ Found {len(sections)} offer sections")

                    found = False
                    for idx, section in enumerate(sections, start=1):
                        offers = section.find_elements(
                            By.XPATH, ".//div[@id='aod-offer']"
                        )
                        log(f"→ Section {idx}: {len(offers)} offers")
                        for offer in offers:
                            # parse price
                            try:
                                whole = offer.find_element(
                                    By.CSS_SELECTOR, ".a-price-whole"
                                ).text.replace(".", "")
                                frac = offer.find_element(
                                    By.CSS_SELECTOR, ".a-price-fraction"
                                ).text
                                price = float(f"{whole}.{frac}")
                            except Exception:
                                log("   – skipping offer: price not found")
                                continue

                            # parse ships-from / sold-by
                            sf = sb = ""
                            try:
                                sf = offer.find_element(
                                    By.XPATH,
                                    ".//div[@id='aod-offer-shipsFrom']//span[contains(@class,'a-color-base')]",
                                ).text.strip()
                            except:
                                log("   – offer missing ships-from")
                            try:
                                # pick either the <a> or the <span> that carries the seller name
                                sb_el = offer.find_element(
                                    By.CSS_SELECTOR,
                                    "#aod-offer-soldBy a.a-link-normal, #aod-offer-soldBy span.a-color-base"
                                )
                                sb = sb_el.text.strip()
                            except:
                                log("   – offer missing sold-by")

                            log(
                                f"   → Offer €{price:.2f}, Ships from “{sf}”, Sold by “{sb}”"
                            )

                            # apply filters
                            if price > item["target_price"]:
                                continue
                            if item.get("check_shipped") and "amazon" not in sf.lower():
                                continue
                            if item.get("check_sold") and "amazon" not in sb.lower():
                                continue

                            # match!
                            msg = (
                                f"✅ {item['name']} is back in stock!\n"
                                f"✅ AMAZON OFFER FOUND!\n{url}\n"
                                f"💰 €{price:.2f} (≤ €{item['target_price']:.2f})\n"
                                f"🚚 Ships from: {sf}\n"
                                f"🏷️ Sold by: {sb}"
                            )
                            save_link_state(doc_id, {"available": True})
                            send_telegram(token, chat_id, msg)
                            log("→ Notifying list-offer match")
                            found = True
                            break

                        if found:
                            break

                    if not found:
                        log("→ No offer met criteria in full list")

                except Exception as e:
                    log(f"→ Offer-list not present or parsing failed: {e}")

            except TimeoutException as e:
                log(f"Timeout on {url}: {e}")
            except Exception as e:
                log(f"Error checking {url}: {e}")

    finally:
        global _driver
        if _driver:
            _driver.quit()
            _driver = None


def _safe_text(ctx, by, selector):
    try:
        return ctx.find_element(by, selector).text.strip()
    except:
        return ""


def _check_core_offer(drv, wait, item):
    """
    Returns True if the price / ships-from / sold-by read from the main PDP
    meet the item’s criteria, setting globals _CORE_PRICE, _CORE_SHIPS, _CORE_SOLD.
    """
    global _CORE_PRICE, _CORE_SHIPS, _CORE_SOLD
    try:
        # 1) price
        price_div = wait.until(
            EC.presence_of_element_located((By.ID, "corePrice_feature_div"))
        )
        offscreen = price_div.find_element(
            By.CSS_SELECTOR, ".a-offscreen"
        ).get_attribute("innerText")
        # strip currency symbol, convert
        _CORE_PRICE = float(offscreen.replace("€", "").replace(",", "").strip())

        # 2) ships-from  / sold-by
        feat = wait.until(
            EC.presence_of_element_located((By.ID, "offer-display-features"))
        )
        # uses the “feature-text-message” spans
        _CORE_SHIPS = _safe_text(
            feat,
            By.CSS_SELECTOR,
            "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message",
        )
        _CORE_SOLD = _safe_text(
            feat,
            By.CSS_SELECTOR,
            "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
        )

        log(
            f"→ Core PDP €{_CORE_PRICE:.2f}, Ships from “{_CORE_SHIPS}”, Sold by “{_CORE_SOLD}”"
        )

        # 3) apply filters
        if _CORE_PRICE > item["target_price"]:
            return False
        if item.get("check_shipped") and "amazon" not in _CORE_SHIPS.lower():
            return False
        if item.get("check_sold") and "amazon" not in _CORE_SOLD.lower():
            return False

        return True
    except Exception:
        return False

if __name__ == "__main__":
    log("⭐️ AmazonWatcher continuous mode starting…")
    while True:
        try:
            check_once()
        except Exception as e:
            log(f"Error in check loop: {e}")
        log("Sleeping for 300 seconds…")
        time.sleep(60)
