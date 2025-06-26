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
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
import threading

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

def init_driver():
    opts = Options()

    # Headless configuration
    opts.add_argument("--headless=new")  # Use new headless mode
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option(
        "excludeSwitches", ["enable-automation", "enable-logging"]
    )
    opts.add_experimental_option("useAutomationExtension", False)

    # Window size randomization
    #width = random.randint(1200, 1920)
    #height = random.randint(800, 1080)
    #opts.add_argument(f"--window-size={width},{height}")

    # User-Agent randomization
    chrome_version = f"{random.randint(100,115)}.0.{random.randint(1000,5000)}.{random.randint(1,200)}"
    platforms = [
        "(Windows NT 10.0; Win64; x64)",
        "(X11; Linux x86_64)",
        "(Macintosh; Intel Mac OS X 13_5)",
    ]
    ua = f"Mozilla/5.0 {random.choice(platforms)} AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
    opts.add_argument(f"user-agent={ua}")

    # Additional stealth parameters
    opts.add_argument(f"--lang=en-US,en;q=0.{random.randint(5,9)}")
    opts.add_argument("--disable-webgl")
    opts.add_argument("--disable-popup-blocking")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    try:
        service = Service(CHROMEDRIVER)
        _driver = webdriver.Chrome(service=service, options=opts)

        # Execute multiple CDP commands
        stealth_script = """
            const newProto = navigator.__proto__;
            delete newProto.webdriver;
            navigator.__proto__ = newProto;
            window.navigator.chrome = {
                app: {
                    isInstalled: false,
                },
                webstore: {
                    onInstallStageChanged: {},
                    onDownloadProgress: {},
                },
                runtime: {
                    PlatformOs: {
                        MAC: 'mac',
                        WIN: 'win',
                        ANDROID: 'android',
                        CROS: 'cros',
                        LINUX: 'linux',
                        OPENBSD: 'openbsd',
                    },
                    PlatformArch: {
                        ARM: 'arm',
                        X86_32: 'x86-32',
                        X86_64: 'x86-64',
                    },
                    PlatformNaclArch: {
                        ARM: 'arm',
                        X86_32: 'x86-32',
                        X86_64: 'x86-64',
                    },
                    RequestUpdateCheckStatus: {
                        THROTTLED: 'throttled',
                        NO_UPDATE: 'no_update',
                        UPDATE_AVAILABLE: 'update_available',
                    },
                    OnInstalledReason: {
                        INSTALL: 'install',
                        UPDATE: 'update',
                        CHROME_UPDATE: 'chrome_update',
                        SHARED_MODULE_UPDATE: 'shared_module_update',
                    },
                },
            };
            Object.defineProperty(navigator, 'plugins', {
                get: () => [{
                    name: 'Chrome PDF Plugin',
                    filename: 'internal-pdf-viewer',
                    description: 'Portable Document Format',
                    version: '1',
                }],
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
            });
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 4,
            });
            Object.defineProperty(Notification, 'permission', {
                get: () => 'denied',
            });
        """

        # Apply stealth parameters
        _driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
        _driver.execute_cdp_cmd(
            "Emulation.setScriptExecutionDisabled", {"value": False}
        )
        _driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": stealth_script}
        )

        # Disable automation flags
        _driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                """
            },
        )

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


# ─── Set Italy Delivery ────────────────────────────────
def set_italy_delivery_once(drv, wait):
    try:
        log("→ Refreshing Webpage")
        drv.refresh()
        time.sleep(5)
        try:
            current = wait.until(
                EC.presence_of_element_located(
                    (By.ID, "glow-ingress-line2")
                )
            ).text.strip()
            if "00049" in current:
                log("→ Delivery already set to Italy (00049); skipping")
                return True
        except Exception:
            # element not found or no text → fall through to setting
            pass
            
        log("→ Setting delivery to Italy (00049)…")
        wait.until(
            EC.element_to_be_clickable((By.ID, "nav-global-location-popover-link"))
        ).click()
        log("→ Clicked popup to open")
        time.sleep(2)
        zip_in = wait.until(
            EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput"))
        )
        log("→ Found Field")
        zip_in.clear()
        log("→ Field Cleared")
        time.sleep(2)
        zip_in.send_keys("00049", Keys.ENTER)
        log("→ Entered Adddress")
        time.sleep(2)
        pop = wait.until(
            EC.presence_of_element_located((By.CLASS_NAME, "a-popover-footer"))
        )
        log("→ Found Footer")
        pop.find_element(By.XPATH, "./*").click()
        log("→ Clicked Done")
        time.sleep(2)
        log("→ Delivery set to Italy 00049")
        return True
    except Exception as e:
        log(f"→ Could not set Italy delivery (already set?). Problem: {e}")
        return False


def check_single_link(doc_id, item, token, chat_id, cool_time):
    """
    Open a fresh browser for item['url'], run the exact same checks
    you had in check_once() for one link, then quit & sleep 10s.
    Loop forever.
    """
    url = item["url"]

    while True:
        doc = db.collection("links").document(doc_id).get()
        if not doc.exists:
            log(f"[{doc_id}] Link deleted—shutting down worker")
            return
        item = doc.to_dict()

        # Cool Down time checking
        if item.get("available"):
            now = time.time()
            since = item.get("available_since")

            # First time we see available=true: record timestamp and skip
            if since is None:
                log(f"→ {url} marked available; starting cool-down of {cool_time}s")
                save_link_state(doc_id, {"available_since": now})
                # we _just_ started cool-down, so sleep full duration
                time.sleep(cool_time)
                continue

            elapsed = now - since
            if elapsed < cool_time:
                remaining = cool_time - elapsed
                log(
                    f"→ {url} still in cool-down ({elapsed:.0f}/{cool_time}s); "
                    f"sleeping {remaining:.0f}s before next check"
                )
                time.sleep(remaining)
                # after sleeping, on next iteration 'available_since' is old
                # so we'll fall through, reset it, and then loop back around
                continue

            # Cool-down expired: reset flag and let the rest of your check run
            log(
                f"→ Cool-down expired for {url}; resetting availability and re-checking"
            )
            save_link_state(
                doc_id,
                {"available": False, "available_since": firestore.DELETE_FIELD},
            )
            # update our local copy so downstream logic sees available=False
            item["available"] = False
            
        # 1) new browser instance
        drv = init_driver()
        wait = WebDriverWait(drv, 5)

        drv.get("https://www.amazon.it/-/en/ref=nav_logo")
        time.sleep(5)
        if not set_italy_delivery_once(drv, WebDriverWait(drv, 15)):
            try:
                drv.quit()
            except:
                pass
            log(f"[{doc_id}] Browser closed; sleeping 10s")
            time.sleep(5)
            continue

        try:
            url = item["url"]
            log(f"Loading page: {url}")

            try:
                drv.get(url)
                time.sleep(2)

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

                time.sleep(3)

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

                time.sleep(4)

                try:
                    drv.execute_script("arguments[0].scrollIntoView(true);", aoc)
                    time.sleep(2)
                    aoc.click()
                    time.sleep(2)
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

                    while time.time() - start < 5:
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
                                    "#aod-offer-soldBy a.a-link-normal, #aod-offer-soldBy span.a-color-base",
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
            # 2) teardown
            try:
                drv.quit()
            except:
                pass
            log(f"[{doc_id}] Browser closed; sleeping 10s")

        time.sleep(5)


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
    # Ensure fresh processes (no inherited gRPC threads)
    mp_ctx = mp.get_context("spawn")

    log("⭐️ AmazonWatcher real-time mode starting…")

    # 1) load config once
    cfg     = load_config()
    token   = cfg.get("token")
    chat_id = cfg.get("chat_id")
    cool    = cfg.get("cool_time", 300)

    # 2) set up executor & tracking, using spawn context
    executor       = ProcessPoolExecutor(mp_context=mp_ctx)
    active_workers = {}  # doc_id -> Future

    # 3) inline snapshot callback
    def on_links_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            doc    = change.document
            doc_id = doc.id
            item   = doc.to_dict()

            if change.type.name == "ADDED":
                log(f"→ Link added: {doc_id}; spawning worker")
                future = executor.submit(
                    check_single_link, doc_id, item, token, chat_id, cool
                )
                active_workers[doc_id] = future

            elif change.type.name == "MODIFIED":
                log(f"→ Link modified: {doc_id}; restarting worker")
                old = active_workers.get(doc_id)
                if old and not old.done():
                    old.cancel()
                future = executor.submit(
                    check_single_link, doc_id, item, token, chat_id, cool
                )
                active_workers[doc_id] = future

            elif change.type.name == "REMOVED":
                log(f"→ Link removed: {doc_id}; cancelling worker")
                old = active_workers.pop(doc_id, None)
                if old and not old.done():
                    old.cancel()

    # 4) attach real-time listener
    listener = db.collection("links").on_snapshot(on_links_snapshot)

    # 5) block until Ctrl+C
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("Shutting down…")
        listener.unsubscribe()
        executor.shutdown(wait=False)
