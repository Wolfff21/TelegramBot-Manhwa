#!/usr/bin/env python3
"""
Manhwa Downloader Bot con Pyppeteer (versión optimizada)
"""

import os
import sys
import time
import zipfile
import tempfile
import hashlib
import shutil
import threading
import asyncio
import requests
from flask import Flask, request, send_from_directory, abort
from pyppeteer import launch
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("❌ Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/manhwa_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

files_store = {}
user_sessions = {}

# ========== FUNCIONES TELEGRAM ==========
def send_message(chat_id, text):
    try:
        resp = requests.post(f"{API_URL}/sendMessage", json={
            'chat_id': chat_id,
            'text': text
        }, timeout=10)
        if resp.ok:
            return resp.json().get('result', {}).get('message_id')
    except:
        pass
    return None

def edit_message(chat_id, message_id, text):
    try:
        requests.post(f"{API_URL}/editMessageText", json={
            'chat_id': chat_id,
            'message_id': message_id,
            'text': text
        }, timeout=10)
    except:
        pass

def delete_message(chat_id, message_id):
    try:
        requests.post(f"{API_URL}/deleteMessage", json={
            'chat_id': chat_id,
            'message_id': message_id
        }, timeout=5)
    except:
        pass

# ========== EXTRACCIÓN DE IMÁGENES CON PYPPETEER ==========
def get_image_urls(url):
    """Usa Pyppeteer para obtener todas las URLs de imágenes del capítulo"""
    async def fetch():
        browser = None
        try:
            browser = await launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = await browser.newPage()
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            # Scroll para cargar todas las imágenes
            last_height = 0
            for _ in range(20):
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await asyncio.sleep(1.5)
                new_height = await page.evaluate('document.body.scrollHeight')
                if new_height == last_height:
                    break
                last_height = new_height
            
            # Extraer todas las imágenes
            image_urls = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('div.page-img-wrap img.page-img'))
                    .map(img => img.src)
                    .filter(src => src && src.startsWith('http'));
            }''')
            
            title = await page.title()
            await browser.close()
            return title, image_urls
        except Exception as e:
            logger.error(f"Error en Pyppeteer: {e}")
            if browser:
                await browser.close()
            return None, None
    
    return asyncio.run(fetch())

# ========== DESCARGA Y ZIP ==========
def download_and_zip(image_urls, title):
    clean_title = title.replace(' ', '_').replace('/', '_')[:50]
    zip_name = f"{clean_title}.zip"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, zip_name)
        
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for idx, img_url in enumerate(image_urls, 1):
                try:
                    resp = requests.get(img_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                    resp.raise_for_status()
                    ext = os.path.splitext(img_url.split('?')[0])[1]
                    if not ext:
                        ext = '.jpg'
                    filename = f"{idx:03d}{ext}"
                    zipf.writestr(filename, resp.content)
                except Exception as e:
                    logger.error(f"Error descargando {img_url}: {e}")
        
        file_hash = hashlib.md5(f"{zip_name}{time.time()}".encode()).hexdigest()[:8]
        final_name = f"{file_hash}.zip"
        final_path = os.path.join(TEMP_DIR, final_name)
        shutil.copy2(zip_path, final_path)
        
        files_store[final_name] = {
            'path': final_path,
            'expires': time.time() + 3600
        }
        
        return final_name

# ========== PROCESO ==========
def process_download(chat_id, message_id, url):
    edit_message(chat_id, message_id, "📥 Preparando...")
    
    try:
        edit_message(chat_id, message_id, "🔍 Extrayendo imágenes...")
        title, image_urls = get_image_urls(url)
        
        if not image_urls:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes")
            return
        
        edit_message(chat_id, message_id, f"📥 Descargando {len(image_urls)} imágenes...")
        
        zip_name = download_and_zip(image_urls, title)
        
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        download_url = f"{base_url}/download/{zip_name}"
        
        send_message(chat_id, f"✅ {title}\n📦 {len(image_urls)} imágenes\n\n🔗 {download_url}\n\n⚠️ El enlace expira en 1 hora")
        delete_message(chat_id, message_id)
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")

# ========== LIMPIEZA ==========
def cleanup_old_files():
    while True:
        time.sleep(1800)
        now = time.time()
        for name, data in list(files_store.items()):
            if data['expires'] < now:
                try:
                    os.remove(data['path'])
                    del files_store[name]
                except:
                    pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

# ========== SERVIDOR ==========
app = Flask(__name__)

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update:
        return 'ok'
    
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        
        if 'text' in msg:
            text = msg['text'].strip()
            
            if text == '/start':
                send_message(chat_id, "📖 Envía el enlace de un capítulo de rncalation.online")
                return 'ok'
            
            elif text.startswith('http'):
                if chat_id in user_sessions:
                    send_message(chat_id, "⏳ Ya hay una descarga en curso")
                    return 'ok'
                
                message_id = send_message(chat_id, "⏳ Procesando...")
                if message_id:
                    user_sessions[chat_id] = True
                    threading.Thread(target=process_download, args=(chat_id, message_id, text)).start()
            else:
                send_message(chat_id, "❌ Envía un enlace válido")
    
    return 'ok'

@app.route('/download/<filename>')
def download_file(filename):
    if filename in files_store and files_store[filename]['expires'] > time.time():
        return send_from_directory(TEMP_DIR, filename, as_attachment=True)
    abort(404)

@app.route('/')
def index():
    return "Manhwa Downloader Bot activo", 200

def setup_webhook():
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not webhook_url:
        return False
    webhook_full = f"{webhook_url}/webhook/{TOKEN}"
    try:
        resp = requests.post(f"{API_URL}/setWebhook", json={'url': webhook_full})
        if resp.ok:
            logger.info("✅ Webhook configurado")
            return True
    except:
        pass
    return False

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    
    print("\n" + "="*50)
    print("📖 MANHWA DOWNLOADER BOT (Pyppeteer)")
    print("="*50)
    print("✅ Bot iniciado")
    print("📌 Soporta capítulos de rncalation.online")
    print("📦 Genera ZIP con todas las imágenes")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=port)
