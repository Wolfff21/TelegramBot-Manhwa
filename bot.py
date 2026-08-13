#!/usr/bin/env python3
"""
Manhwa Downloader Bot - Simple y directo
"""

import os
import sys
import time
import zipfile
import tempfile
import hashlib
import shutil
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, send_from_directory, abort
import logging

# Configuración básica
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("❌ Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/manhwa_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

# Almacenamiento temporal
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

# ========== DESCARGA DE IMÁGENES ==========
def extract_images(url):
    """Extrae las URLs de las imágenes del capítulo"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except:
        return None, None
    
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Título del capítulo
    title_tag = soup.find('title')
    title = title_tag.text.strip() if title_tag else 'Capitulo'
    
    # Imágenes
    images = []
    for img in soup.select('div.page-img-wrap img.page-img'):
        src = img.get('src')
        if src:
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = 'https://rncalation.online' + src
            images.append(src)
    
    # Si no encuentra con el selector específico, busca todas las imágenes
    if not images:
        for img in soup.find_all('img'):
            src = img.get('src')
            if src and '/uploads/pages/' in src:
                images.append(src)
    
    return title, images

def download_and_zip(images, title):
    """Descarga imágenes y crea ZIP"""
    # Limpiar título para nombre de archivo
    clean_title = title.replace(' ', '_').replace('/', '_')[:50]
    zip_name = f"{clean_title}.zip"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, zip_name)
        
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for idx, img_url in enumerate(images, 1):
                try:
                    resp = requests.get(img_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                    resp.raise_for_status()
                    
                    # Nombre del archivo: 001.jpg, 002.jpg, etc.
                    ext = os.path.splitext(img_url.split('?')[0])[1]
                    if not ext:
                        ext = '.jpg'
                    filename = f"{idx:03d}{ext}"
                    zipf.writestr(filename, resp.content)
                except Exception as e:
                    logger.error(f"Error descargando {img_url}: {e}")
        
        # Guardar en el directorio temporal global
        file_hash = hashlib.md5(f"{zip_name}{time.time()}".encode()).hexdigest()[:8]
        final_name = f"{file_hash}.zip"
        final_path = os.path.join(TEMP_DIR, final_name)
        shutil.copy2(zip_path, final_path)
        
        # Registrar para limpieza (1 hora)
        files_store[final_name] = {
            'path': final_path,
            'expires': time.time() + 3600
        }
        
        return final_name

# ========== PROCESO DE DESCARGA ==========
def process_download(chat_id, message_id, url):
    # Mensaje inicial
    edit_message(chat_id, message_id, "📥 Preparando descarga...")
    
    try:
        # Extraer imágenes
        edit_message(chat_id, message_id, "🔍 Extrayendo imágenes...")
        title, images = extract_images(url)
        
        if not images:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes")
            return
        
        edit_message(chat_id, message_id, f"📥 Descargando {len(images)} imágenes...")
        
        # Descargar y crear ZIP
        zip_name = download_and_zip(images, title)
        
        # Generar enlace
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        download_url = f"{base_url}/download/{zip_name}"
        
        # Enviar mensaje con enlace
        msg = send_message(chat_id, f"✅ {title}\n📦 {len(images)} imágenes\n\n🔗 {download_url}\n\n⚠️ El enlace expira en 1 hora")
        
        # Borrar mensaje de progreso
        delete_message(chat_id, message_id)
        
        # Limpiar sesión
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")

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
                    logger.info(f"🗑️ Eliminado: {name}")
                except:
                    pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

# ========== SERVIDOR WEB ==========
app = Flask(__name__)

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update:
        return 'ok'
    
    # Mensajes
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        
        if 'text' in msg:
            text = msg['text'].strip()
            
            if text == '/start':
                send_message(chat_id, "📖 Envía el enlace de un capítulo de rncalation.online")
                return 'ok'
            
            elif text.startswith('http'):
                # Verificar si ya hay una descarga en curso
                if chat_id in user_sessions:
                    send_message(chat_id, "⏳ Ya hay una descarga en curso")
                    return 'ok'
                
                # Iniciar descarga
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

# ========== CONFIGURAR WEBHOOK ==========
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

# ========== INICIO ==========
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    
    print("\n" + "="*50)
    print("📖 MANHWA DOWNLOADER BOT")
    print("="*50)
    print("✅ Bot iniciado")
    print("📌 URL: rncalation.online")
    print("📦 Genera ZIP con imágenes")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=port)
