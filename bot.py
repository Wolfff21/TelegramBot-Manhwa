#!/usr/bin/env python3
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
import datetime
import unicodedata
from io import BytesIO
from PIL import Image
from flask import Flask, request
from pyppeteer import launch
from pyppeteer_stealth import stealth
from pyrogram import Client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
GROUP_ID = int(os.environ.get("GROUP_ID", 0))
GROUP_LINK = os.environ.get("GROUP_LINK", "")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

if not TOKEN or not API_ID or not API_HASH or not ADMIN_ID:
    logger.error("❌ Faltan variables de entorno críticas")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/multisite_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

user_sessions = {}
user_cooldowns = {}

# ========== GESTIÓN DE CONFIGURACIÓN ==========
CONFIG_FILE = 'sites.json'
DEFAULT_CONFIG = {
    "sites": {},
    "settings": {
        "auto_backup_hours": 0, "last_backup": 0, "global_maintenance": False,
        "auto_repair_enabled": False, "auto_repair_time": "03:00", "last_auto_repair_date": "",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "max_images": 0, # 0 = sin límite
        "download_delay": 0.5, # Segundos entre imágenes
        "proxy": "" # Opcional (ej: http://ip:puerto)
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: 
                conf = json.load(f)
                if "settings" not in conf: conf["settings"] = DEFAULT_CONFIG["settings"]
                for k, v in DEFAULT_CONFIG["settings"].items():
                    if k not in conf["settings"]: conf["settings"][k] = v
                return conf
        except: pass
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: json.dump(config, f, indent=2)

CONFIG = load_config()

# ========== FUNCIONES TELEGRAM ==========
def send_message(chat_id, text, reply_markup=None):
    try:
        data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': True}
        if reply_markup: data['reply_markup'] = reply_markup
        requests.post(f"{API_URL}/sendMessage", json=data, timeout=10)
    except Exception as e: logger.error(f"Error enviando mensaje: {e}")

def edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        data = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': True}
        if reply_markup: data['reply_markup'] = reply_markup
        requests.post(f"{API_URL}/editMessageText", json=data, timeout=10)
    except Exception as e: logger.error(f"Error editando mensaje: {e}")

def answer_callback(callback_id):
    try: requests.post(f"{API_URL}/answerCallbackQuery", json={'callback_query_id': callback_id}, timeout=5)
    except: pass

def format_filename(title):
    if not title: return "Capitulo"
    # Limpiar acentos y caracteres raros para el nombre visible
    clean = unicodedata.normalize('NFKD', title).encode('ASCII', 'ignore').decode('utf-8')
    clean = clean.strip().replace(' ', '_')
    for char in '<>:"/\\|?*': 
        clean = clean.replace(char, '')
    return re.sub(r'_+', '_', clean)[:60].rstrip('_')

# ========== EXTRACCIÓN Y DESCARGA ==========
def get_image_urls(url, site_config):
    async def fetch():
        browser = None
        try:
            launch_args = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            if CONFIG['settings'].get('proxy'):
                launch_args.append(f"--proxy-server={CONFIG['settings']['proxy']}")

            browser = await launch(
                headless=True, args=launch_args,
                handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False
            )
            page = await browser.newPage()
            await stealth(page)
            
            ua = CONFIG['settings'].get('user_agent')
            await page.setUserAgent(ua)
            await page.setViewport({'width': 1920, 'height': 1080})
            
            logger.info(f"🌐 Navegando a {url}")
            try:
                await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            except Exception as e:
                logger.warning(f"Timeout de red, continuando por si acaso... {e}")

            # Detección de Cloudflare
            title = await page.title()
            if "Just a moment" in title or "Cloudflare" in title or "Attention Required" in title:
                await browser.close()
                return title, [], 'blocked'

            scroll_selector = site_config.get('scroll_selector', site_config['image_selector'])
            prev_count, repeats = 0, 0
            
            for i in range(30):
                await page.evaluate('window.scrollBy(0, window.innerHeight)')
                await asyncio.sleep(1.0)
                count = await page.evaluate(f"() => {{ return document.querySelectorAll(`{scroll_selector}`).length; }}")
                
                if count == prev_count:
                    repeats += 1
                    if repeats >= 3: break
                else:
                    repeats = 0
                    prev_count = count
            
            await asyncio.sleep(2)
            img_sel = site_config['image_selector']
            urls = await page.evaluate(f"""() => {{
                return Array.from(document.querySelectorAll(`{img_sel}`))
                    .map(img => img.src || img.getAttribute('data-src') || img.getAttribute('data-original'))
                    .filter(src => src && src.startsWith('http'));
            }}""")
            
            title = await page.title()
            await browser.close()
            return title, urls, 'active'
        except Exception as e:
            logger.error(f"❌ Error Pyppeteer: {e}")
            if browser: await browser.close()
            return None, None, 'error'
    return asyncio.run(fetch())

# ========== COMANDO DEBUG ==========
def debug_url(chat_id, url):
    async def fetch_debug():
        browser = None
        try:
            browser = await launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'], handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False)
            page = await browser.newPage()
            await stealth(page)
            await page.setUserAgent(CONFIG['settings'].get('user_agent'))
            await page.setViewport({'width': 1920, 'height': 1080})
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            screenshot_path = os.path.join(TEMP_DIR, 'debug.png')
            await page.screenshot({'path': screenshot_path, 'fullPage': False})
            html_path = os.path.join(TEMP_DIR, 'debug.html')
            html_content = await page.content()
            with open(html_path, 'w', encoding='utf-8') as f: f.write(html_content)
                
            await browser.close()
            
            asyncio.run(send_file_pyrogram(chat_id, screenshot_path, 'debug.png', "📸 Captura de lo que ve el bot."))
            asyncio.run(send_file_pyrogram(chat_id, html_path, 'debug.html', "📄 HTML de la página para revisar selectores."))
            send_message(chat_id, "✅ Debug completado. Revisa la captura. Si ves un logo de Cloudflare, estamos bloqueados.")
            
        except Exception as e:
            logger.error(f"Error en debug: {e}")
            send_message(chat_id, f"❌ Error en debug: {str(e)[:100]}")
            if browser: await browser.close()
    
    asyncio.run(fetch_debug())

def download_and_pdf(image_urls, title, chat_id, message_id):
    clean_title = format_filename(title)
    pdf_name = f"{clean_title}.pdf" 
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Guardamos con un nombre seguro en el disco para evitar errores de Pyrogram
        safe_disk_name = "documento_temporal.pdf" 
        pdf_path = os.path.join(tmpdir, safe_disk_name)
        
        local_images = []
        max_images = CONFIG['settings'].get('max_images', 0)
        delay = CONFIG['settings'].get('download_delay', 0.5)
        
        total = len(image_urls)
        if max_images > 0 and total > max_images:
            total = max_images
            image_urls = image_urls[:max_images]

        for idx, img_url in enumerate(image_urls, 1):
            try:
                headers = {'User-Agent': CONFIG['settings'].get('user_agent')}
                resp = requests.get(img_url, headers=headers, timeout=30)
                resp.raise_for_status()
                
                try:
                    img = Image.open(BytesIO(resp.content))
                    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                    img_path = os.path.join(tmpdir, f"{idx:03d}.jpg")
                    img.save(img_path, "JPEG")
                    local_images.append(img_path)
                except Exception as img_err:
                    logger.error(f"Error convirtiendo imagen {idx}: {img_err}")
                    continue

                if idx % 5 == 0 or idx == total:
                    edit_message(chat_id, message_id, f"📥 Descargando imágenes...\n🖼️ {idx}/{total}")
                
                time.sleep(delay)
            except Exception as e: 
                logger.error(f"Error descargando {img_url}: {e}")
        
        if not local_images: return None, None
            
        edit_message(chat_id, message_id, "📄 Convirtiendo a PDF...")
        try:
            with open(pdf_path, "wb") as f: 
                f.write(img2pdf.convert(local_images))
            return pdf_path, pdf_name 
        except Exception as e:
            logger.error(f"❌ Error creando PDF: {e}")
            return None, None

async def send_file_pyrogram(chat_id, file_path, file_name, caption):
    async with Client("my_bot_session", api_id=int(API_ID), api_hash=API_HASH, bot_token=TOKEN, in_memory=True) as app:
        await app.send_document(chat_id=chat_id, document=file_path, file_name=file_name, caption=caption)

def process_download(chat_id, message_id, url):
    try:
        if CONFIG['settings'].get('global_maintenance', False):
            edit_message(chat_id, message_id, "🚧 **Bot en Mantenimiento Global**\n\nIntenta más tarde.")
            user_sessions.pop(chat_id, None); return

        site_key, site_config = None, None
        for k, s in CONFIG['sites'].items():
            if s['url_pattern'] in url:
                site_key, site_config = k, s; break
                
        if not site_config:
            edit_message(chat_id, message_id, "❌ Sitio no soportado.")
            user_sessions.pop(chat_id, None); return

        if site_config.get('status') == 'maintenance':
            edit_message(chat_id, message_id, f"🚧 **{site_config['name']} en mantenimiento**")
            user_sessions.pop(chat_id, None); return
        
        edit_message(chat_id, message_id, f"🔍 Extrayendo de {site_config['name']}...")
        title, image_urls, status = get_image_urls(url, site_config)
        
        if status == 'blocked':
            edit_message(chat_id, message_id, "🟠 **Bloqueado por Cloudflare**\nEl sitio está protegiendo sus imágenes contra bots.")
            CONFIG['sites'][site_key]['status'] = 'blocked'
            save_config(CONFIG)
            send_message(ADMIN_ID, f"🟠 **Sitio Bloqueado**\n**{site_config['name']}** ha bloqueado al bot con Cloudflare.")
            user_sessions.pop(chat_id, None); return

        if not image_urls:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes.\n\n⚠️ *El administrador ha sido notificado.*")
            CONFIG['sites'][site_key]['status'] = 'error'
            save_config(CONFIG)
            send_message(ADMIN_ID, f"⚠️ **Fallo detectado**\nEl sitio **{site_config['name']}** no devolvió imágenes.\nUsa `/debug {url}`.")
            user_sessions.pop(chat_id, None); return
        
        pdf_path, pdf_name = download_and_pdf(image_urls, title, chat_id, message_id)
        if not pdf_path:
            edit_message(chat_id, message_id, "❌ Error creando el PDF.")
            user_sessions.pop(chat_id, None); return
        
        edit_message(chat_id, message_id, "☁️ Subiendo a Telegram...")
        try:
            asyncio.run(send_file_pyrogram(chat_id, pdf_path, pdf_name, f"✅ **{title}**\n📄 Enviado como PDF"))
            edit_message(chat_id, message_id, f"✅ **Enviado**\n📁 {pdf_name}")
        except Exception as py_err:
            logger.error(f"❌ Error en Pyrogram: {py_err}")
            edit_message(chat_id, message_id, f"❌ Fallo al subir: {str(py_err)[:100]}")
            
        user_sessions.pop(chat_id, None)
    except Exception as e:
        logger.error(f"Error general: {e}")
        user_sessions.pop(chat_id, None)

# ========== MOTOR DE AUTO-REPARACIÓN ==========
def discover_best_selector(url):
    async def fetch():
        browser = None
        try:
            browser = await launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'], handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False)
            page = await browser.newPage()
            await stealth(page)
            await page.setUserAgent(CONFIG['settings'].get('user_agent'))
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            for _ in range(5):
                await page.evaluate('window.scrollBy(0, window.innerHeight)')
                await asyncio.sleep(1)
            img_data = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('img')).map(img => {
                    return { src: img.src || img.getAttribute('data-src') || '', class: img.className || '', parentClass: img.parentElement ? img.parentElement.className : '' }
                }).filter(x => x.src.startsWith('http'));
            }''')
            await browser.close()
            return img_data
        except: 
            if browser: await browser.close()
            return []
    imgs = asyncio.run(fetch())
    if not imgs: return None
    candidates = {}
    for img in imgs:
        src = img['src'].lower()
        cls = img['class']
        parent = img['parentClass']
        if any(bad in src for bad in ['logo', 'banner', 'icon', 'avatar', 'placeholder', 'button']): continue
        score = 0
        if any(good in src for good in ['/uploads/', '/chapters/', '/storage/', '/manga/', '/reader/', '.jpg', '.webp', '.png']): score += 10
        if any(good in parent.lower() for good in ['reading', 'chapter', 'content', 'viewer', 'page', 'reader']):
            score += 20
            candidates[f"div.{parent.split()[0]} img"] = candidates.get(f"div.{parent.split()[0]} img", 0) + score
        if cls:
            score += 10
            candidates[f"img.{cls.split()[0]}"] = candidates.get(f"img.{cls.split()[0]}", 0) + score
        else:
            candidates["img"] = candidates.get("img", 0) + score
    if not candidates: return None
    return max(candidates, key=candidates.get)

def run_auto_repair():
    CONFIG['settings']['global_maintenance'] = True
    save_config(CONFIG)
    reporte = "🤖 **Reporte de Auto-Reparación**\n\n"
    cambios = 0
    for key, site in CONFIG['sites'].items():
        test_url = site.get('test_url')
        if not test_url:
            reporte += f"⚠️ **{site['name']}**: Sin test_url.\n"
            continue
        old_sel = site['image_selector']
        new_sel = discover_best_selector(test_url)
        if new_sel and new_sel != old_sel:
            site['image_selector'] = new_sel
            site['scroll_selector'] = new_sel
            site['status'] = 'active'
            cambios += 1
            reporte += f"✅ **{site['name']}**: Actualizado a `{new_sel}`\n"
        elif new_sel == old_sel:
            reporte += f"🟢 **{site['name']}**: Ya era óptimo.\n"
        else:
            reporte += f"❌ **{site['name']}**: Fallo.\n"
    save_config(CONFIG)
    CONFIG['settings']['global_maintenance'] = False
    save_config(CONFIG)
    send_message(ADMIN_ID, reporte + f"\n🏁 Se actualizaron {cambios} sitios.")

def scheduler_worker():
    while True:
        time.sleep(60)
        if not CONFIG['settings'].get('auto_repair_enabled', False): continue
        now = datetime.datetime.now()
        if now.strftime("%H:%M") == CONFIG['settings'].get('auto_repair_time') and now.strftime("%Y-%m-%d") != CONFIG['settings'].get('last_auto_repair_date'):
            CONFIG['settings']['last_auto_repair_date'] = now.strftime("%Y-%m-%d")
            save_config(CONFIG)
            threading.Thread(target=run_auto_repair).start()

threading.Thread(target=scheduler_worker, daemon=True).start()

# ========== SISTEMA DE BACKUP ==========
def do_backup():
    CONFIG['settings']['last_backup'] = time.time()
    save_config(CONFIG)
    tmp_path = os.path.join(TEMP_DIR, 'backup_config.json')
    with open(tmp_path, 'w') as f: json.dump(CONFIG, f, indent=2)
    try: asyncio.run(send_file_pyrogram(ADMIN_ID, tmp_path, 'backup_config.json', "💾 **Backup**"))
    except: pass

def backup_worker():
    while True:
        time.sleep(300)
        hours = CONFIG['settings'].get('auto_backup_hours', 0)
        if hours > 0 and (time.time() - CONFIG['settings'].get('last_backup', 0)) >= hours * 3600:
            do_backup()

threading.Thread(target=backup_worker, daemon=True).start()

# ========== SERVIDOR WEB (WEBHOOK) ==========
app = Flask(__name__)

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    global CONFIG
    update = request.json
    if not update: return 'ok'
    
    if 'message' in update and 'document' in update['message']:
        msg = update['message']
        chat_id = msg['chat']['id']
        if chat_id == ADMIN_ID and msg['document']['file_name'] == 'backup_config.json':
            file_info = requests.get(f"{API_URL}/getFile?file_id={msg['document']['file_id']}").json()
            content = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{file_info['result']['file_path']}").text
            try:
                new_config = json.loads(content)
                if "sites" in new_config:
                    CONFIG = new_config
                    save_config(CONFIG)
                    send_message(chat_id, "✅ **Configuración restaurada.**")
            except Exception as e: send_message(chat_id, f"❌ Error: {e}")
        return 'ok'
    
    if 'callback_query' in update:
        cb = update['callback_query']
        chat_id = cb['message']['chat']['id']
        message_id = cb['message']['message_id']
        data = cb['data']
        answer_callback(cb['id'])
        
        if chat_id != ADMIN_ID: return 'ok'
        
        if data == 'admin_panel':
            r_status = "🟢 ON" if CONFIG['settings'].get('auto_repair_enabled') else "🔴 OFF"
            kb = {'inline_keyboard': [
                [{'text': '📥 Backup Manual', 'callback_data': 'backup_manual'}, {'text': f"⏱️ Backup: {CONFIG['settings']['auto_backup_hours']}h", 'callback_data': 'backup_menu'}],
                [{'text': f"🤖 Auto-Reparación: {r_status}", 'callback_data': 'repair_menu'}],
                [{'text': '⚙️ Ajustes del Motor', 'callback_data': 'engine_settings'}],
                [{'text': '🔍 Escanear Selectores AHORA', 'callback_data': 'repair_now'}],
                [{'text': '🌐 Gestionar Sitios', 'callback_data': 'manage_sites'}]
            ]}
            edit_message(chat_id, message_id, "⚙️ **Panel de Administrador**", kb)
        elif data == 'engine_settings':
            kb = {'inline_keyboard': [
                [{'text': f"🖼️ Límite Imágenes: {CONFIG['settings'].get('max_images', 0)}", 'callback_data': 'set_max_img'}],
                [{'text': f"⏱️ Retraso: {CONFIG['settings'].get('download_delay', 0.5)}s", 'callback_data': 'set_delay'}],
                [{'text': '🖥️ Cambiar User-Agent', 'callback_data': 'set_ua'}],
                [{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]
            ]}
            edit_message(chat_id, message_id, "⚙️ **Ajustes del Motor**", kb)
        elif data == 'set_max_img':
            send_message(chat_id, "🖼️ Envía el nuevo límite de imágenes usando el comando:\n`/setlimit 50` (0 para ilimitado)")
        elif data == 'set_delay':
            send_message(chat_id, "⏱️ Envía el nuevo retraso usando el comando:\n`/setdelay 1.5` (en segundos)")
        elif data == 'set_ua':
            send_message(chat_id, "🖥️ Envía el nuevo User-Agent usando el comando:\n`/setua Tu_User_Agent_Aqui`")
        elif data == 'repair_menu':
            r_status = "🟢 Activado" if CONFIG['settings'].get('auto_repair_enabled') else "🔴 Desactivado"
            r_time = CONFIG['settings'].get('auto_repair_time')
            kb = {'inline_keyboard': [
                [{'text': f"Cambiar Estado ({r_status})", 'callback_data': 'toggle_repair'}],
                [{'text': f"⏰ Hora Actual: {r_time}", 'callback_data': 'set_repair_time'}],
                [{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]
            ]}
            edit_message(chat_id, message_id, "🤖 **Motor de Auto-Reparación**", kb)
        elif data == 'toggle_repair':
            CONFIG['settings']['auto_repair_enabled'] = not CONFIG['settings'].get('auto_repair_enabled', False)
            save_config(CONFIG)
            send_message(chat_id, "✅ Estado cambiado.")
            edit_message(chat_id, message_id, "Panel actualizado.", {'inline_keyboard': [[{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]]})
        elif data == 'set_repair_time':
            send_message(chat_id, "⏰ Envía: `/sethora HH:MM`")
        elif data == 'repair_now':
            send_message(chat_id, "⏳ Iniciando escaneo manual...")
            edit_message(chat_id, message_id, "⚙️ Escaneo en curso...", {'inline_keyboard': [[{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]]})
            threading.Thread(target=run_auto_repair).start()
        elif data == 'manage_sites':
            kb_list = []
            for k, s in CONFIG['sites'].items():
                st = s.get('status', 'active')
                if st == 'active': emoji = "🟢"
                elif st == 'blocked': emoji = "🟠"
                elif st == 'error': emoji = "🔴"
                else: emoji = "🟡"
                kb_list.append([{'text': f"{emoji} {s['name']}", 'callback_data': f"site_{k}"}])
            kb_list.append([{'text': '🔙 Volver', 'callback_data': 'admin_panel'}])
            edit_message(chat_id, message_id, "🌐 **Gestionar Sitios**\n🟢Activo 🟠Cloudflare 🔴Error 🟡Mantenimiento", {'inline_keyboard': kb_list})
        elif data.startswith('site_'):
            key = data.split('_')[1]
            site = CONFIG['sites'].get(key)
            if site:
                kb = {'inline_keyboard': [
                    [{'text': '🟢 Marcar Activo', 'callback_data': f'setact_{key}'}],
                    [{'text': '🟡 Mantenimiento Manual', 'callback_data': f'setmant_{key}'}],
                    [{'text': '🔙 Volver', 'callback_data': 'manage_sites'}]
                ]}
                edit_message(chat_id, message_id, f"🌐 **{site['name']}**\nEstado: `{site.get('status', 'active')}`\nTest URL: `{site.get('test_url', 'No configurada')}`", kb)
        elif data.startswith('setact_') or data.startswith('setmant_'):
            action, key = data.split('_')[0], data.split('_')[1]
            CONFIG['sites'][key]['status'] = 'active' if action == 'setact' else 'maintenance'
            save_config(CONFIG)
            send_message(chat_id, f"✅ Estado actualizado.")
            edit_message(chat_id, message_id, "Listo.", {'inline_keyboard': [[{'text': '🔙 Volver', 'callback_data': 'manage_sites'}]]})
        elif data == 'backup_menu':
            kb = {'inline_keyboard': [[{'text': '2h', 'callback_data': 'auto_2'}, {'text': '8h', 'callback_data': 'auto_8'}], [{'text': '24h', 'callback_data': 'auto_24'}], [{'text': 'Off', 'callback_data': 'auto_0'}], [{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]]}
            edit_message(chat_id, message_id, "⏱️ **Backup Automático**", kb)
        elif data.startswith('auto_'):
            hours = int(data.split('_')[1])
            CONFIG['settings']['auto_backup_hours'] = hours
            save_config(CONFIG)
            send_message(chat_id, f"✅ Backup a {hours} horas.")
            edit_message(chat_id, message_id, "Guardado.", {'inline_keyboard': [[{'text': '🔙 Volver', 'callback_data': 'admin_panel'}]]})
        elif data == 'backup_manual':
            send_message(chat_id, "⏳ Generando...")
            do_backup()
        return 'ok'

    if 'message' in update and 'text' in update['message']:
        msg = update['message']
        chat_id = msg['chat']['id']
        text = msg['text'].strip()
        chat_type = msg['chat']['type']
        user_obj = msg['from']
        username = user_obj.get('username', '')
        first_name = user_obj.get('first_name', 'Usuario')
        user_link = f"[{first_name}](tg://user?id={chat_id})" if not username else f"@{username}"
        
        if chat_id == ADMIN_ID:
            if text == '/admin':
                send_message(chat_id, "👑 Abriendo panel...", {'inline_keyboard': [[{'text': '⚙️ Abrir Panel', 'callback_data': 'admin_panel'}]]})
                return 'ok'
            if text.startswith('/setlimit'):
                try:
                    CONFIG['settings']['max_images'] = int(text[9:].strip())
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Límite configurado a {CONFIG['settings']['max_images']} imágenes.")
                except: send_message(chat_id, "❌ Formato incorrecto.")
                return 'ok'
            if text.startswith('/setdelay'):
                try:
                    CONFIG['settings']['download_delay'] = float(text[9:].strip())
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Retraso configurado a {CONFIG['settings']['download_delay']}s.")
                except: send_message(chat_id, "❌ Formato incorrecto.")
                return 'ok'
            if text.startswith('/setua'):
                CONFIG['settings']['user_agent'] = text[6:].strip()
                save_config(CONFIG)
                send_message(chat_id, "✅ User-Agent actualizado.")
                return 'ok'
            if text.startswith('/sethora'):
                new_time = text[8:].strip()
                if re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', new_time):
                    CONFIG['settings']['auto_repair_time'] = new_time
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Hora configurada a {new_time}.")
                else: send_message(chat_id, "❌ Formato HH:MM.")
                return 'ok'
            if text.startswith('/addsite'):
                parts = text[8:].strip().split('|')
                if len(parts) >= 4:
                    name, pattern, sel = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    test_url = parts[3].strip()
                    key = re.sub(r'[^a-z0-9]', '_', name.lower())
                    CONFIG['sites'][key] = {
                        "name": name, "url_pattern": pattern, 
                        "image_selector": sel, "title_selector": "title", "scroll_selector": sel, 
                        "active": True, "status": "active", "test_url": test_url
                    }
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Sitio **{name}** agregado con URL de prueba.")
                else:
                    send_message(chat_id, "❌ Formato incorrecto. Usa:\n`/addsite Nombre | dominio.com | selector_css | url_de_capitulo`")
                return 'ok'
            if text.startswith('/removesite'):
                key = text[11:].strip().lower()
                if key in CONFIG['sites']:
                    del CONFIG['sites'][key]
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Sitio `{key}` eliminado.")
                else: send_message(chat_id, f"❌ Sitio `{key}` no encontrado.")
                return 'ok'
            if text.startswith('/debug'):
                url_debug = text[6:].strip()
                if url_debug.startswith('http'):
                    send_message(chat_id, "🕵️ Iniciando modo debug...")
                    threading.Thread(target=debug_url, args=(chat_id, url_debug)).start()
                else: send_message(chat_id, "❌ Uso: `/debug https://link`")
                return 'ok'

        if text == '/start':
            kb = None
            if GROUP_LINK:
                kb = {'inline_keyboard': [[{'text': '👥 Unirse al Grupo de Soporte', 'url': GROUP_LINK}]]}
            send_message(chat_id, "📖 **¡Bienvenido!**\nEnvía un enlace de un capítulo y te lo devolveré en PDF.\n\nComandos:\n`/status` `/reportar` `/sugerir`", kb)
            return 'ok'
        
        if text == '/status' or text == '/sites':
            txt = "🌐 **Estado de los Sitios:**\n\n"
            for k, s in CONFIG['sites'].items():
                st = s.get('status', 'active')
                if st == 'active': emoji = "🟢"
                elif st == 'blocked': emoji = "🟠"
                elif st == 'error': emoji = "🔴"
                else: emoji = "🟡"
                txt += f"{emoji} **{s['name']}**\n"
            txt += "\n🟢Activo 🟠Cloudflare 🔴Error 🟡Mantenimiento"
            send_message(chat_id, txt)
            return 'ok'

        if text.startswith('/reportar'):
            now = time.time()
            if now - user_cooldowns.get(f"report_{chat_id}", 0) < 900:
                send_message(chat_id, "⏳ Ya has reportado un error hace poco. Espera 15 minutos.")
                return 'ok'
            user_cooldowns[f"report_{chat_id}"] = now
            motivo = text[9:].strip()
            send_message(chat_id, "🤖 **Bot Autónomo activado.**\nRevisando el sitio...")
            
            def auto_repair_from_report():
                site_to_fix = None
                for k, s in CONFIG['sites'].items():
                    if s['name'].lower() in motivo.lower() or s['url_pattern'] in motivo.lower():
                        site_to_fix = k; break
                if not site_to_fix:
                    send_message(chat_id, "❌ No pude identificar la web. Usa `/status`.")
                    send_message(ADMIN_ID, f"🚨 **Reporte no identificado**\n👤 {user_link} (ID: `{chat_id}`)\n📝 {motivo}")
                    return
                site = CONFIG['sites'][site_to_fix]
                if not site.get('test_url'):
                    send_message(chat_id, f"⚠️ **{site['name']}** sin test_url. Admin avisado.")
                    send_message(ADMIN_ID, f"🚨 **Fallo en {site['name']}**\n👤 {user_link}\n📝 {motivo}")
                    return
                send_message(ADMIN_ID, f"🤖 **Reporte de {user_link}**\nReparando **{site['name']}**...")
                send_message(chat_id, f"🔍 Escaneando **{site['name']}**...")
                new_sel = discover_best_selector(site['test_url'])
                if new_sel and new_sel != site['image_selector']:
                    site['image_selector'] = new_sel
                    site['scroll_selector'] = new_sel
                    site['status'] = 'active'
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ **¡Reparado!** Ya puedes descargar de **{site['name']}**.")
                    send_message(ADMIN_ID, f"✅ **Auto-Reparación Exitosa:** {site['name']}")
                else:
                    site['status'] = 'error'
                    save_config(CONFIG)
                    send_message(chat_id, f"❌ No pude reparar **{site['name']}**. Admin notificado.")
                    send_message(ADMIN_ID, f"❌ **Fallo en auto-reparación:** {site['name']}")
            
            threading.Thread(target=auto_repair_from_report).start()
            return 'ok'

        if text.startswith('/sugerir'):
            partes = text[8:].strip().split('|')
            if len(partes) < 2:
                send_message(chat_id, "❌ Formato incorrecto. Necesito la URL principal Y la URL de un capítulo para escanear.\n\nUso:\n`/sugerir https://web.com | https://web.com/capitulo-1`")
                return 'ok'
            
            url_principal = partes[0].strip()
            url_capitulo = partes[1].strip()
            
            if not url_principal.startswith('http') or not url_capitulo.startswith('http'):
                send_message(chat_id, "❌ Los enlaces deben empezar con http o https.")
                return 'ok'

            send_message(chat_id, "🤖 **Bot Autónomo.**\nAnalizando el capítulo de prueba para agregar la web...")
            
            def auto_add_from_suggestion():
                new_sel = discover_best_selector(url_capitulo)
                if new_sel:
                    domain = url_principal.split('//')[1].split('/')[0].replace('www.', '')
                    name = domain.split('.')[0].capitalize()
                    key = re.sub(r'[^a-z0-9]', '_', name.lower())
                    CONFIG['sites'][key] = {
                        "name": name, "url_pattern": domain, 
                        "image_selector": new_sel, "title_selector": "title", "scroll_selector": new_sel, 
                        "active": True, "status": "active", "test_url": url_capitulo
                    }
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ **¡Sitio Agregado!** La web **{name}** ya está soportada.")
                    send_message(ADMIN_ID, f"🤖 **Auto-Agregado por sugerencia**\n👤 {user_link} (ID: `{chat_id}`)\n🔗 Web: `{domain}`\n📝 Selector: `{new_sel}`")
                else:
                    send_message(chat_id, "❌ No pude encontrar el patrón de imágenes. Admin avisado.")
                    send_message(ADMIN_ID, f"💡 **Sugerencia fallida**\n👤 {user_link} (ID: `{chat_id}`)\n🔗 Web: {url_principal}\n🔗 Cap: {url_capitulo}")
            
            threading.Thread(target=auto_add_from_suggestion).start()
            return 'ok'

        if text.startswith('http'):
            if chat_type in ['group', 'supergroup']:
                send_message(chat_id, "📥 Para descargar, por favor **envíame este enlace por privado**.")
                return 'ok'
            if chat_id in user_sessions:
                send_message(chat_id, "⏳ Ya hay una descarga en curso.")
                return 'ok'
            if CONFIG['settings'].get('global_maintenance', False):
                send_message(chat_id, "🚧 El bot está en mantenimiento.")
                return 'ok'
            site_key = None
            for k, s in CONFIG['sites'].items():
                if s['url_pattern'] in text: site_key = k; break
            if not site_key:
                send_message(chat_id, "❌ Sitio no soportado.")
                return 'ok'
            if CONFIG['sites'][site_key].get('status', 'active') != 'active':
                send_message(chat_id, f"🚧 El sitio **{CONFIG['sites'][site_key]['name']}** no está activo.")
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