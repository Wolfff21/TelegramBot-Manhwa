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
from flask import Flask, request
from pyppeteer import launch
from pyrogram import Client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
GROUP_ID = int(os.environ.get("GROUP_ID", 0)) # ID del grupo para reportes (opcional)
GROUP_LINK = os.environ.get("GROUP_LINK", "") # Link de invitación al grupo
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

if not TOKEN or not API_ID or not API_HASH or not ADMIN_ID:
    logger.error("❌ Faltan variables de entorno críticas")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/multisite_downloads'
os.makedirs(TEMP_DIR, exist_ok=True)

user_sessions = {}
user_cooldowns = {} # Para evitar spam de reportes

# ========== GESTIÓN DE CONFIGURACIÓN ==========
CONFIG_FILE = 'sites.json'
DEFAULT_CONFIG = {
    "sites": {},
    "settings": {
        "auto_backup_hours": 0, 
        "last_backup": 0,
        "global_maintenance": False,
        "auto_repair_enabled": False,
        "auto_repair_time": "03:00",
        "last_auto_repair_date": ""
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
                headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
                handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False
            )
            page = await browser.newPage()
            await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            await page.setViewport({'width': 1920, 'height': 1080})
            await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': 60000})
            
            scroll_selector = site_config.get('scroll_selector', site_config['image_selector'])
            prev_count, repeats = 0, 0
            
            for i in range(30):
                await page.evaluate('window.scrollBy(0, window.innerHeight)')
                await asyncio.sleep(1.0)
                count = await page.evaluate(f"document.querySelectorAll(`{scroll_selector}`).length")
                if count == prev_count:
                    repeats += 1
                    if repeats >= 3: break
                else:
                    repeats = 0
                    prev_count = count
            
            await asyncio.sleep(2)
            img_sel = site_config['image_selector']
            urls = await page.evaluate(f"Array.from(document.querySelectorAll(`{img_sel}`)).map(img => img.src || img.getAttribute('data-src') || img.getAttribute('data-original')).filter(src => src && src.startsWith('http'))")
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
            except Exception as e: logger.error(f"Error descargando {img_url}: {e}")
        
        if not local_images: return None, None
        edit_message(chat_id, message_id, "📄 Convirtiendo a PDF...")
        with open(pdf_path, "wb") as f: f.write(img2pdf.convert(local_images))
        return pdf_path, pdf_name

# ========== PYROGRAM Y PROCESO ==========
async def send_file_pyrogram(chat_id, file_path, file_name, caption):
    async with Client("my_bot_session", api_id=int(API_ID), api_hash=API_HASH, bot_token=TOKEN, in_memory=True) as app:
        await app.send_document(chat_id=chat_id, document=file_path, file_name=file_name, caption=caption)

def process_download(chat_id, message_id, url):
    try:
        if CONFIG['settings'].get('global_maintenance', False):
            edit_message(chat_id, message_id, "🚧 **Bot en Mantenimiento Global**\n\nIntenta más tarde.")
            user_sessions.pop(chat_id, None)
            return

        site_key, site_config = None, None
        for k, s in CONFIG['sites'].items():
            if s['url_pattern'] in url:
                site_key, site_config = k, s
                break
                
        if not site_config:
            edit_message(chat_id, message_id, "❌ Sitio no soportado.")
            user_sessions.pop(chat_id, None)
            return

        if site_config.get('status') == 'maintenance':
            edit_message(chat_id, message_id, f"🚧 **{site_config['name']} en mantenimiento**")
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, f"🔍 Extrayendo de {site_config['name']}...")
        title, image_urls = get_image_urls(url, site_config)
        
        if not image_urls:
            edit_message(chat_id, message_id, "❌ No se encontraron imágenes.\n\n⚠️ *El administrador ha sido notificado.*")
            CONFIG['sites'][site_key]['status'] = 'error'
            save_config(CONFIG)
            send_message(ADMIN_ID, f"⚠️ **Fallo detectado**\nEl sitio **{site_config['name']}** falló.")
            user_sessions.pop(chat_id, None)
            return
        
        pdf_path, pdf_name = download_and_pdf(image_urls, title, chat_id, message_id)
        if not pdf_path:
            edit_message(chat_id, message_id, "❌ Error descargando imágenes.")
            user_sessions.pop(chat_id, None)
            return
        
        edit_message(chat_id, message_id, "☁️ Subiendo a Telegram...")
        asyncio.run(send_file_pyrogram(chat_id, pdf_path, pdf_name, f"✅ **{title}**\n📄 Enviado como PDF"))
        edit_message(chat_id, message_id, f"✅ **Enviado**\n📁 {pdf_name}")
        user_sessions.pop(chat_id, None)
        
    except Exception as e:
        logger.error(f"Error en proceso: {e}")
        edit_message(chat_id, message_id, f"❌ Error: {str(e)[:100]}")
        user_sessions.pop(chat_id, None)

# ========== MOTOR DE AUTO-REPARACIÓN ==========
def discover_best_selector(url):
    async def fetch():
        browser = None
        try:
            browser = await launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'], handleSIGINT=False, handleSIGTERM=False, handleSIGHUP=False)
            page = await browser.newPage()
            await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
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
        except Exception as e:
            if browser: await browser.close()
            return []
    imgs = asyncio.run(fetch())
    if not imgs: return None, 0
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
    if not candidates: return None, 0
    return max(candidates, key=candidates.get), len(imgs)

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
        new_sel, count = discover_best_selector(test_url)
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
        current_time = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        if current_time == CONFIG['settings'].get('auto_repair_time') and today != CONFIG['settings'].get('last_auto_repair_date'):
            CONFIG['settings']['last_auto_repair_date'] = today
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
    
    # --- RESTAURAR BACKUP (Solo Admin en Privado) ---
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
    
    # --- BOTONES (CALLBACKS) - Solo Admin ---
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
                [{'text': '🔍 Escanear Selectores AHORA', 'callback_data': 'repair_now'}],
                [{'text': '🌐 Gestionar Sitios', 'callback_data': 'manage_sites'}]
            ]}
            edit_message(chat_id, message_id, "⚙️ **Panel de Administrador**", kb)
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
                emoji = "🟢" if st == 'active' else ("🔴" if st == 'error' else "🟡")
                kb_list.append([{'text': f"{emoji} {s['name']}", 'callback_data': f"site_{k}"}])
            kb_list.append([{'text': '🔙 Volver', 'callback_data': 'admin_panel'}])
            edit_message(chat_id, message_id, "🌐 **Gestionar Sitios**", {'inline_keyboard': kb_list})
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

    # --- TEXTO Y COMANDOS ---
    if 'message' in update and 'text' in update['message']:
        msg = update['message']
        chat_id = msg['chat']['id']
        text = msg['text'].strip()
        chat_type = msg['chat']['type']
        
        # ===== COMANDOS DE ADMIN (SOLO PRIVADO) =====
        if chat_id == ADMIN_ID:
            if text == '/admin':
                send_message(chat_id, "👑 Abriendo panel...", {'inline_keyboard': [[{'text': '⚙️ Abrir Panel', 'callback_data': 'admin_panel'}]]})
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
                if len(parts) >= 3:
                    name, pattern, sel = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    test_url = parts[3].strip() if len(parts) > 3 else ""
                    key = re.sub(r'[^a-z0-9]', '_', name.lower())
                    CONFIG['sites'][key] = {
                        "name": name, "url_pattern": pattern, 
                        "image_selector": sel, "title_selector": "title", "scroll_selector": sel, 
                        "active": True, "status": "active", "test_url": test_url
                    }
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Sitio **{name}** agregado.")
                return 'ok'
            if text.startswith('/removesite'):
                key = text[11:].strip().lower()
                if key in CONFIG['sites']:
                    del CONFIG['sites'][key]
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ Sitio `{key}` eliminado.")
                else: send_message(chat_id, f"❌ Sitio `{key}` no encontrado.")
                return 'ok'

        # ===== COMANDOS PÚBLICOS (CUALQUIER PRIVADO O GRUPO) =====
        if text == '/start':
            # Botón opcional para el grupo
            kb = None
            if GROUP_LINK:
                kb = {'inline_keyboard': [[{'text': '👥 Unirse al Grupo de Soporte', 'url': GROUP_LINK}]]}
            
            send_message(chat_id, 
                "📖 **¡Bienvenido al Bot!**\n\n"
                "Envíame un enlace de un capítulo de manhwa y te lo devolveré en un PDF listo para leer.\n\n"
                "**Comandos disponibles:**\n"
                "• `/status` - Ver estado de las webs\n"
                "• `/reportar <mensaje>` - Reportar un error\n"
                "• `/sugerir <enlace>` - Proponer nueva web\n\n"
                "*Usa el bot en privado para descargar.*", kb)
            return 'ok'
        
        if text == '/status' or text == '/sites':
            txt = "🌐 **Estado de los Sitios:**\n\n"
            for k, s in CONFIG['sites'].items():
                st = s.get('status', 'active')
                emoji = "🟢" if st == 'active' else ("🔴" if st == 'error' else "🟡")
                txt += f"{emoji} **{s['name']}**\n"
            if CONFIG['settings'].get('global_maintenance', False):
                txt += "\n🚧 *Bot en Mantenimiento Global.*"
            send_message(chat_id, txt)
            return 'ok'

        # --- AUTO-REPARACIÓN POR REPORTE DE USUARIO ---
        if text.startswith('/reportar'):
            now = time.time()
            last_report = user_cooldowns.get(f"report_{chat_id}", 0)
            if now - last_report < 900:
                send_message(chat_id, "⏳ Ya has reportado un error hace poco. Espera 15 minutos.")
                return 'ok'
            
            user_cooldowns[f"report_{chat_id}"] = now
            motivo = text[9:].strip()
            send_message(chat_id, "🤖 **Bot Autónomo activado.**\nEstoy revisando el sitio para intentar repararlo automáticamente...")
            
            def auto_repair_from_report():
                site_to_fix = None
                for k, s in CONFIG['sites'].items():
                    if s['name'].lower() in motivo.lower() or s['url_pattern'] in motivo.lower():
                        site_to_fix = k; break
                
                if not site_to_fix:
                    send_message(chat_id, "❌ No he podido identificar la web. Escribe el nombre exacto del sitio o usa `/status` para ver los nombres.")
                    send_message(ADMIN_ID, f"🚨 **Reporte no identificado**\nUsuario `{chat_id}` reportó: {motivo}")
                    return

                site = CONFIG['sites'][site_to_fix]
                if not site.get('test_url'):
                    send_message(chat_id, f"⚠️ **{site['name']}** no tiene test_url configurado. Avisando al admin...")
                    send_message(ADMIN_ID, f"🚨 **Fallo reportado en {site['name']}**\nUsuario `{chat_id}` dice: {motivo}")
                    return

                send_message(ADMIN_ID, f"🤖 **Reporte recibido de {chat_id}**\nIntentando reparar **{site['name']}**...")
                send_message(chat_id, f"🔍 Escaneando **{site['name']}**...")
                
                new_sel, count = discover_best_selector(site['test_url'])
                
                if new_sel and new_sel != site['image_selector']:
                    site['image_selector'] = new_sel
                    site['scroll_selector'] = new_sel
                    site['status'] = 'active'
                    save_config(CONFIG)
                    send_message(chat_id, f"✅ **¡Reparado!** He actualizado el selector de **{site['name']}**. Ya puedes descargar.")
                    send_message(ADMIN_ID, f"✅ **Auto-Reparación Exitosa**\nEl sitio {site['name']} fue reparado solo.")
                else:
                    site['status'] = 'error'
                    save_config(CONFIG)
                    send_message(chat_id, f"❌ No pude reparar **{site['name']}** automáticamente. He avisado al admin.")
                    send_message(ADMIN_ID, f"❌ **Fallo en auto-reparación de {site['name']}**.")
            
            threading.Thread(target=auto_repair_from_report).start()
            return 'ok'

        # --- AUTO-AGREGADO POR SUGERENCIA DE USUARIO ---
        if text.startswith('/sugerir'):
            url_sugerida = text[8:].strip()
            if not url_sugerida.startswith('http'):
                send_message(chat_id, "❌ Enlace inválido. Ejemplo: `/sugerir https://nuevaweb.com`")
                return 'ok'
                
            send_message(chat_id, "🤖 **Bot Autónomo.**\nEstoy analizando esta web para agregarla automáticamente. Esto puede tardar un minuto...")
            
            def auto_add_from_suggestion():
                new_sel, count = discover_best_selector(url_sugerida)
                
                if new_sel and count > 0:
                    domain = url_sugerida.split('//')[1].split('/')[0].replace('www.', '')
                    name = domain.split('.')[0].capitalize()
                    key = re.sub(r'[^a-z0-9]', '_', name.lower())
                    
                    CONFIG['sites'][key] = {
                        "name": name, "url_pattern": domain, 
                        "image_selector": new_sel, "title_selector": "title", "scroll_selector": new_sel, 
                        "active": True, "status": "active", "test_url": url_sugerida
                    }
                    save_config(CONFIG)
                    
                    send_message(chat_id, f"✅ **¡Sitio Agregado!**\nLa web **{name}** ha sido analizada y agregada exitosamente.")
                    send_message(ADMIN_ID, f"🤖 **Auto-Agregado por sugerencia**\nWeb: {domain}\nSelector: `{new_sel}`\nAgregada automáticamente por el usuario {chat_id}.")
                else:
                    send_message(chat_id, "❌ No pude encontrarle el patrón a esta web. Le pasé la sugerencia al administrador.")
                    send_message(ADMIN_ID, f"💡 **Sugerencia de sitio (Fallo automático)**\nUsuario {chat_id} sugirió: {url_sugerida}\nRevisar a mano.")
            
            threading.Thread(target=auto_add_from_suggestion).start()
            return 'ok'

        # --- PROCESAR ENLACES DE DESCARGA ---
        if text.startswith('http'):
            # Si el enlace viene del grupo, le decimos que vaya al privado
            if chat_type in ['group', 'supergroup']:
                send_message(chat_id, "📥 Para descargar este capítulo, por favor **envíame este enlace por privado**. El grupo es solo para soporte y sugerencias.")
                return 'ok'

            # Si estamos en un chat privado:
            if chat_id in user_sessions:
                send_message(chat_id, "⏳ Ya hay una descarga en curso.")
                return 'ok'
            
            if CONFIG['settings'].get('global_maintenance', False):
                send_message(chat_id, "🚧 El bot está en mantenimiento. Intenta más tarde.")
                return 'ok'

            site_key = None
            for k, s in CONFIG['sites'].items():
                if s['url_pattern'] in text: site_key = k; break
                    
            if not site_key:
                send_message(chat_id, "❌ Sitio no soportado. Usa /status para ver la lista.")
                return 'ok'
            
            site_status = CONFIG['sites'][site_key].get('status', 'active')
            if site_status != 'active':
                send_message(chat_id, f"🚧 El sitio **{CONFIG['sites'][site_key]['name']}** está en mantenimiento o reportado con errores.")
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