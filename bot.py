#!/usr/bin/env python3
import os
import sys
import time
import asyncio
import tempfile
import threading
import json
import re
import io
import requests
import img2pdf
from flask import Flask, request
from pyppeteer import launch
from pyrogram import Client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

if not TOKEN or not API_ID or not API_HASH:
    logger.error("❌ Faltan variables de entorno")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/multisite_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

user_sessions = {}

# ========== GESTIÓN DE CONFIGURACIÓN Y BACKUPS ==========
CONFIG_FILE = 'sites.json'
DEFAULT_CONFIG = {
    "sites": {},
    "settings": {
        "auto_backup_hours": 0, # 0 = desactivado
        "last_backup": 0
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        except: pass
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: json.dump(config, f, indent=2)

CONFIG = load_config()

# ========== FUNCIONES TELEGRAM ==========
def send_message(chat_id, text, reply_markup=None):
    try:
        data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
        if reply_markup: data['reply_markup'] = reply_markup
        requests.post(f"{API_URL}/sendMessage", json=data, timeout=10)
    except Exception as e: logger.error(f"Error enviando mensaje: {e}")

def edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        data = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown'}
        if reply_markup: data['reply_markup'] = reply_markup
        requests.post(f"{API_URL}/editMessageText", json=data, timeout=10)
    except Exception as e: logger.error(f"Error editando mensaje: {e}")

def answer_callback(callback_id):
    try: requests.post(f"{API_URL}/answerCallbackQuery", json={'callback_query_id': callback_id}, timeout=5)
    except: pass

def format_filename(title):
    if not title: return "Capitulo"
    clean = title.strip().replace(' ', '_')
    for char in '<>:"/\\|?*': clean = clean.replace(char, '')
    return re.sub(r'_+', '_', clean)[:80].rstrip('_')

# ========== EXTRACCIÓN Y DESCARGA ==========
def get_image_urls(url, site_config):
    async def fetch():
        browser = None
        try:
            browser = await launch(
                headless=True, 
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
                handleSIGINT=False, handleSIGTERM=False
            )
            page = await browser.newPage()
            await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            await page.setViewport({'width': 1920, 'height': 1080})
            
            logger.info(f"🌐 Navegando a {url}")
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            scroll_selector = site_config.get('scroll_selector', site_config['image_selector'])
            prev_count, repeats = 0, 0
            
            # Scroll robusto
            for i in range(30):
                await page.evaluate('window.scrollBy(0, window.innerHeight)')
                await asyncio.sleep(1.0)
                count = await page.evaluate(f"document.querySelectorAll(`{scroll_selector}`).length")
                logger.info(f"   Scroll {i+1}: {count} imágenes")
                
                if count == prev_count:
                    repeats += 1
                    if repeats >= 3: break
                else:
                    repeats = 0
                    prev_count = count
            
            await asyncio.sleep(2)
            
            img_sel = site_config['image_selector']
            urls = await page.evaluate(f"""
                Array.from(document.querySelectorAll(`{img_sel}`))
                    .map(img => img.src || img.getAttribute('data-src') || img.getAttribute('data-original'))
                    .filter(src => src && src.startsWith('http'))
            """)
            
            title = await page.title()
            await browser.close()
            return title, urls
        except Exception as e:
            logger.error(f"❌ Error Pyppeteer: {e}")
            if browser: await browser.close()
            return None, None
    return asyncio.run(fetch())

def download_and_pdf(image_urls, title, chat_id, message_id):
    clean_title = format_filename(title)
    pdf_name = f"{clean_title}.pdf"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, pdf_name)
        local_images = []
        
        for idx, img_url in enumerate(image_urls, 1):
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                resp = requests.get(img_url, headers=headers, timeout=30)
                resp.raise_for_status()
                ext = os.path.splitext(img_url.split('?')[0])[1] or '.jpg'
                img_path = os.path.join(tmpdir, f"{idx:03d}{ext}")
                with open(img_path, 'wb') as f: f.write(resp.content)
                local_images.append(img_path)
                
                if idx % 5 == 0 or idx == len(image_urls):
                    edit_message(chat_id, message_id, f"📥 Descargando imágenes...\n🖼️ {idx}/{len(image_urls)}")
            except Exception as e:
                logger.error(f"Error descargando {img_url}: {e}")
        
        if not local_images: return None, None
            
        edit_message(chat_id, message_id, "📄 Convirtiendo a PDF...")
        with open(pdf_path, "wb") as f: f.write(img2pdf.convert(local_images))
        return pdf_path, pdf_name

# ========== PYROGRAM (Envíos y Descargas) ==========
async def send_file_pyrogram(chat_id, file_path, file_name, caption):
    async with Client("my_bot_session", api_id=int(API_ID), api_hash=API_HASH, bot_token=TOKEN, in_memory=True) as app:
        await app.send_document(chat_id=chat_id, document=file_path, file_name=file_name, caption=caption)

def process_download(chat_id, message_id, url):
    try:
        site_key, site_config = None, None
        for k, s in CONFIG['sites'].items():
            if s['url_pattern'] in url:
                site_key, site_config = k, s
                break
                
        if not site_config:
            edit_message(chat_id, message_id, "❌ Sitio no soportado.")
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, f"🔍 Extrayendo de {site_config['name']}...")
        title, image_urls = get_image_urls(url, site_config)
        
        if not image_urls:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes.")
            user_sessions.pop(chat_id, None)
            return
        
        pdf_path, pdf_name = download_and_pdf(image_urls, title, chat_id, message_id)
        
        if not pdf_path:
            edit_message(chat_id, message_id, "❌ Error descargando imágenes.")
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, "☁️ Subiendo a Telegram (sin límites)...")
        asyncio.run(send_file_pyrogram(chat_id, pdf_path, pdf_name, f"✅ **{title}**\n📄 Enviado como PDF"))
        
        edit_message(chat_id, message_id, f"✅ **Enviado**\n📁 {pdf_name}")
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        logger.error(f"Error en proceso: {e}")
        edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
        user_sessions.pop(chat_id, None)

# ========== SISTEMA DE BACKUP ==========
def do_backup():
    global CONFIG
    CONFIG['settings']['last_backup'] = time.time()
    save_config(CONFIG)
    
    json_str = json.dumps(CONFIG, indent=2)
    tmp_path = os.path.join(TEMP_DIR, 'backup_config.json')
    with open(tmp_path, 'w') as f: f.write(json_str)
    
    try:
        asyncio.run(send_file_pyrogram(
            ADMIN_ID, tmp_path, 'backup_config.json', 
            "💾 **Backup Automático**\nGuarda este archivo. Si el bot se reinicia, envíalo de vuelta para restaurar los sitios."
        ))
        logger.info("Backup enviado al admin.")
    except Exception as e:
        logger.error(f"Error enviando backup: {e}")

def backup_worker():
    while True:
        time.sleep(300) # Revisar cada 5 minutos
        hours = CONFIG['settings'].get('auto_backup_hours', 0)
        if hours > 0:
            elapsed = time.time() - CONFIG['settings'].get('last_backup', 0)
            if elapsed >= hours * 3600:
                logger.info("Ejecutando backup automático...")
                do_backup()

# Iniciar hilo de backup
threading.Thread(target=backup_worker, daemon=True).start()

# ========== SERVIDOR WEB Y COMANDOS ==========
app = Flask(__name__)

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    global CONFIG
    update = request.json
    if not update: return 'ok'
    
    # --- MANEJO DE DOCUMENTOS (RESTAURAR BACKUP) ---
    if 'message' in update and 'document' in update['message']:
        msg = update['message']
        chat_id = msg['chat']['id']
        if chat_id == ADMIN_ID:
            doc = msg['document']
            if doc['file_name'] == 'backup_config.json':
                file_id = doc['file_id']
                file_info = requests.get(f"{API_URL}/getFile?file_id={file_id}").json()
                file_path = file_info['result']['file_path']
                file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                
                content = requests.get(file_url).text
                try:
                    new_config = json.loads(content)
                    if "sites" in new_config:
                        CONFIG = new_config
                        save_config(CONFIG)
                        send_message(chat_id, "✅ **Configuración restaurada correctamente.**")
                    else:
                        send_message(chat_id, "❌ El archivo no tiene el formato correcto.")
                except Exception as e:
                    send_message(chat_id, f"❌ Error al leer el archivo: {e}")
        return 'ok'
    
    # --- MANEJO DE BOTONES (CALLBACKS) ---
    if 'callback_query' in update:
        cb = update['callback_query']
        chat_id = cb['message']['chat']['id']
        message_id = cb['message']['message_id']
        data = cb['data']
        answer_callback(cb['id'])
        
        if chat_id != ADMIN_ID: return 'ok'
        
        if data == 'admin_panel':
            kb = {'inline_keyboard': [
                [{'text': '📥 Hacer Backup Manual', 'callback_data': 'backup_manual'}],
                [{'text': f"⏱️ Auto-Backup: {CONFIG['settings']['auto_backup_hours']}h", 'callback_data': 'backup_menu'}]
            ]}
            edit_message(chat_id, message_id, "⚙️ **Panel de Administrador**\n\nAquí puedes gestionar los backups de tu bot.", kb)
            
        elif data == 'backup_menu':
            kb = {'inline_keyboard': [
                [{'text': '2 Horas', 'callback_data': 'auto_2'}, {'text': '8 Horas', 'callback_data': 'auto_8'}],
                [{'text': '16 Horas', 'callback_data': 'auto_16'}, {'text': '24 Horas', 'callback_data': 'auto_24'}],
                [{'text': 'Desactivar', 'callback_data': 'auto_0'}],
                [{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]
            ]}
            edit_message(chat_id, message_id, "⏱️ **Configurar Backup Automático**\n\n¿Cada cuánto tiempo quieres que te envíe el archivo de configuración?", kb)
            
        elif data.startswith('auto_'):
            hours = int(data.split('_')[1])
            CONFIG['settings']['auto_backup_hours'] = hours
            save_config(CONFIG)
            send_message(chat_id, f"✅ Backup automático configurado a {hours} horas." if hours > 0 else "✅ Backup automático desactivado.")
            edit_message(chat_id, message_id, "⚙️ **Panel de Administrador**\n\nConfiguración guardada.")
            
        elif data == 'backup_manual':
            send_message(chat_id, "⏳ Generando backup...")
            do_backup()
        return 'ok'

    # --- MANEJO DE TEXTO ---
    if 'message' in update and 'text' in update['message']:
        msg = update['message']
        chat_id = msg['chat']['id']
        text = msg['text'].strip()
        
        # Comandos de Admin
        if chat_id == ADMIN_ID:
            if text == '/admin':
                kb = {'inline_keyboard': [[{'text': '⚙️ Abrir Panel', 'callback_data': 'admin_panel'}]]}
                send_message(chat_id, "👑 **Bienvenido, Admin.**", kb)
                return 'ok'
                
            if text.startswith('/addsite'):
                parts = text[8:].strip().split('|')
                if len(parts) >= 3:
                    name, pattern, sel = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    key = re.sub(r'[^a-z0-9]', '_', name.lower())
                    CONFIG['sites'][key] = {
                        "name": name, "url_pattern": pattern, 
                        "image_selector": sel, "title_selector": "title", "scroll_selector": sel, "active": True
                    }
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Sitio **{name}** agregado.")
                return 'ok'
                
            if text == '/sites':
                txt = "📋 *Sitios configurados:*\n"
                for k, s in CONFIG['sites'].items(): txt += f"• {s['name']} (`{k}`)\n"
                if not CONFIG['sites']: txt += "_No hay sitios configurados._"
                send_message(chat_id, txt)
                return 'ok'

        # Comandos Públicos
        if text == '/start':
            send_message(chat_id, "📖 Envía un enlace de capítulo y te lo devolveré en PDF.")
            return 'ok'
        
        if text.startswith('http'):
            if chat_id in user_sessions:
                send_message(chat_id, "⏳ Ya hay una descarga en curso.")
                return 'ok'
            
            site_key = None
            for k, s in CONFIG['sites'].items():
                if s['url_pattern'] in text:
                    site_key = k; break
                    
            if not site_key:
                send_message(chat_id, "❌ Sitio no soportado.")
                return 'ok'
            
            res = requests.post(f"{API_URL}/sendMessage", json={'chat_id': chat_id, 'text': '⏳ Procesando...'})
            message_id = res.json().get('result', {}).get('message_id')
            
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
    port_env = os.environ.get("PORT", "10000") or "10000"
    port = int(port_env) if port_env.isdigit() else 10000
    logger.info(f"🚀 Bot iniciado en puerto {port}")
    app.run(host='0.0.0.0', port=port)
