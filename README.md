# Amazon Stock & Price Watcher (Backend)

A headless Selenium bot that reads URLs from Firestore, scans Amazon for offers ≤ your target price, and notifies you via Telegram.

## Setup

1. **Firebase**  
   - Create a project, enable Firestore.  
   - Add collections:
     - `config/settings` → `{ token: "...", chat_id: "..." }`
     - `links/` → add docs with fields:  
       `{ url, target_price, check_shipped, check_sold, available:false }`
   - Generate a service-account JSON.

2. **Environment**  
   - In Render (or locally), set:
     - `FIREBASE_SERVICE_ACCOUNT_JSON` → _contents_ of your service-account JSON  
     - Optional: `CHECK_INTERVAL`, `LOG=true`

3. **Deploy on Render**  
   - Create a **Background Worker** (no HTTP).  
   - Link your GitHub repo.  
   - Build command: _no change_ (uses Dockerfile)  
   - Start command: _no change_ (`python main.py`)  

Render will build the Docker image (with Chrome + chromedriver), start your bot, and keep it running.

---

Once deployed, your bot will automatically:
1. Load all links from Firestore  
2. Run headlessly in a loop  
3. Send you Telegram alerts  

Feel free to let me know if any step needs clarification!
