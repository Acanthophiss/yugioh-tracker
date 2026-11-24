import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'yugioh_super_secret_key_change_me')

# ==========================================
# ΡΥΘΜΙΣΕΙΣ ΣΥΝΔΕΣΗΣ SUPABASE (POSTGRESQL)
# ==========================================
DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    "postgresql://postgres.ymaqwysbwwbgwrvxdxau:mL8eG-nZ7UnerZd@aws-1-eu-west-1.pooler.supabase.com:6543/postgres"
)

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

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')

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
            gr_low REAL DEFAULT 0.0,
            cm_avg REAL DEFAULT 0.0,
            last_checked TEXT DEFAULT 'Ποτέ'
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS api_settings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            provider TEXT DEFAULT 'free_ygopro',
            api_key TEXT DEFAULT '',
            custom_url TEXT DEFAULT 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}&render_js=true&premium_proxy=true',
            max_workers INTEGER DEFAULT 5
        )
    ''')

    # Ασφαλής προσθήκη νέων στηλών αν ο πίνακας υπήρχε ήδη
    c.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS gr_low REAL DEFAULT 0.0")
    c.execute("ALTER TABLE api_settings ADD COLUMN IF NOT EXISTS max_workers INTEGER DEFAULT 5")

    conn.commit()
    conn.close()


init_db()


def sanitize_workers(val):
    try:
        num = int(val)
        return max(1, min(num, 10))
    except (TypeError, ValueError):
        return 5


def get_user_api_settings(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM api_settings WHERE user_id = %s", (user_id,))
    settings = c.fetchone()
    if not settings:
        c.execute(
            """
            INSERT INTO api_settings (user_id, provider, api_key, custom_url, max_workers)
            VALUES (%s, 'free_ygopro', '', 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}&render_js=true&premium_proxy=true', 5)
            """,
            (user_id,)
        )
        conn.commit()
        c.execute("SELECT * FROM api_settings WHERE user_id = %s", (user_id,))
        settings = c.fetchone()
    conn.close()
    settings = dict(settings)
    settings['max_workers'] = sanitize_workers(settings.get('max_workers', 5))
    return settings


# --- ΔΩΡΕΑΝ ΛΕΙΤΟΥΡΓΙΑ (YGOPRODeck API - μόνο εικόνα & fallback CM avg) ---
def fetch_from_free_api(card_name):
    try:
        clean_name = card_name.strip()
        encoded_name = requests.utils.quote(clean_name)
        url = f"https://db.ygoprodeck.com/api/v7/cardinfo.php?name={encoded_name}"
        response = requests.get(url, timeout=15)

        cm_avg = 0.0
        img_url = ""

        if response.status_code == 200:
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                card = data["data"][0]
                if "card_images" in card and len(card["card_images"]) > 0:
                    img_url = card["card_images"][0].get("image_url_small", "")

                if "card_prices" in card and len(card["card_prices"]) > 0:
                    prices = card["card_prices"][0]
                    cm_avg = float(prices.get("cardmarket_price", 0.0) or 0.0)

        return 0.0, cm_avg, img_url
    except Exception as e:
        print(f"Σφάλμα Free API για {card_name}: {e}")
        return 0.0, 0.0, ""


# --- CUSTOM API / SCRAPINGBEE ΛΕΙΤΟΥΡΓΙΑ ---
def parse_eu_price(text):
    if not text:
        return 0.0
    # Κρατάμε μόνο τιμές με σύμβολο € ή δεκαδικά/χιλιάδες
    if '€' not in text and not re.search(r'\d+[,\.]\d{2}', text):
        return 0.0
    cleaned = re.sub(r'[^\d,\.]', '', text).strip()
    if not cleaned:
        return 0.0
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def build_custom_request_url(target_cm_url, card, settings):
    api_key = (settings.get('api_key') or '').strip()
    custom_url = (settings.get('custom_url') or '').strip()
    if not custom_url:
        custom_url = 'https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={cm_url}&render_js=true&premium_proxy=true'

    final_url = (
        custom_url
        .replace('{api_key}', requests.utils.quote(api_key))
        .replace('{cm_url}', requests.utils.quote(target_cm_url, safe=''))
        .replace('{name}', requests.utils.quote(card['name']))
        .replace('{set_code}', requests.utils.quote(card['set_code']))
        .replace('{rarity}', requests.utils.quote(card['rarity']))
    )

    if 'scrapingbee.com' in final_url and 'render_js=' not in final_url:
        separator = '&' if '?' in final_url else '?'
        final_url += f"{separator}render_js=false"

    return final_url


def is_greek_seller_element(el):
    if not el:
        return False
    # Ελέγχουμε aria-label, title, data-* και κείμενο για Ελλάδα / Greece
    attrs_blob = " ".join([
        str(el.get('aria-label', '')),
        str(el.get('title', '')),
        str(el.get('data-original-title', '')),
        str(el.get('class', '')),
        el.get_text(" ", strip=True)
    ]).lower()
    return ('greece' in attrs_blob or 'ελλάδα' in attrs_blob or re.search(r'\b(gr|ell)\b', attrs_blob))


def extract_greek_lowest_price(soup):
    greek_prices = []

    # Ψάχνουμε γραμμές προσφορών / containers πωλητών στο Cardmarket
    candidate_rows = soup.select(
        '.article-row, .table-body .row, tr[id^="articleRow"], div[class*="article-row"]'
    )
    if not candidate_rows:
        candidate_rows = soup.find_all('tr')

    for row in candidate_rows:
        has_greek = False
        for tag in row.find_all(['span', 'i', 'svg', 'a', 'div', 'td']):
            if is_greek_seller_element(tag):
                has_greek = True
                break

        if not has_greek:
            continue

        # Μέσα στη γραμμή του Έλληνα πωλητή βρίσκουμε την τιμή σε €
        for price_el in row.find_all(['span', 'div', 'strong', 'td']):
            txt = price_el.get_text(" ", strip=True)
            if '€' in txt and len(txt) <= 25:
                val = parse_eu_price(txt)
                if 0.005 <= val <= 100000:
                    greek_prices.append(val)

    return min(greek_prices) if greek_prices else 0.0


def extract_cardmarket_prices(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    cm_low = 0.0
    cm_avg = 0.0

    for dt in soup.find_all('dt'):
        label = dt.get_text(" ", strip=True).lower()
        dd = dt.find_next_sibling('dd')
        if not dd:
            continue
        val = parse_eu_price(dd.get_text(" ", strip=True))
        if label == 'from' and val > 0:
            cm_low = val
        elif ('price trend' in label or '30-days average' in label) and val > 0 and cm_avg == 0.0:
            cm_avg = val

    gr_low = extract_greek_lowest_price(soup)
    return cm_low, cm_avg, gr_low, soup


def fetch_from_custom_api(card, settings):
    api_key = (settings.get('api_key') or '').strip()
    if not api_key:
        return 0.0, 0.0, 0.0, None

    cm_url = (card['cm_url'] or '').strip()
    resolved_url = None

    try:
        req_url = build_custom_request_url(cm_url, card, settings)
        resp = requests.get(req_url, timeout=90)
        print(f"[Custom API] Card: {card['name']} | Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"[Custom API Error] {resp.text[:250]}")
            return 0.0, 0.0, 0.0, None

        cm_low, cm_avg, gr_low, soup = extract_cardmarket_prices(resp.text)

        if cm_low == 0.0 and cm_avg == 0.0 and '/Products/Search' in cm_url:
            product_link = soup.find('a', href=re.compile(r'/Products/Singles/'))
            if product_link and product_link.get('href'):
                resolved_url = urljoin('https://www.cardmarket.com', product_link['href'])
                req_url_2 = build_custom_request_url(resolved_url, card, settings)
                resp_2 = requests.get(req_url_2, timeout=90)
                if resp_2.status_code == 200:
                    cm_low, cm_avg, gr_low, _ = extract_cardmarket_prices(resp_2.text)

        print(f"Custom API Result -> Low: {cm_low}€, GR Low: {gr_low}€, Avg: {cm_avg}€")
        return cm_low, cm_avg, gr_low, resolved_url
    except Exception as e:
        print(f"Σφάλμα Custom API για {card['name']}: {e}")
        return 0.0, 0.0, 0.0, None


def refresh_single_card(card, settings, use_custom, now_str):
    cid, name = card['id'], card['name']
    try:
        _, free_cm_avg, img_url = fetch_from_free_api(name)
        if not img_url:
            img_url = card.get('image_url') or ''

        cm_low = card.get('cm_low') or 0.0
        gr_low = card.get('gr_low') or 0.0
        cm_avg = free_cm_avg if free_cm_avg > 0 else (card.get('cm_avg') or 0.0)
        updated_url = card['cm_url']

        if use_custom:
            custom_low, custom_avg, custom_gr_low, resolved_url = fetch_from_custom_api(card, settings)
            if custom_low > 0:
                cm_low = custom_low
            if custom_avg > 0:
                cm_avg = custom_avg
            if custom_gr_low > 0:
                gr_low = custom_gr_low
            if resolved_url:
                updated_url = resolved_url

        if cm_low > 0 or cm_avg > 0 or gr_low > 0:
            return {
                'id': cid,
                'cm_low': cm_low,
                'gr_low': gr_low,
                'cm_avg': cm_avg,
                'image_url': img_url,
                'cm_url': updated_url,
                'last_checked': now_str
            }
    except Exception as e:
        print(f"[Refresh Error] Card {name} (id={cid}): {e}")
    return None


def run_batch_refresh(user_id, cards_list, settings):
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    api_key_val = (settings.get('api_key') or '').strip()
    use_custom = (settings.get('provider') == 'custom_api' and bool(api_key_val))
    workers = sanitize_workers(settings.get('max_workers', 5))

    updates = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(refresh_single_card, dict(card), settings, use_custom, now_str)
            for card in cards_list
        ]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                updates.append(res)

    if updates:
        conn = get_db_connection()
        c = conn.cursor()
        for item in updates:
            c.execute('''
                UPDATE cards
                SET cm_low = %s, gr_low = %s, cm_avg = %s, image_url = %s, cm_url = %s, last_checked = %s
                WHERE id = %s AND user_id = %s
            ''', (
                item['cm_low'], item['gr_low'], item['cm_avg'],
                item['image_url'], item['cm_url'], item['last_checked'],
                item['id'], user_id
            ))
        conn.commit()
        conn.close()


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
    max_workers = sanitize_workers(request.form.get('max_workers', 5))

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE api_settings
        SET provider = %s, api_key = %s, custom_url = %s, max_workers = %s
        WHERE user_id = %s
    """, (provider, api_key, custom_url, max_workers, current_user.id))
    conn.commit()
    conn.close()
    flash('Οι ρυθμίσεις API αποθηκεύτηκαν!', 'success')
    return redirect(url_for('index'))


@app.route('/add', methods=['POST'])
@login_required
def add_card():
    name = request.form['name'].strip()
    set_code = request.form['set_code'].strip()
    rarity = request.form['rarity'].strip()
    my_price = float(request.form['my_price'])
    quantity = int(request.form.get('quantity', 1))
    category = request.form.get('category', 'Πώληση')
    alert_percent = float(request.form.get('alert_percent', 15.0))

    cm_url = request.form.get('cm_url', '').strip()
    if not cm_url:
        search_term = name.replace(' ', '+')
        cm_url = f"https://www.cardmarket.com/en/YuGiOh/Products/Search?searchString={search_term}"

    _, _, image_url = fetch_from_free_api(name)

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
    new_name = request.form.get('name', '').strip()
    new_set_code = request.form.get('set_code', '').strip()
    new_price = float(request.form['my_price'])
    new_qty = int(request.form['quantity'])
    new_cat = request.form['category']
    new_rarity = request.form['rarity'].strip()
    new_alert = float(request.form['alert_percent'])
    new_cm_url = request.form.get('cm_url', '').strip()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, set_code, cm_url FROM cards WHERE id = %s AND user_id = %s", (card_id, current_user.id))
    existing = c.fetchone()
    if not existing:
        conn.close()
        return redirect(url_for('index'))

    final_name = new_name if new_name else existing['name']
    final_set_code = new_set_code if new_set_code else existing['set_code']
    final_cm_url = new_cm_url if new_cm_url else existing['cm_url']

    c.execute('''
        UPDATE cards
        SET name = %s, set_code = %s, my_price = %s, quantity = %s,
            category = %s, rarity = %s, alert_percent = %s, cm_url = %s
        WHERE id = %s AND user_id = %s
    ''', (
        final_name, final_set_code, new_price, new_qty,
        new_cat, new_rarity, new_alert, final_cm_url,
        card_id, current_user.id
    ))
    conn.commit()
    conn.close()
    flash('Η κάρτα ενημερώθηκε επιτυχώς.', 'success')
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


@app.route('/refresh_card/<int:card_id>')
@login_required
def refresh_single(card_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM cards WHERE id = %s AND user_id = %s", (card_id, current_user.id))
    card = c.fetchone()
    conn.close()

    if card:
        settings = get_user_api_settings(current_user.id)
        run_batch_refresh(current_user.id, [card], settings)
        flash(f"Ενημερώθηκαν οι τιμές για: {card['name']}", 'info')

    return redirect(url_for('index'))


@app.route('/refresh')
@login_required
def manual_refresh():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM cards WHERE user_id = %s", (current_user.id,))
    cards = c.fetchall()
    conn.close()

    if not cards:
        return redirect(url_for('index'))

    settings = get_user_api_settings(current_user.id)
    user_id = current_user.id

    # Εκτέλεση στο παρασκήνιο ώστε να μη γίνεται ποτέ Gunicorn timeout
    thread = threading.Thread(
        target=run_batch_refresh,
        args=(user_id, [dict(x) for x in cards], settings),
        daemon=True
    )
    thread.start()

    flash(
        f"Ξεκίνησε η ενημέρωση {len(cards)} καρτών στο παρασκήνιο (έως {settings['max_workers']} ταυτόχρονες κλήσεις). Κάνε ανανέωση σε λίγο!",
        'info'
    )
    return redirect(url_for('index'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)