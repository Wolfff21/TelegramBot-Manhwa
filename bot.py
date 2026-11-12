import os
import sys
import time
import asyncio
import tempfile
import threading
import json
import re
import requests
import img2pdf
from flask import Flask, request, send_from_directory, abort
from pyppeteer import launch
from pyrogram import Client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
API_ID = os.environ.get("API_ID")         # <--- NUEVO
API_HASH = os.environ.get("API_HASH")     # <--- NUEVO

if not TOKEN or not API_ID or not API_HASH:
    print("❌ Faltan variables de entorno (TOKEN, API_ID, API_HASH)")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/multisite_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

files_store = {}
user_sessions = {}

# ========== CONFIGURACIÓN DE SITIOS ==========
CONFIG_FILE = 'sites.json'
def load_sites():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        except: return {}
    return {}

def save_sites(sites):
    with open(CONFIG_FILE, 'w') as f: json.dump(sites, f, indent=2)

SITES = load_sites()

def get_site_for_url(url):
    for key, site in SITES.items():
        if not site.get('active', True): continue
        if site['url_pattern'] in url: return key, site
    return None, None

# ========== FUNCIONES TELEGRAM BÁSICAS ==========
def send_message(chat_id, text):
    try:
        requests.post(f"{API_URL}/sendMessage", json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}, timeout=10)
    except: pass

def edit_message(chat_id, message_id, text):
    try:
        requests.post(f"{API_URL}/editMessageText", json={'chat_id': chat_id, 'message_id': message_id, 'text': text}, timeout=10)
    except: pass

# ========== FORMATO Y EXTRACCIÓN ==========
def format_filename(title):
    if not title: return "Capitulo"
    clean = title.strip().replace(' ', '_')
    for char in '<>:"/\\|?*': clean = clean.replace(char, '')
    return re.sub(r'_+', '_', clean)[:80].rstrip('_')

def get_image_urls(url, site_config):
    async def fetch():
        browser = None
        try:
            browser = await launch(headless=True, args=['--no-sandbox'], handleSIGINT=False, handleSIGTERM=False)
            page = await browser.newPage()
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            scroll_selector = site_config.get('scroll_selector', site_config['image_selector'])
            prev_count, repeats = 0, 0
            
            for _ in range(50):
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await asyncio.sleep(1.5)
                count = await page.evaluate(f"document.querySelectorAll('{scroll_selector}').length")
                if count == prev_count:
                    repeats += 1
                    if repeats >= 2: break
                else:
                    repeats = 0
                    prev_count = count
            
            await asyncio.sleep(2)
            img_sel = site_config['image_selector']
            urls = await page.evaluate(f"Array.from(document.querySelectorAll('{img_sel}')).map(img => img.src || img.getAttribute('data-src') || img.getAttribute('data-original')).filter(src => src && src.startsWith('http'))")
            title = await page.title()
            await browser.close()
            return title, urls
        except Exception as e:
            logger.error(f"Error Pyppeteer: {e}")
            if browser: await browser.close()
            return None, None
    return asyncio.run(fetch())

# ========== DESCARGA Y CONVERSIÓN A PDF ==========
def download_and_pdf(image_urls, title, chat_id, message_id):
    clean_title = format_filename(title)
    pdf_name = f"{clean_title}.pdf"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, pdf_name)
        local_images = []
        
        total = len(image_urls)
        for idx, img_url in enumerate(image_urls, 1):
            try:
                resp = requests.get(img_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                resp.raise_for_status()
                ext = os.path.splitext(img_url.split('?')[0])[1] or '.jpg'
                img_path = os.path.join(tmpdir, f"{idx:03d}{ext}")
                with open(img_path, 'wb') as f:
                    f.write(resp.content)
                local_images.append(img_path)
                
                if idx % 5 == 0 or idx == total:
                    edit_message(chat_id, message_id, f"📥 Descargando imágenes...\n🖼️ {idx}/{total}")
            except Exception as e:
                logger.error(f"Error descargando {img_url}: {e}")
        
        # Convertir a PDF
        edit_message(chat_id, message_id, "📄 Convirtiendo a PDF...")
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(local_images))
            
        return pdf_path, pdf_name

# ========== SUBIDA POR PYROGRAM ==========
async def upload_to_telegram(chat_id, pdf_path, pdf_name, title):
    # Inicializamos Pyrogram con el Token del bot y las credenciales API
    async with Client("my_bot_session", api_id=int(API_ID), api_hash=API_HASH, bot_token=TOKEN, in_memory=True) as app:
        await app.send_document(
            chat_id=chat_id,
            document=pdf_path,
            file_name=pdf_name,
            caption=f"✅ **{title}**\n📄 Enviado como PDF\n\n¡Listo para leer!"
        )

# ========== PROCESO DE DESCARGA ==========
def process_download(chat_id, message_id, url):
    try:
        site_key, site_config = get_site_for_url(url)
        if not site_config:
            edit_message(chat_id, message_id, "❌ Sitio no soportado.")
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, f"🔍 Extrayendo imágenes de {site_config['name']}...")
        title, image_urls = get_image_urls(url, site_config)
        
        if not image_urls:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes.")
            user_sessions.pop(chat_id, None)
            return
        
        pdf_path, pdf_name = download_and_pdf(image_urls, title, chat_id, message_id)
        
        edit_message(chat_id, message_id, "☁️ Subiendo a Telegram (sin límites)...")
        
        # Ejecutar Pyrogram en un loop de asyncio
        asyncio.run(upload_to_telegram(chat_id, pdf_path, pdf_name, title))
        
        edit_message(chat_id, message_id, f"✅ **Enviado correctamente**\n📁 {pdf_name}")
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
        user_sessions.pop(chat_id, None)

# ========== SERVIDOR WEB Y COMANDOS ==========
app = Flask(__name__)

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update: return 'ok'
    
    if 'message' in update and 'text' in update['message']:
        msg = update['message']
        chat_id = msg['chat']['id']
        text = msg['text'].strip()
        
        # Comandos Admin
        if chat_id == ADMIN_ID:
            if text == '/sites':
                txt = "📋 *Sitios:*\n"
                for k, s in SITES.items(): txt += f"• {s['name']} (`{k}`)\n"
                send_message(chat_id, txt)
                return 'ok'
            if text.startswith('/addsite'):
                parts = text[8:].strip().split('|')
                if len(parts) >= 3:
                    name, pattern, sel = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    key = re.sub(r'[^a-z0-9]', '_', name.lower())
                    SITES[key] = {"name": name, "url_pattern": pattern, "image_selector": sel, "title_selector": "title", "scroll_selector": sel, "active": True}
                    save_sites(SITES)
                    send_message(chat_id, f"✅ Sitio {name} agregado.")
                return 'ok'

        # Comando Start
        if text == '/start':
            send_message(chat_id, "📖 Envía el enlace de un capítulo. Te lo devolveré en PDF al instante.")
            return 'ok'
        
        # URLs
        if text.startswith('http'):
            if chat_id in user_sessions:
                send_message(chat_id, "⏳ Ya hay una descarga en curso.")
                return 'ok'
            site_key, site_config = get_site_for_url(text)
            if not site_config:
                send_message(chat_id, "❌ Sitio no soportado.")
                return 'ok'
            
            message_id = requests.post(f"{API_URL}/sendMessage", json={'chat_id': chat_id, 'text': '⏳ Procesando...'}).json().get('result', {}).get('message_id')
            if message_id:
                user_sessions[chat_id] = True
                threading.Thread(target=process_download, args=(chat_id, message_id, text)).start()
                
    return 'ok'

@app.route('/')
def index(): return "Bot PDF activo", 200

def setup_webhook():
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if url: requests.post(f"{API_URL}/setWebhook", json={'url': f"{url}/webhook/{TOKEN}"})

if __name__ == '__main__':
    setup_webhook()
    
    # Manejo seguro del puerto para Docker/Render
    port_env = os.environ.get("PORT", "10000")
    if not port_env:
        port_env = "10000"
        
    try:
        port = int(port_env)
    except ValueError:
        port = 10000
        logger.warning("⚠️ Puerto no válido, usando 10000 por defecto")
        
    app.run(host='0.0.0.0', port=port)
