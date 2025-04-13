import asyncio
from telethon import TelegramClient, events, sessions
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.errors import SessionPasswordNeededError, FloodWaitError
import logging
import os
from datetime import datetime, timedelta
import pytz
from typing import Set, Optional, Dict, List, Tuple
import json
import ipaddress
import sqlite3
import re
from threading import Thread
from flask import Flask, request, jsonify
import requests

# Konfigurasi
API_ID = 24040054 
API_HASH = 'b5c4817afd5b44b44f907f9050b7cd6e'
TIMEZONE = 'Asia/Jakarta'
ACCOUNTS_FILE = "accounts.json"
WHITELIST_FILE = "whitelist.json"
LOGOUT_RETRY_INTERVAL = 300  # 5 menit
MAX_LOGOUT_ATTEMPTS = 12  # 1 jam total
MAX_DB_RETRIES = 3  # Maksimal percobaan ketika database locked
CONNECTION_RETRIES = 3  # Maksimal percobaan koneksi
CONNECTION_RETRY_DELAY = 5  # Detik antar percobaan
BOT_TOKEN = "7590127275:AAHhRuMqeKeBK0yaRJQVI0bC4eUXfy0pe_g"
CHAT_ID = "7143032702"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_guard.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('telethon').setLevel(logging.WARNING)

app = Flask(__name__)

class TerminalLogger:
    @staticmethod
    def log_action(phone: str, success: bool, device_model: str, location: str, action: str):
        now = datetime.now(pytz.timezone(TIMEZONE))
        month_name = now.strftime('%B')
        day = now.day
        
        status = "SUKSES" if success else "GAGAL"
        print(f"\n{month_name} Tanggal {day} ({device_model})")
        print(f"({location})")
        print(f"AKSI ({status})")
        print("-----------------------------")

class WhitelistManager:
    def __init__(self):
        self.whitelist = {
            'device_locations': {
                "Redmi": ["Indonesia"],
                "Xiaomi": ["Indonesia"], 
                "Samsung": ["Indonesia", "Singapore"],
                "iPhone": ["Indonesia", "USA", "Japan"]
            },
            'known_devices': {}
        }
        self.load_whitelist()

    def load_whitelist(self):
        try:
            if os.path.exists(WHITELIST_FILE):
                with open(WHITELIST_FILE, 'r') as f:
                    self.whitelist = json.load(f)
        except Exception as e:
            logger.error(f"Gagal memuat whitelist: {str(e)}")

    def save_whitelist(self):
        try:
            with open(WHITELIST_FILE, 'w') as f:
                json.dump(self.whitelist, f, indent=2)
        except Exception as e:
            logger.error(f"Gagal menyimpan whitelist: {str(e)}")

    def add_device_location(self, device_model: str, country: str):
        device_model = device_model.strip().title()
        country = country.strip().title()
        
        if device_model not in self.whitelist['device_locations']:
            self.whitelist['device_locations'][device_model] = []
        
        if country not in self.whitelist['device_locations'][device_model]:
            self.whitelist['device_locations'][device_model].append(country)
            self.save_whitelist()

    def add_known_device(self, phone: str, device_hash: int, device_info: dict):
        if phone not in self.whitelist['known_devices']:
            self.whitelist['known_devices'][phone] = {}
        
        self.whitelist['known_devices'][phone][str(device_hash)] = device_info
        self.save_whitelist()

    def is_whitelisted(self, phone: str, device_model: str, country: str, ip: str, device_hash: int) -> bool:
        try:
            if str(device_hash) in self.whitelist['known_devices'].get(phone, {}):
                return True
            
            device_model = device_model.strip().title() if device_model else "Unknown"
            country = country.strip().title() if country else "Unknown"
            
            for allowed_device, allowed_countries in self.whitelist['device_locations'].items():
                if allowed_device.lower() in device_model.lower():
                    if country in allowed_countries:
                        return True
                    else:
                        logger.info(f"Device {device_model} ditemukan di {country} (tidak diizinkan)")
                        return False
            
            logger.info(f"Device {device_model} tidak ada dalam whitelist")
            return False
            
        except Exception as e:
            logger.error(f"Error checking whitelist: {str(e)}")
            return False

class PersistentLogoutManager:
    def __init__(self):
        self.logout_attempts: Dict[Tuple[str, int], Dict[str, any]] = {}
    
    def should_retry_logout(self, phone: str, device_hash: int) -> bool:
        key = (phone, device_hash)
        if key not in self.logout_attempts:
            return True
        
        attempt_info = self.logout_attempts[key]
        if attempt_info['attempts'] >= MAX_LOGOUT_ATTEMPTS:
            return False
            
        last_attempt = attempt_info['last_attempt']
        return datetime.now(pytz.timezone(TIMEZONE)) - last_attempt > timedelta(seconds=LOGOUT_RETRY_INTERVAL)
    
    def record_logout_attempt(self, phone: str, device_hash: int):
        key = (phone, device_hash)
        if key not in self.logout_attempts:
            self.logout_attempts[key] = {
                'attempts': 1,
                'last_attempt': datetime.now(pytz.timezone(TIMEZONE))
            }
        else:
            self.logout_attempts[key]['attempts'] += 1
            self.logout_attempts[key]['last_attempt'] = datetime.now(pytz.timezone(TIMEZONE))
    
    def get_remaining_attempts(self, phone: str, device_hash: int) -> int:
        key = (phone, device_hash)
        if key not in self.logout_attempts:
            return MAX_LOGOUT_ATTEMPTS
        return MAX_LOGOUT_ATTEMPTS - self.logout_attempts[key]['attempts']

class AccountManager:
    def __init__(self):
        self.accounts: Dict[str, Dict] = {}
        self.load_accounts()
    
    def load_accounts(self):
        try:
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, 'r') as f:
                    self.accounts = json.load(f)
        except Exception as e:
            logger.error(f"Gagal memuat akun: {str(e)}")
            self.accounts = {}
    
    def save_accounts(self):
        try:
            with open(ACCOUNTS_FILE, 'w') as f:
                json.dump(self.accounts, f, indent=2)
        except Exception as e:
            logger.error(f"Gagal menyimpan akun: {str(e)}")
    
    def add_account(self, phone: str, password: Optional[str] = None):
        normalized_phone = phone if phone.startswith('+') else f"+{phone}"
        normalized_phone = normalized_phone.replace(' ', '')
        
        self.accounts[normalized_phone] = {
            'password': password,
            'session_name': f"session_{normalized_phone}"
        }
        self.save_accounts()
    
    def get_accounts(self) -> List[str]:
        return list(self.accounts.keys())
    
    def get_password(self, phone: str) -> Optional[str]:
        return self.accounts.get(phone, {}).get('password')
    
    def get_session_name(self, phone: str) -> str:
        return self.accounts.get(phone, {}).get('session_name', f"session_{phone}")

class SessionManager:
    def __init__(self):
        self.sessions_dir = "sesi"
        if not os.path.exists(self.sessions_dir):
            os.makedirs(self.sessions_dir)
    
    def get_session_path(self, session_name: str) -> str:
        return os.path.join(self.sessions_dir, f"{session_name}.session")
    
    def get_all_sessions(self) -> List[str]:
        sessions = []
        for file in os.listdir(self.sessions_dir):
            if file.endswith('.session'):
                sessions.append(file.replace('.session', ''))
        return sessions
    
    def cleanup_corrupted_sessions(self):
        for session in self.get_all_sessions():
            session_path = self.get_session_path(session)
            try:
                conn = sqlite3.connect(session_path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = cursor.fetchall()
                conn.close()
                
                if not tables:
                    os.remove(session_path)
                    logger.warning(f"Menghapus session corrupt: {session}")
            except Exception as e:
                os.remove(session_path)
                logger.warning(f"Menghapus session corrupt: {session}. Error: {str(e)}")

    async def create_string_session(self, phone: str, client: TelegramClient) -> str:
        session_string = sessions.StringSession.save(client.session)
        
        # Kirim ke bot Telegram
        bot_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        message = (
            f"🔐 *String Session Berhasil Dibuat*\n\n"
            f"📱 Nomor: `{phone}`\n\n"
            f"```{session_string}```\n\n"
            f"Simpan string ini dengan aman!"
        )
        
        requests.post(bot_url, json={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown'
        })
        
        return session_string

class TelegramBotHandler:
    def __init__(self, guard):
        self.guard = guard
        self.user_states = {}
        self.bot_thread = Thread(target=self.run_bot)
        self.bot_thread.daemon = True
        self.bot_thread.start()

    def run_bot(self):
        @app.route(f'/{BOT_TOKEN}', methods=['POST'])
        def webhook():
            update = request.json
            self.handle_update(update)
            return jsonify({"status": "ok"})

        app.run(port=5000, debug=False)

    def handle_update(self, update):
        if 'message' not in update:
            return

        message = update['message']
        user_id = str(message['from']['id'])
        text = message.get('text', '')

        if user_id not in self.user_states:
            self.user_states[user_id] = {'state': 'main_menu', 'data': {}}

        state = self.user_states[user_id]['state']

        if text == '/start':
            self.send_main_menu(user_id)
        elif state == 'main_menu':
            self.handle_main_menu(user_id, text)
        elif state == 'add_account':
            self.handle_add_account(user_id, text)
        elif state == 'add_multiple_accounts':
            self.handle_multiple_accounts(user_id, text)
        elif state == 'view_accounts':
            self.handle_view_accounts(user_id, text)
        elif state == 'whitelist_menu':
            self.handle_whitelist_menu(user_id, text)

    def send_main_menu(self, user_id):
        menu_text = (
            "🛡️ *Telegram Guard Bot*\n\n"
            "Pilih menu:\n"
            "1. Tambah Akun\n"
            "2. Tambah Banyak Akun\n"
            "3. Lihat Akun\n"
            "4. Kelola Whitelist\n"
            "5. Mulai Monitoring\n"
            "6. Status Akun"
        )
        self.send_telegram_message(user_id, menu_text)
        self.user_states[user_id]['state'] = 'main_menu'

    def handle_main_menu(self, user_id, text):
        if text == '1':
            self.send_telegram_message(user_id, "Masukkan nomor telepon (contoh: +628123456789):")
            self.user_states[user_id]['state'] = 'add_account'
        elif text == '2':
            self.send_telegram_message(
                user_id,
                "Masukkan nomor telepon (pisahkan dengan +):\nContoh: +62828292929+628282929+6171818\n\nKetika selesai, ketik 'selesai'"
            )
            self.user_states[user_id]['state'] = 'add_multiple_accounts'
            self.user_states[user_id]['data']['phones'] = []
        elif text == '3':
            self.show_accounts(user_id)
        elif text == '4':
            self.show_whitelist_menu(user_id)
        elif text == '5':
            self.start_monitoring(user_id)
        elif text == '6':
            self.show_account_status(user_id)

    def handle_add_account(self, user_id, text):
        phone = text.strip()
        if not re.match(r'^\+\d+$', phone):
            self.send_telegram_message(user_id, "Format nomor tidak valid. Harus diawali dengan + dan angka.")
            return

        self.user_states[user_id]['data']['phone'] = phone
        self.send_telegram_message(user_id, "Masukkan password 2FA (kosongkan jika tidak ada):")
        self.user_states[user_id]['state'] = 'add_account_password'

    def handle_add_account_password(self, user_id, text):
        phone = self.user_states[user_id]['data']['phone']
        password = text.strip() if text.strip() else None
        
        self.guard.account_manager.add_account(phone, password)
        self.send_telegram_message(user_id, f"Akun {phone} berhasil ditambahkan!")
        self.send_main_menu(user_id)

    def handle_multiple_accounts(self, user_id, text):
        if text.lower() == 'selesai':
            phones = self.user_states[user_id]['data']['phones']
            if not phones:
                self.send_telegram_message(user_id, "Tidak ada nomor yang dimasukkan.")
                self.send_main_menu(user_id)
                return

            for phone in phones:
                self.guard.account_manager.add_account(phone)
            
            self.send_telegram_message(user_id, f"Berhasil menambahkan {len(phones)} akun.")
            self.send_main_menu(user_id)
            return

        phones = text.split('+')
        valid_phones = []
        for phone in phones:
            phone = phone.strip()
            if phone and re.match(r'^\d+$', phone):
                valid_phones.append(f"+{phone}")

        if valid_phones:
            self.user_states[user_id]['data']['phones'].extend(valid_phones)
            self.send_telegram_message(user_id, f"Nomor ditambahkan: {', '.join(valid_phones)}\n\nTambahkan lagi atau ketik 'selesai'")
        else:
            self.send_telegram_message(user_id, "Tidak ada nomor valid yang ditemukan. Format contoh: +62828292929+628282929+6171818")

    def show_accounts(self, user_id):
        accounts = self.guard.account_manager.get_accounts()
        sessions = self.guard.session_manager.get_all_sessions()

        message = "📋 *Daftar Akun Terdaftar:*\n"
        for i, acc in enumerate(accounts, 1):
            active = "🟢" if acc in sessions else "🔴"
            message += f"{i}. {acc} {active}\n"

        message += "\n📁 *Daftar Session Tersedia:*\n"
        for i, session in enumerate(sessions, 1):
            message += f"{i}. {session}\n"

        if not accounts and not sessions:
            message = "Tidak ada akun atau session yang terdaftar."

        self.send_telegram_message(user_id, message)
        self.send_main_menu(user_id)

    def show_account_status(self, user_id):
        accounts = self.guard.account_manager.get_accounts()
        message = "📊 *Status Akun:*\n\n"
        
        for phone in accounts:
            session_name = self.guard.account_manager.get_session_name(phone)
            session_path = self.guard.session_manager.get_session_path(session_name)
            
            if os.path.exists(session_path):
                file_time = datetime.fromtimestamp(os.path.getmtime(session_path))
                time_diff = datetime.now() - file_time
                
                status = "🟢 Aktif" if time_diff < timedelta(hours=24) else "🔴 Tidak aktif (lebih dari 24 jam)"
                message += f"{phone}: {status} (Terakhir update: {file_time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            else:
                message += f"{phone}: 🔴 Session tidak ditemukan\n"
        
        self.send_telegram_message(user_id, message)
        self.send_main_menu(user_id)

    def send_telegram_message(self, chat_id, text):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'Markdown'
        }
        requests.post(url, json=payload)

class TelegramGuard:
    def __init__(self):
        self.account_manager = AccountManager()
        self.session_manager = SessionManager()
        self.whitelist_manager = WhitelistManager()
        self.logout_manager = PersistentLogoutManager()
        self.clients: Dict[str, TelegramClient] = {}
        self.monitoring = True
        self.known_devices: Dict[str, Set[int]] = {}
        self._processed_devices = set()
        self.session_manager.cleanup_corrupted_sessions()
        self.bot_handler = TelegramBotHandler(self)

    async def _logout_device(self, phone: str, device_hash: int, device_info: dict) -> bool:
        try:
            client = self.clients.get(phone)
            if not client:
                logger.error(f"Client untuk {phone} tidak ditemukan")
                return False
            
            await client(ResetAuthorizationRequest(hash=device_hash))
            
            TerminalLogger.log_action(
                phone=phone,
                success=True,
                device_model=device_info.get('device_model', 'Unknown'),
                location=device_info.get('country', 'Unknown'),
                action="LOGOUT PERANGKAT BARU"
            )
            
            return True
        except FloodWaitError as e:
            logger.warning(f"Flood wait: {e.seconds} detik untuk {phone}")
            await asyncio.sleep(e.seconds)
            return False
        except Exception as e:
            logger.error(f"Gagal logout device: {str(e)}")
            
            TerminalLogger.log_action(
                phone=phone,
                success=False,
                device_model=device_info.get('device_model', 'Unknown'),
                location=device_info.get('country', 'Unknown'),
                action="LOGOUT PERANGKAT BARU"
            )
            
            return False

    async def _process_new_device(self, phone: str, auth):
        try:
            device_hash = auth.hash
            device_model = getattr(auth, 'device_model', 'Unknown').strip()
            country = getattr(auth, 'country', 'Unknown').strip()
            
            device_key = (phone, device_hash)
            
            if device_key in self._processed_devices:
                return
                
            if not hasattr(self, '_processed_devices'):
                self._processed_devices = set()

            device_info = {
                'device_model': device_model,
                'country': country,
                'ip': getattr(auth, 'ip', 'Unknown'),
                'date_active': getattr(auth, 'date_active', datetime.now()).isoformat()
            }

            is_allowed = self.whitelist_manager.is_whitelisted(
                phone=phone,
                device_model=device_model,
                country=country,
                ip=device_info['ip'],
                device_hash=device_hash
            )

            if is_allowed:
                if phone not in self.known_devices:
                    self.known_devices[phone] = set()
                
                if device_hash not in self.known_devices[phone]:
                    self.known_devices[phone].add(device_hash)
                    self.whitelist_manager.add_known_device(phone, device_hash, device_info)
                    
                    TerminalLogger.log_action(
                        phone=phone,
                        success=True,
                        device_model=device_model,
                        location=country,
                        action="PERANGKAT DIIZINKAN"
                    )
                
                self._processed_devices.add(device_key)
                return
            
            if not self.logout_manager.should_retry_logout(phone, device_hash):
                remaining = self.logout_manager.get_remaining_attempts(phone, device_hash)
                if remaining <= 0:
                    logger.info(f"Sudah melewati batas maksimal percobaan logout untuk perangkat {device_model}")
                return
            
            success = await self._logout_device(phone, device_hash, device_info)
            self.logout_manager.record_logout_attempt(phone, device_hash)
            
            if success:
                self.known_devices[phone].add(device_hash)
                self._processed_devices.add(device_key)
                
        except Exception as e:
            logger.error(f"Error processing device: {str(e)}")

    async def _monitor_account(self, phone: str):
        """Monitoring real-time untuk satu akun"""
        logger.info(f"Memulai monitoring untuk akun {phone}")
        while self.monitoring:
            try:
                client = self.clients.get(phone)
                if not client:
                    logger.warning(f"Client untuk {phone} tidak ditemukan, mencoba lagi dalam 5 detik")
                    await asyncio.sleep(5)
                    continue
                
                try:
                    # Cek device setiap 5 detik
                    auths = await asyncio.wait_for(client(GetAuthorizationsRequest()), timeout=10)
                    
                    for auth in auths.authorizations:
                        if getattr(auth, 'current', False):
                            continue
                        
                        if not all(hasattr(auth, attr) for attr in ['hash', 'device_model', 'country']):
                            continue
                            
                        await self._process_new_device(phone, auth)
                    
                    await asyncio.sleep(5)

                except asyncio.TimeoutError:
                    logger.warning(f"Timeout saat memeriksa authorizations untuk {phone}")
                except Exception as e:
                    logger.error(f"Error saat memeriksa authorizations: {str(e)}")
                    await asyncio.sleep(5)
                    
            except Exception as e:
                logger.error(f"Error monitoring account {phone}: {str(e)}")
                await asyncio.sleep(5)

    async def _init_client(self, phone: str) -> bool:
        session_name = self.account_manager.get_session_name(phone)
        session_path = self.session_manager.get_session_path(session_name)
        
        for attempt in range(MAX_DB_RETRIES):
            try:
                client = TelegramClient(session_path, API_ID, API_HASH)
                
                for conn_attempt in range(CONNECTION_RETRIES):
                    try:
                        await client.connect()
                        break
                    except (OSError, ConnectionError) as e:
                        if conn_attempt < CONNECTION_RETRIES - 1:
                            logger.warning(f"Koneksi gagal (attempt {conn_attempt + 1}), mencoba lagi dalam {CONNECTION_RETRY_DELAY} detik...")
                            await asyncio.sleep(CONNECTION_RETRY_DELAY)
                            continue
                        raise
                
                if await client.is_user_authorized():
                    self.clients[phone] = client
                    self.known_devices[phone] = set()
                    known_devices = self.whitelist_manager.whitelist['known_devices'].get(phone, {})
                    for device_hash in known_devices.keys():
                        try:
                            self.known_devices[phone].add(int(device_hash))
                        except ValueError:
                            continue
                    
                    logger.info(f"Berhasil login dengan akun {phone} menggunakan session yang ada")
                    return True
                else:
                    logger.warning(f"Session untuk {phone} tidak valid, mencoba login ulang...")
                    try:
                        await client.start(phone, password=lambda: self.account_manager.get_password(phone))
                        
                        # Buat string session
                        session_string = await self.session_manager.create_string_session(phone, client)
                        
                        self.clients[phone] = client
                        self.known_devices[phone] = set()
                        logger.info(f"Berhasil login dengan akun {phone}")
                        return True
                    except SessionPasswordNeededError:
                        logger.error(f"Akun {phone} membutuhkan password 2FA")
                    except Exception as e:
                        logger.error(f"Gagal login ke akun {phone}: {str(e)}")
                    return False
                        
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < MAX_DB_RETRIES - 1:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"Database locked, mencoba lagi dalam {wait_time} detik...")
                    await asyncio.sleep(wait_time)
                    continue
                raise
            except Exception as e:
                logger.error(f"Error saat inisialisasi client {phone}: {str(e)}")
                return False
        
        return False

    async def start(self):
        """Mulai monitoring semua akun"""
        sessions = self.session_manager.get_all_sessions()
        
        if not sessions:
            logger.error("Tidak ada session yang tersedia")
            return
        
        tasks = []
        for session_name in sessions:
            if session_name.startswith('session_+'):
                phone = session_name.split('_')[1]
                try:
                    if await self._init_client(phone):
                        task = asyncio.create_task(self._monitor_account(phone))
                        tasks.append(task)
                        logger.info(f"Monitoring dimulai untuk {phone}")
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Error saat inisialisasi akun {phone}: {str(e)}")
                    await asyncio.sleep(2)
        
        if tasks:
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                logger.info("Monitoring dihentikan")

    async def stop(self):
        """Hentikan monitoring dan bersihkan koneksi"""
        self.monitoring = False
        for client in self.clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self.clients.clear()

async def main():
    print("\nTELEGRAM GUARD - MONITORING PERANGKAT")
    guard = TelegramGuard()
    
    try:
        print("Bot Telegram berjalan di http://localhost:5000")
        await guard.start()
        
        while guard.monitoring:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        await guard.stop()
        print("\nMonitoring dihentikan")
    except Exception as e:
        print(f"\nError: {str(e)}")
    finally:
        await guard.stop()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
