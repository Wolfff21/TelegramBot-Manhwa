#!/usr/bin/env python3
"""
Manhwa Downloader Bot - Multisitio
Soporta rncalation.online, olympusxyz.com y más (configurable)
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
import json
import re
import requests
from flask import Flask, request, send_from_directory, abort
from pyppeteer import launch
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))  # Tu ID de Telegram

if not TOKEN:
    print("❌ Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/multisite_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

files_store = {}
user_sessions = {}

# ========== CONFIGURACIÓN DE SITIOS ==========
CONFIG_FILE = 'sites.json'

def load_sites():
    """Carga la configuración de sitios desde JSON"""
    default_sites = {
        "rncalation": {
            "name": "Rncalation",
            "url_pattern": "rncalation.online",
            "image_selector": "div.page-img-wrap img.page-img",
            "title_selector": "title",
            "scroll_selector": "div.page-img-wrap img.page-img",
            "active": True
        },
        "olympus": {
            "name": "OlympusXYZ",
            "url_pattern": "olympusxyz.com",
            "image_selector": "img[alt*=\"Page\"]",
            "title_selector": "title",
            "scroll_selector": "img[alt*=\"Page\"]",
            "active": True
        }
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                sites = json.load(f)
                # Asegurar que los sitios por defecto estén presentes
                for key, value in default_sites.items():
                    if key not in sites:
                        sites[key] = value
                return sites
        except Exception as e:
            logger.error(f"Error cargando sitios: {e}")
            return default_sites
    else:
        # Crear archivo por defecto
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_sites, f, indent=2)
        return default_sites

def save_sites(sites):
    """Guarda la configuración de sitios"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(sites, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error guardando sitios: {e}")
        return False

SITES = load_sites()

def get_site_for_url(url):
    """Detecta qué sitio coincide con la URL"""
    for key, site in SITES.items():
        if not site.get('active', True):
            continue
        if site['url_pattern'] in url:
            return key, site
    return None, None

# ========== FUNCIONES TELEGRAM ==========
def send_message(chat_id, text, keyboard=None):
    try:
        data = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'Markdown'
        }
        if keyboard:
            data['reply_markup'] = keyboard
        resp = requests.post(f"{API_URL}/sendMessage", json=data, timeout=10)
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

def is_admin(chat_id):
    return chat_id == ADMIN_ID

# ========== FORMATEAR NOMBRE ==========
def format_filename(title):
    if not title:
        return "capitulo"
    clean = title.strip()
    clean = clean.replace(' ', '_')
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        clean = clean.replace(char, '')
    clean = re.sub(r'_+', '_', clean)
    if len(clean) > 80:
        clean = clean[:80]
    clean = clean.rstrip('_')
    return clean

# ========== EXTRACCIÓN GENÉRICA ==========
def get_image_urls(url, site_config):
    """Extrae imágenes usando los selectores configurados"""
    async def fetch():
        browser = None
        try:
            browser = await launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox'],
                handleSIGINT=False,
                handleSIGTERM=False,
                handleSIGHUP=False
            )
            page = await browser.newPage()
            
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            # Scroll usando el selector configurado
            scroll_selector = site_config.get('scroll_selector', site_config['image_selector'])
            previous_count = 0
            max_scrolls = 50
            same_count_repeats = 0
            
            for attempt in range(max_scrolls):
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await asyncio.sleep(1.5)
                
                current_count = await page.evaluate(f'''() => {{
                    return document.querySelectorAll('{scroll_selector}').length;
                }}''')
                
                logger.info(f"📊 Imágenes cargadas: {current_count}")
                
                if current_count == previous_count:
                    same_count_repeats += 1
                    if same_count_repeats >= 2:
                        logger.info("✅ Todas las imágenes cargadas")
                        break
                else:
                    same_count_repeats = 0
                    previous_count = current_count
            
            await asyncio.sleep(2)
            
            # Extraer imágenes
            image_selector = site_config['image_selector']
            image_urls = await page.evaluate(f'''() => {{
                return Array.from(document.querySelectorAll('{image_selector}'))
                    .map(img => img.src || img.getAttribute('data-src') || img.getAttribute('data-original'))
                    .filter(src => src && src.startsWith('http'));
            }}''')
            
            # Extraer título
            title_selector = site_config.get('title_selector', 'title')
            title = await page.evaluate(f'''() => {{
                const el = document.querySelector('{title_selector}');
                return el ? el.textContent.trim() : 'Capitulo';
            }}''')
            
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
    
    return asyncio.run(fetch())

# ========== DESCARGA Y ZIP ==========
def download_and_zip(image_urls, title, progress_callback=None):
    clean_title = format_filename(title)
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
                    
                    if idx % 5 == 0 or idx == total:
                        if progress_callback:
                            progress_callback(idx, total)
                    
                    logger.info(f"📥 Descargada {idx}/{total}")
                    
                except Exception as e:
                    logger.error(f"Error descargando {img_url}: {e}")
        
        file_hash = hashlib.md5(f"{zip_name}{time.time()}".encode()).hexdigest()[:8]
        final_name = f"{file_hash}.zip"
        final_path = os.path.join(TEMP_DIR, final_name)
        shutil.copy2(zip_path, final_path)
        
        files_store[final_name] = {
            'path': final_path,
            'expires': time.time() + 3600,
            'original_name': zip_name
        }
        
        return final_name, zip_name

# ========== PROCESO DE DESCARGA ==========
def process_download(chat_id, message_id, url):
    try:
        # Detectar sitio
        site_key, site_config = get_site_for_url(url)
        if not site_config:
            edit_message(chat_id, message_id, 
                "❌ Sitio no soportado\n\n"
                "Sitios disponibles:\n" + 
                "\n".join([f"• {s['name']}" for s in SITES.values() if s.get('active', True)])
            )
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, f"📥 Preparando descarga desde {site_config['name']}...")
        
        # Extraer imágenes
        edit_message(chat_id, message_id, "🔍 Extrayendo imágenes...\n⏳ Esto toma unos segundos")
        title, image_urls = get_image_urls(url, site_config)
        
        if not image_urls:
            edit_message(chat_id, message_id, 
                "❌ No se encontraron imágenes\n"
                "Verifica que el enlace sea correcto o que el selector sea válido"
            )
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, f"📥 Descargando {len(image_urls)} imágenes...")
        
        def update_progress(current, total):
            try:
                edit_message(chat_id, message_id, f"📥 Descargando imágenes...\n🖼️ {current}/{total}")
            except:
                pass
        
        final_name, original_name = download_and_zip(image_urls, title, update_progress)
        
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
        download_url = f"{base_url}/download/{final_name}"
        
        send_message(
            chat_id,
            f"✅ **{title}**\n"
            f"📦 {len(image_urls)} imágenes\n"
            f"📁 `{original_name}`\n\n"
            f"🔗 [Descargar ZIP]({download_url})\n\n"
            f"⚠️ _El enlace expira en 1 hora_"
        )
        
        delete_message(chat_id, message_id)
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        try:
            edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
        except:
            send_message(chat_id, f"❌ Error: {str(e)[:100]}")
        user_sessions.pop(chat_id, None)

# ========== COMANDOS DE ADMIN ==========
def list_sites(chat_id):
    """Lista todos los sitios configurados"""
    text = "📋 **Sitios configurados:**\n\n"
    for key, site in SITES.items():
        status = "✅ Activo" if site.get('active', True) else "❌ Inactivo"
        text += f"**{site['name']}** ({key})\n"
        text += f"  URL: `{site['url_pattern']}`\n"
        text += f"  Selector: `{site['image_selector']}`\n"
        text += f"  Estado: {status}\n\n"
    send_message(chat_id, text)

def add_site(chat_id, args):
    """Agrega un nuevo sitio"""
    try:
        # Formato: /addsite nombre url_pattern image_selector [title_selector] [scroll_selector]
        parts = args.split('|')
        if len(parts) < 3:
            send_message(chat_id, 
                "❌ Formato incorrecto\n\n"
                "Uso:\n"
                "`/addsite nombre | url_pattern | image_selector | title_selector | scroll_selector`\n\n"
                "Ejemplo:\n"
                "`/addsite MiSitio | misitio.com | div.imagen img | title | div.imagen img`"
            )
            return
        
        name = parts[0].strip()
        url_pattern = parts[1].strip()
        image_selector = parts[2].strip()
        title_selector = parts[3].strip() if len(parts) > 3 else 'title'
        scroll_selector = parts[4].strip() if len(parts) > 4 else image_selector
        
        # Generar key única
        key = re.sub(r'[^a-zA-Z0-9]', '_', name.lower())
        base_key = key
        counter = 1
        while key in SITES:
            key = f"{base_key}_{counter}"
            counter += 1
        
        SITES[key] = {
            "name": name,
            "url_pattern": url_pattern,
            "image_selector": image_selector,
            "title_selector": title_selector,
            "scroll_selector": scroll_selector,
            "active": True
        }
        
        if save_sites(SITES):
            send_message(chat_id, 
                f"✅ Sitio **{name}** agregado correctamente\n"
                f"ID: `{key}`\n"
                f"URL: `{url_pattern}`\n"
                f"Selector: `{image_selector}`"
            )
        else:
            send_message(chat_id, "❌ Error al guardar la configuración")
            
    except Exception as e:
        send_message(chat_id, f"❌ Error: {str(e)[:100]}")

def remove_site(chat_id, key):
    """Elimina un sitio"""
    if key not in SITES:
        send_message(chat_id, f"❌ Sitio `{key}` no encontrado")
        return
    
    name = SITES[key]['name']
    del SITES[key]
    
    if save_sites(SITES):
        send_message(chat_id, f"✅ Sitio **{name}** eliminado")
    else:
        send_message(chat_id, "❌ Error al guardar la configuración")

def toggle_site(chat_id, key):
    """Activa/desactiva un sitio"""
    if key not in SITES:
        send_message(chat_id, f"❌ Sitio `{key}` no encontrado")
        return
    
    SITES[key]['active'] = not SITES[key].get('active', True)
    status = "activado" if SITES[key]['active'] else "desactivado"
    
    if save_sites(SITES):
        send_message(chat_id, f"✅ Sitio **{SITES[key]['name']}** {status}")
    else:
        send_message(chat_id, "❌ Error al guardar la configuración")

# ========== LIMPIEZA AUTOMÁTICA ==========
def cleanup_old_files():
    while True:
        time.sleep(1800)
        now = time.time()
        for name, data in list(files_store.items()):
            if data['expires'] < now:
                try:
                    os.remove(data['path'])
                    del files_store[name]
                    logger.info(f"🗑️ Eliminado: {name}")
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
            
            # Comandos del admin
            if is_admin(chat_id):
                if text == '/sites':
                    list_sites(chat_id)
                    return 'ok'
                
                if text.startswith('/addsite'):
                    args = text[8:].strip()
                    if args:
                        add_site(chat_id, args)
                    else:
                        send_message(chat_id, "❌ Especifica los parámetros. Usa: `/addsite nombre | url | selector`")
                    return 'ok'
                
                if text.startswith('/removesite'):
                    key = text[11:].strip()
                    if key:
                        remove_site(chat_id, key)
                    else:
                        send_message(chat_id, "❌ Especifica el ID del sitio. Usa: `/removesite rncalation`")
                    return 'ok'
                
                if text.startswith('/togglesite'):
                    key = text[11:].strip()
                    if key:
                        toggle_site(chat_id, key)
                    else:
                        send_message(chat_id, "❌ Especifica el ID del sitio. Usa: `/togglesite rncalation`")
                    return 'ok'
            
            # Comando start (público)
            if text == '/start':
                sites_list = "\n".join([f"• {s['name']}" for s in SITES.values() if s.get('active', True)])
                send_message(chat_id, 
                    f"📖 **Manhwa Downloader Bot - Multisitio**\n\n"
                    f"Envía el enlace de un capítulo y lo descargaré en ZIP.\n\n"
                    f"**Sitios soportados:**\n{sites_list}\n\n"
                    f"📌 _El archivo se descarga con el nombre del capítulo_\n"
                    f"⏳ _Los enlaces expiran en 1 hora_"
                )
                return 'ok'
            
            # Procesar URL
            elif text.startswith('http'):
                if chat_id in user_sessions:
                    send_message(chat_id, "⏳ Ya hay una descarga en curso")
                    return 'ok'
                
                # Verificar si hay algún sitio que soporte esta URL
                site_key, site_config = get_site_for_url(text)
                if not site_config:
                    send_message(chat_id, 
                        "❌ Sitio no soportado\n\n"
                        "Sitios disponibles:\n" + 
                        "\n".join([f"• {s['name']}" for s in SITES.values() if s.get('active', True)])
                    )
                    return 'ok'
                
                message_id = send_message(chat_id, "⏳ Procesando enlace...")
                if message_id:
                    user_sessions[chat_id] = True
                    threading.Thread(target=process_download, args=(chat_id, message_id, text)).start()
            else:
                send_message(chat_id, "❌ Envía un enlace válido que comience con http:// o https://")
    
    return 'ok'

@app.route('/download/<filename>')
def download_file(filename):
    if filename in files_store and files_store[filename]['expires'] > time.time():
        original_name = files_store[filename].get('original_name', filename)
        return send_from_directory(
            TEMP_DIR, 
            filename, 
            as_attachment=True, 
            download_name=original_name
        )
    abort(404)

@app.route('/')
def index():
    return "Manhwa Downloader Bot - Multisitio activo", 200

def setup_webhook():
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not webhook_url:
        return False
    webhook_full = f"{webhook_url}/webhook/{TOKEN}"
    try:
        resp = requests.post(f"{API_URL}/setWebhook", json={'url': webhook_full})
        if resp.ok:
            logger.info(f"✅ Webhook configurado en {webhook_full}")
            return True
    except Exception as e:
        logger.error(f"Error configurando webhook: {e}")
    return False

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    
    print("\n" + "="*60)
    print("📖 MANHWA DOWNLOADER BOT - MULTISITIO")
    print("="*60)
    print("✅ Bot iniciado")
    print(f"📌 {len(SITES)} sitios configurados")
    for key, site in SITES.items():
        status = "✅" if site.get('active', True) else "❌"
        print(f"   {status} {site['name']} ({key})")
    print("\n📋 Comandos de admin:")
    print("   /sites - Listar sitios")
    print("   /addsite nombre | url_pattern | image_selector | title_selector | scroll_selector")
    print("   /removesite key - Eliminar sitio")
    print("   /togglesite key - Activar/Desactivar sitio")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=port)
