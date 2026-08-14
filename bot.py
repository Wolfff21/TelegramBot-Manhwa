#!/usr/bin/env python3
"""
Manhwa Downloader Bot - Versión estable con Pyppeteer
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
    except Exception as e:
        logger.error(f"Error send_message: {e}")
    return None

def edit_message(chat_id, message_id, text):
    try:
        requests.post(f"{API_URL}/editMessageText", json={
            'chat_id': chat_id,
            'message_id': message_id,
            'text': text
        }, timeout=10)
    except Exception as e:
        logger.error(f"Error edit_message: {e}")

def delete_message(chat_id, message_id):
    try:
        requests.post(f"{API_URL}/deleteMessage", json={
            'chat_id': chat_id,
            'message_id': message_id
        }, timeout=5)
    except Exception as e:
        logger.error(f"Error delete_message: {e}")

# ========== EXTRACCIÓN DE IMÁGENES CON PYPPETEER (CORREGIDO) ==========
def get_image_urls(url):
    """Usa Pyppeteer para obtener todas las URLs de imágenes del capítulo"""
    async def fetch():
        browser = None
        try:
            # CORRECCIÓN: Deshabilitar manejo de señales
            browser = await launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox'],
                handleSIGINT=False,
                handleSIGTERM=False,
                handleSIGHUP=False
            )
            page = await browser.newPage()
            
            # Navegar a la URL
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            # Scroll para cargar todas las imágenes (lazy loading)
            last_height = 0
            for _ in range(20):
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await asyncio.sleep(1.5)
                new_height = await page.evaluate('document.body.scrollHeight')
                if new_height == last_height:
                    break
                last_height = new_height
            
            # Esperar un poco más para que carguen las imágenes
            await asyncio.sleep(2)
            
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
                try:
                    await browser.close()
                except:
                    pass
            return None, None
    
    # Ejecutar el async
    return asyncio.run(fetch())

# ========== DESCARGA Y ZIP ==========
def download_and_zip(image_urls, title):
    clean_title = title.replace(' ', '_').replace('/', '_').replace(':', '')[:50]
    zip_name = f"{clean_title}.zip"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, zip_name)
        
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            total = len(image_urls)
            for idx, img_url in enumerate(image_urls, 1):
                try:
                    resp = requests.get(img_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                    resp.raise_for_status()
                    ext = os.path.splitext(img_url.split('?')[0])[1]
                    if not ext or ext not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                        ext = '.jpg'
                    filename = f"{idx:03d}{ext}"
                    zipf.writestr(filename, resp.content)
                    logger.info(f"📥 Descargada {idx}/{total}")
                except Exception as e:
                    logger.error(f"Error descargando {img_url}: {e}")
                    # Continuar con la siguiente imagen
        
        # Guardar en el directorio temporal
        file_hash = hashlib.md5(f"{zip_name}{time.time()}".encode()).hexdigest()[:8]
        final_name = f"{file_hash}.zip"
        final_path = os.path.join(TEMP_DIR, final_name)
        shutil.copy2(zip_path, final_path)
        
        files_store[final_name] = {
            'path': final_path,
            'expires': time.time() + 3600  # 1 hora
        }
        
        return final_name

# ========== PROCESO DE DESCARGA ==========
def process_download(chat_id, message_id, url):
    try:
        edit_message(chat_id, message_id, "📥 Preparando descarga...")
        
        # Extraer imágenes
        edit_message(chat_id, message_id, "🔍 Extrayendo imágenes del capítulo...")
        title, image_urls = get_image_urls(url)
        
        if not image_urls:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes\nVerifica que el enlace sea correcto")
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, f"📥 Descargando {len(image_urls)} imágenes...")
        
        # Descargar y crear ZIP
        zip_name = download_and_zip(image_urls, title)
        
        # Generar enlace de descarga
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        download_url = f"{base_url}/download/{zip_name}"
        
        # Enviar mensaje con el enlace
        send_message(
            chat_id,
            f"✅ {title}\n"
            f"📦 {len(image_urls)} imágenes\n\n"
            f"🔗 {download_url}\n\n"
            f"⚠️ El enlace expira en 1 hora"
        )
        
        # Limpiar
        delete_message(chat_id, message_id)
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        logger.error(f"Error en process_download: {e}")
        try:
            edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
        except:
            send_message(chat_id, f"❌ Error: {str(e)[:100]}")
        user_sessions.pop(chat_id, None)

# ========== LIMPIEZA AUTOMÁTICA ==========
def cleanup_old_files():
    while True:
        time.sleep(1800)  # Cada 30 minutos
        now = time.time()
        for name, data in list(files_store.items()):
            if data['expires'] < now:
                try:
                    os.remove(data['path'])
                    del files_store[name]
                    logger.info(f"🗑️ Eliminado archivo expirado: {name}")
                except Exception as e:
                    logger.error(f"Error eliminando {name}: {e}")

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

# ========== SERVIDOR WEB ==========
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
                send_message(chat_id, 
                    "📖 Manhwa Downloader Bot\n\n"
                    "Envía el enlace de un capítulo de rncalation.online\n\n"
                    "Ejemplo:\n"
                    "https://rncalation.online/leer/f33b1ede0218dbc131f97"
                )
                return 'ok'
            
            elif text.startswith('http'):
                if chat_id in user_sessions:
                    send_message(chat_id, "⏳ Ya hay una descarga en curso, espera a que termine")
                    return 'ok'
                
                message_id = send_message(chat_id, "⏳ Procesando enlace...")
                if message_id:
                    user_sessions[chat_id] = True
                    # Ejecutar en hilo separado
                    threading.Thread(target=process_download, args=(chat_id, message_id, text)).start()
            else:
                send_message(chat_id, "❌ Envía un enlace que comience con http:// o https://")
    
    return 'ok'

@app.route('/download/<filename>')
def download_file(filename):
    if filename in files_store and files_store[filename]['expires'] > time.time():
        return send_from_directory(TEMP_DIR, filename, as_attachment=True)
    abort(404)

@app.route('/')
def index():
    return "Manhwa Downloader Bot activo", 200

# ========== CONFIGURAR WEBHOOK ==========
def setup_webhook():
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not webhook_url:
        logger.warning("⚠️ RENDER_EXTERNAL_URL no configurado, usando localhost")
        return False
    
    webhook_full = f"{webhook_url}/webhook/{TOKEN}"
    try:
        resp = requests.post(f"{API_URL}/setWebhook", json={'url': webhook_full})
        if resp.ok:
            logger.info(f"✅ Webhook configurado en {webhook_full}")
            return True
        else:
            logger.error(f"❌ Error configurando webhook: {resp.text}")
    except Exception as e:
        logger.error(f"❌ Error configurando webhook: {e}")
    return False

# ========== INICIO ==========
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    
    print("\n" + "="*60)
    print("📖 MANHWA DOWNLOADER BOT")
    print("="*60)
    print("✅ Bot iniciado correctamente")
    print("📌 Soporta capítulos de rncalation.online")
    print("📦 Genera ZIP con todas las imágenes ordenadas")
    print("🕐 Los archivos expiran en 1 hora")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=port)
