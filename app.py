import os
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = 'yugioh_super_secret_key_change_me'

# ==========================================
# ΡΥΘΜΙΣΕΙΣ ΣΥΝΔΕΣΗΣ SUPABASE (POSTGRESQL)
# ==========================================
DATABASE_URL = "postgresql://postgres.ymaqwysbwwbgwrvxdxau:mL8eG-nZ7UnerZd@aws-1-eu-west-1.pooler.supabase.com:6543/postgres"

# Προαιρετικές ρυθμίσεις ειδοποιήσεων
DISCORD_WEBHOOK_URL = ""  
GMAIL_USER = ""         
GMAIL_APP_PASSWORD = ""   
RECEIVER_EMAIL = ""       

# Flask-Login Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id, username):
        self.id = str(id)
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
    user_data = c.fetchone()
    conn.close()
    if user_data:
        return User(user_data['id'], user_data['username'])
    return None

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    # Πίνακας Χρηστών
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    
    # Πίνακας Καρτών (συνδεδεμένος με user_id)
    c.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
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

    # Πίνακας Ρυθμίσεων API ανά χρήστη
    c.execute('''
        CREATE TABLE IF NOT EXISTS api_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT DEFAULT 'free_ygopro',
            api_key TEXT DEFAULT '',
            custom_url TEXT DEFAULT 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}'
        )
    ''')
    
    conn.commit()
    conn.close()

# Δημιουργία πινάκων αυτόματα στην εκκίνηση
init_db()

def get_user_api_settings(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM api_settings WHERE user_id = %s", (user_id,))
    settings = c.fetchone()
    if not settings:
        c.execute("INSERT INTO api_settings (user_id, provider, api_key, custom_url) VALUES (%s, 'free_ygopro', '', 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}')", (user_id,))
        conn.commit()
        c.execute("SELECT * FROM api_settings WHERE user_id = %s", (user_id,))
        settings = c.fetchone()
    conn.close()
    return settings

# --- ΔΩΡΕΑΝ ΛΕΙΤΟΥΡΓΙΑ (YGOPRODeck API) ---
def fetch_from_free_api(card_name):
    try:
        clean_name = card_name.strip()
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

# --- AUTH ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username = %s", (username,))
        user_row = c.fetchone()
        conn.close()
        
        if user_row and check_password_hash(user_row['password'], password):
            user = User(user_row['id'], user_row['username'])
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Λάθος όνομα χρήστη ή κωδικός!', 'danger')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username = %s", (username,))
        if c.fetchone():
            conn.close()
            flash('Το όνομα χρήστη υπάρχει ήδη!', 'danger')
            return redirect(url_for('register'))
            
        hashed_password = generate_password_hash(password)
        c.execute("INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id", (username, hashed_password))
        user_id = c.fetchone()['id']
        conn.commit()
        conn.close()
        
        user = User(user_id, username)
        login_user(user)
        return redirect(url_for('index'))
        
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/api/search_cards')
@login_required
def api_search_cards():
    query = request.args.get('fname', '').strip()
    if len(query) < 3:
        return jsonify([])
    try:
        url = f"https://db.ygoprodeck.com/api/v7/cardinfo.php?fname={requests.utils.quote(query)}&num=10&offset=0"
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
@login_required
def index():
    search_query = request.args.get('q', '').lower()
    conn = get_db_connection()
    c = conn.cursor()
    
    if search_query:
        c.execute("""
            SELECT * FROM cards 
            WHERE user_id = %s AND (lower(name) LIKE %s OR lower(set_code) LIKE %s OR lower(rarity) LIKE %s) 
            ORDER BY id DESC
        """, (current_user.id, f'%{search_query}%', f'%{search_query}%', f'%{search_query}%'))
    else:
        c.execute("SELECT * FROM cards WHERE user_id = %s ORDER BY id DESC", (current_user.id,))
        
    cards = c.fetchall()

    total_my_value = sum(card['my_price'] * card['quantity'] for card in cards)
    total_cm_value = sum((card['cm_low'] if card['cm_low'] > 0 else card['cm_avg']) * card['quantity'] for card in cards)

    conn.close()
    settings = get_user_api_settings(current_user.id)
    return render_template('index.html', cards=cards, total_my=total_my_value, total_cm=total_cm_value, search=search_query, settings=settings)

@app.route('/save_settings', methods=['POST'])
@login_required
def save_settings():
    provider = request.form.get('provider', 'free_ygopro')
    api_key = request.form.get('api_key', '').strip()
    custom_url = request.form.get('custom_url', '').strip()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE api_settings SET provider = %s, api_key = %s, custom_url = %s WHERE user_id = %s
    """, (provider, api_key, custom_url, current_user.id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/add', methods=['POST'])
@login_required
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

    _, _, _, image_url = fetch_from_free_api(name)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO cards (user_id, name, set_code, rarity, my_price, quantity, category, alert_percent, cm_url, image_url) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (current_user.id, name, set_code, rarity, my_price, quantity, category, alert_percent, cm_url, image_url))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/update_card/<int:card_id>', methods=['POST'])
@login_required
def update_card(card_id):
    new_price = float(request.form['my_price'])
    new_qty = int(request.form['quantity'])
    new_cat = request.form['category']
    new_rarity = request.form['rarity']
    new_alert = float(request.form['alert_percent'])
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE cards 
        SET my_price = %s, quantity = %s, category = %s, rarity = %s, alert_percent = %s 
        WHERE id = %s AND user_id = %s
    ''', (new_price, new_qty, new_cat, new_rarity, new_alert, card_id, current_user.id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete/<int:card_id>')
@login_required
def delete_card(card_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM cards WHERE id = %s AND user_id = %s", (card_id, current_user.id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/refresh')
@login_required
def manual_refresh():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name FROM cards WHERE user_id = %s", (current_user.id,))
    cards = c.fetchall()
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    for card in cards:
        cid, name = card['id'], card['name']
        _, cm_avg, tcg_usd, img_url = fetch_from_free_api(name)

        if cm_avg > 0 or tcg_usd > 0:
            c.execute('''
                UPDATE cards 
                SET cm_avg = %s, tcg_price = %s, image_url = %s, last_checked = %s
                WHERE id = %s AND user_id = %s
            ''', (cm_avg, tcg_usd, img_url, now_str, cid, current_user.id))
            
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

    import os

    if __name__ == '__main__':
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port)