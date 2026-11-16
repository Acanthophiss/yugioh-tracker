import sqlite3
import time
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ==========================================
# ΡΥΘΜΙΣΕΙΣ ΕΙΔΟΠΟΙΗΣΕΩΝ (Συμπλήρωσε τα δικά σου)
# ==========================================
DISCORD_WEBHOOK_URL = ""  
GMAIL_USER = ""           
GMAIL_APP_PASSWORD = ""   
RECEIVER_EMAIL = ""       
# ==========================================

DB_NAME = "cards.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            set_code TEXT NOT NULL,
            rarity TEXT NOT NULL,
            my_price REAL NOT NULL,
            quantity INTEGER DEFAULT 1,
            category TEXT DEFAULT 'Πώληση',
            alert_percent REAL DEFAULT 15.0,
            cm_url TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            cm_low REAL DEFAULT 0.0,
            cm_avg REAL DEFAULT 0.0,
            tcg_price REAL DEFAULT 0.0,
            last_checked TEXT DEFAULT 'Ποτέ'
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS api_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            provider TEXT DEFAULT 'free_ygopro',
            api_key TEXT DEFAULT '',
            custom_url TEXT DEFAULT 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}'
        )
    ''')
    
    c.execute("INSERT OR IGNORE INTO api_settings (id, provider, api_key, custom_url) VALUES (1, 'free_ygopro', '', 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}')")
    
    conn.commit()
    conn.close()

def get_api_settings():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM api_settings WHERE id = 1")
    settings = c.fetchone()
    conn.close()
    return settings

# --- ΣΥΝΑΡΤΗΣΕΙΣ ΕΙΔΟΠΟΙΗΣΕΩΝ ---
def send_notifications(message):
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        except Exception as e:
            print("Σφάλμα Discord:", e)

    if GMAIL_USER and GMAIL_APP_PASSWORD and RECEIVER_EMAIL:
        try:
            msg = MIMEText(message)
            msg['Subject'] = 'Yu-Gi-Oh! Ειδοποίηση Τιμής Κάρτας'
            msg['From'] = GMAIL_USER
            msg['To'] = RECEIVER_EMAIL

            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
        except Exception as e:
            print("Σφάλμα Email:", e)

# --- ΔΩΡΕΑΝ ΛΕΙΤΟΥΡΓΙΑ (YGOPRODeck API) ---
def fetch_from_free_api(card_name):
    try:
        clean_name = card_name.strip()
        # Χρησιμοποιούμε requests.utils.quote για να κωδικοποιηθούν σωστά τα σύμβολα όπως το &
        encoded_name = requests.utils.quote(clean_name)
        url = f"https://db.ygoprodeck.com/api/v7/cardinfo.php?name={encoded_name}"
        response = requests.get(url, timeout=15)
        
        cm_avg = 0.0
        tcg_usd = 0.0
        img_url = ""

        if response.status_code == 200:
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                card = data["data"][0]
                if "card_images" in card and len(card["card_images"]) > 0:
                    img_url = card["card_images"][0].get("image_url_small", "")
                
                if "card_prices" in card and len(card["card_prices"]) > 0:
                    prices = card["card_prices"][0]
                    cm_avg = float(prices.get("cardmarket_price", 0.0))
                    tcg_usd = float(prices.get("tcgplayer_price", 0.0))

        return 0.0, cm_avg, tcg_usd, img_url
    except Exception as e:
        print(f"Σφάλμα Free API για {card_name}: {e}")
        return 0.0, 0.0, 0.0, ""

# --- CUSTOM / PAID API ΛΕΙΤΟΥΡΓΙΑ ---
def fetch_from_custom_api(card_name, set_code, rarity, cm_url, api_key, custom_url_template):
    _, fallback_avg, tcg_usd, img_url = fetch_from_free_api(card_name)
    try:
        formatted_url = custom_url_template.format(
            api_key=api_key.strip(),
            name=requests.utils.quote(card_name.strip()),
            set_code=requests.utils.quote(set_code.strip()),
            rarity=requests.utils.quote(rarity.strip()),
            cm_url=requests.utils.quote(cm_url.strip())
        )
        
        response = requests.get(formatted_url, timeout=30)
        if response.status_code != 200:
            return 0.0, fallback_avg, tcg_usd, img_url

        content_type = response.headers.get('Content-Type', '')
        if 'application/json' in content_type:
            data = response.json()
            low_val = float(data.get('low', data.get('min_price', data.get('cm_low', 0.0))))
            avg_val = float(data.get('trend', data.get('avg', data.get('average_price', data.get('price', fallback_avg)))))
            return low_val, avg_val, tcg_usd, img_url
        else:
            soup = BeautifulSoup(response.text, 'html.parser')
            cm_low = 0.0
            cm_avg = 0.0
            for dt in soup.find_all('dt'):
                label = dt.get_text(strip=True).lower()
                dd = dt.find_next_sibling('dd')
                if dd:
                    text_val = dd.get_text(strip=True).replace('€', '').replace('.', '').replace(',', '.').strip()
                    match = re.search(r'\d+(\.\d+)?', text_val)
                    if match:
                        val = float(match.group())
                        if 'from' in label or 'από' in label:
                            cm_low = val
                        elif 'price trend' in label or 'τάση' in label:
                            cm_avg = val
            return cm_low, (cm_avg if cm_avg > 0 else fallback_avg), tcg_usd, img_url
    except Exception as e:
        print(f"Εξαίρεση Custom API: {e}")
        return 0.0, fallback_avg, tcg_usd, img_url

# --- ΕΛΕΓΧΟΣ ΟΛΩΝ ΤΩΝ ΚΑΡΤΩΝ ---
def update_all_prices():
    settings = get_api_settings()
    provider = settings['provider']
    api_key = settings['api_key']
    custom_url = settings['custom_url']

    print(f"Ξεκινάει ο έλεγχος τιμών (Λειτουργία: {provider})...")
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, name, set_code, rarity, my_price, alert_percent, cm_url FROM cards")
    cards = c.fetchall()

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    for index, card in enumerate(cards):
        cid, name, set_code, rarity, my_price, alert_percent, cm_url = card
        
        if provider == 'custom_api' and custom_url:
            cm_low, cm_avg, tcg_usd, img_url = fetch_from_custom_api(name, set_code, rarity, cm_url, api_key, custom_url)
        else:
            cm_low, cm_avg, tcg_usd, img_url = fetch_from_free_api(name)

        if cm_low > 0 or cm_avg > 0 or tcg_usd > 0:
            c.execute('''
                UPDATE cards 
                SET cm_low = ?, cm_avg = ?, tcg_price = ?, image_url = ?, last_checked = ?
                WHERE id = ?
            ''', (cm_low, cm_avg, tcg_usd, img_url, now_str, cid))

            compare_price = cm_low if cm_low > 0 else cm_avg
            threshold = alert_percent / 100.0

            if compare_price > my_price * (1 + threshold):
                msg = f"📈 **ΑΥΞΗΣΗ ΤΙΜΗΣ (+{alert_percent:.0f}%)!** Η κάρτα **{name} ({set_code} - {rarity})** ανέβηκε!\n• Δική σου τιμή: {my_price:.2f}€\n• Τιμή Αγοράς: {compare_price:.2f}€\n• Link: {cm_url}"
                send_notifications(msg)
            elif compare_price < my_price * (1 - threshold) and compare_price > 0:
                msg = f"📉 **ΠΤΩΣΗ ΤΙΜΗΣ (-{alert_percent:.0f}%)!** Η κάρτα **{name} ({set_code} - {rarity})** έπεσε!\n• Δική σου τιμή: {my_price:.2f}€\n• Τιμή Αγοράς: {compare_price:.2f}€\n• Link: {cm_url}"
                send_notifications(msg)

        if index < len(cards) - 1:
            time.sleep(1.0)

    conn.commit()
    conn.close()
    print("Ο έλεγχος ολοκληρώθηκε!")

@app.route('/api/search_cards')
def api_search_cards():
    query = request.args.get('fname', '').strip()
    if len(query) < 3:
        return jsonify([])
    try:
        url = f"https://db.ygoprodeck.com/api/v7/cardinfo.php?fname={query}&num=10&offset=0"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            results = []
            for card in data:
                sets = []
                for s in card.get("card_sets", []):
                    sets.append({
                        "set_code": s.get("set_code", ""),
                        "set_rarity": s.get("set_rarity", ""),
                        "set_name": s.get("set_name", "")
                    })
                results.append({
                    "name": card.get("name", ""),
                    "image": card.get("card_images", [{}])[0].get("image_url_small", ""),
                    "sets": sets
                })
            return jsonify(results)
    except Exception as e:
        print("Autocomplete error:", e)
    return jsonify([])

@app.route('/')
def index():
    search_query = request.args.get('q', '').lower()
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if search_query:
        c.execute("SELECT * FROM cards WHERE lower(name) LIKE ? OR lower(set_code) LIKE ? OR lower(rarity) LIKE ? ORDER BY id DESC", 
                  (f'%{search_query}%', f'%{search_query}%', f'%{search_query}%'))
    else:
        c.execute("SELECT * FROM cards ORDER BY id DESC")
        
    cards = c.fetchall()

    total_my_value = sum(card['my_price'] * card['quantity'] for card in cards)
    total_cm_value = sum((card['cm_low'] if card['cm_low'] > 0 else card['cm_avg']) * card['quantity'] for card in cards)

    conn.close()
    settings = get_api_settings()
    return render_template('index.html', cards=cards, total_my=total_my_value, total_cm=total_cm_value, search=search_query, settings=settings)

@app.route('/save_settings', methods=['POST'])
def save_settings():
    provider = request.form.get('provider', 'free_ygopro')
    api_key = request.form.get('api_key', '').strip()
    custom_url = request.form.get('custom_url', '').strip()

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE api_settings SET provider = ?, api_key = ?, custom_url = ? WHERE id = 1", (provider, api_key, custom_url))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/add', methods=['POST'])
def add_card():
    name = request.form['name']
    set_code = request.form['set_code']
    rarity = request.form['rarity']
    my_price = float(request.form['my_price'])
    quantity = int(request.form.get('quantity', 1))
    category = request.form.get('category', 'Πώληση')
    alert_percent = float(request.form.get('alert_percent', 15.0))
    
    cm_url = request.form.get('cm_url', '').strip()
    if not cm_url:
        search_term = name.replace(' ', '+')
        cm_url = f"https://www.cardmarket.com/en/YuGiOh/Products/Search?searchString={search_term}"

    # Τραβάμε απευθείας την εικόνα από το δωρεάν API κατά την προσθήκη
    _, _, _, image_url = fetch_from_free_api(name)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        INSERT INTO cards (name, set_code, rarity, my_price, quantity, category, alert_percent, cm_url, image_url) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (name, set_code, rarity, my_price, quantity, category, alert_percent, cm_url, image_url))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/update_card/<int:card_id>', methods=['POST'])
def update_card(card_id):
    new_price = float(request.form['my_price'])
    new_qty = int(request.form['quantity'])
    new_cat = request.form['category']
    new_rarity = request.form['rarity']
    new_alert = float(request.form['alert_percent'])
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        UPDATE cards 
        SET my_price = ?, quantity = ?, category = ?, rarity = ?, alert_percent = ? 
        WHERE id = ?
    ''', (new_price, new_qty, new_cat, new_rarity, new_alert, card_id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete/<int:card_id>')
def delete_card(card_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/refresh')
def manual_refresh():
    update_all_prices()
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=update_all_prices, trigger="interval", hours=6)
    scheduler.start()
    
    app.run(debug=False, port=5000)