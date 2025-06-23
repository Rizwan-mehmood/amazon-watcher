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
        try:
            log("🔍 Checking for anti-bot validation button…")
            validate_btn = drv.find_element(By.XPATH, "//button[contains(text(), 'Continua con gli acquisti')]")
            validate_btn.click()
            log("✅ Clicked 'Continua con gli acquisti' button")
            time.sleep(3)
        except:
            log("ℹ️ No anti-bot button detected, continuing normally")
        log("→ Setting delivery to Italy (00049)…")
        wait.until(
            EC.element_to_be_clickable((By.ID, "nav-global-location-slot"))
        ).click()
        log("→ Clicked the container")
        zip_in = wait.until(
            EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput"))
        )
        log("→ Found Input field")
        zip_in.clear()
        log("→ Cleared")
        zip_in.send_keys("00049", Keys.ENTER)
        log("→ Send and enter")
        time.sleep(4)
        pop = wait.until(
            EC.presence_of_element_located((By.CLASS_NAME, "a-popover-footer"))
        )
        log("→ Found footer")
        pop.find_element(By.XPATH, "./*").click()
        log("→ Clicked span")
        time.sleep(4)
        drv.refresh()
        time.sleep(4)
        log("→ Delivery set to Italy 00049")
    except Exception as e:
        log(f"❌ Failed to set Italy delivery: {type(e).__name__} - {e}")



# ─── Core check loop ────────────────────────────────────
def check_once():
    cfg = load_config()
    token = cfg.get("token")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        raise RuntimeError("Missing token/chat_id in Firestore config")

    drv = init_driver()
    wait = WebDriverWait(drv, 20)

    drv.get("https://www.amazon.it/-/en/ref=nav_logo")

    time.sleep(20)

    set_italy_delivery_once(drv, wait)
    
    try:
        for doc_id, item in load_links():
            if item.get("available"):
                continue

            url = item["url"]
            log(f"Loading page: {url}")
            try:
                drv.get(url)
                time.sleep(8)

                # ─── Check out-of-stock ───────────────────────────
                try:
                    wait.until(EC.presence_of_element_located((By.ID, "outOfStock")))
                    log("→ Still out of stock")
                    continue
                except:
                    pass

                # ─── Dismiss cookies ───────────────────────────────
                try:
                    cookie = wait.until(
                        EC.presence_of_element_located((By.ID, "sp-cc-rejectall-link"))
                    )
                    cookie.click()
                    log("→ Cookies dismissed")
                except:
                    pass

                # ─── Open all buying choices ───────────────────────
                aoc = wait.until(
                    EC.presence_of_element_located(
                        (By.ID, "buybox-see-all-buying-choices")
                    )
                )
                drv.execute_script("arguments[0].scrollIntoView(true);", aoc)
                aoc.click()
                time.sleep(6)
                log("→ Offers list opened")

                # ─── Iterate offers ───────────────────────────────
                container = wait.until(
                    EC.presence_of_element_located((By.ID, "aod-offer-list"))
                )
                wrapper = container.find_element(By.XPATH, "./div")
                offers = wrapper.find_elements(By.XPATH, "./div[@id='aod-offer']")

                found = False
                for offer in offers:
                    try:
                        whole = offer.find_element(
                            By.CSS_SELECTOR, ".a-price-whole"
                        ).text.replace(".", "")
                        frac = offer.find_element(
                            By.CSS_SELECTOR, ".a-price-fraction"
                        ).text
                        price = float(f"{whole}.{frac}")
                    except:
                        continue

                    # Ships from / Sold by
                    try:
                        sf = offer.find_element(
                            By.XPATH,
                            ".//div[@id='aod-offer-shipsFrom']//span[contains(@class,'a-color-base')]",
                        ).text.strip()
                    except:
                        sf = ""
                    try:
                        sb = offer.find_element(
                            By.XPATH,
                            ".//div[@id='aod-offer-soldBy']//a[contains(@class,'a-link-normal')]",
                        ).text.strip()
                    except:
                        sb = ""

                    log(f"  → Offer €{price:.2f}, Ships from “{sf}”, Sold by “{sb}”")

                    if price > item["target_price"]:
                        continue
                    if item.get("check_shipped") and "amazon" not in sf.lower():
                        continue
                    if item.get("check_sold") and "amazon" not in sb.lower():
                        continue

                    # ─── Found one! ────────────────────────────────
                    msg = (
                        f"✅ AMAZON OFFER FOUND!\n{url}\n"
                        f"💰 €{price:.2f} (≤ €{item['target_price']:.2f})\n"
                        f"🚚 Ships from: {sf}\n"
                        f"🏷️ Sold by: {sb}"
                    )
                    save_link_state(doc_id, {"available": True})
                    send_telegram(token, chat_id, msg)
                    found = True
                    break

                if not found:
                    log("→ No offer met criteria")

            except TimeoutException as e:
                log(f"Timeout on {url}: {e}")
            except Exception as e:
                log(f"Error checking {url}: {e}")

    finally:
        global _driver
        if _driver:
            _driver.quit()
            _driver = None


if __name__ == "__main__":
    log("⭐️ AmazonWatcher continuous mode starting…")
    while True:
        try:
            check_once()
        except Exception as e:
            log(f"Error in check loop: {e}")
        log("Sleeping for 300 seconds…")
        time.sleep(60)
