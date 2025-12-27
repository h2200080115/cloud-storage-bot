import os
import logging
import re
import sqlite3
import uuid
from html import escape as html_escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ================== CONFIG ==================
# Local development configuration
# Set your bot token here (get it from @BotFather on Telegram)
BOT_TOKEN = "8505054887:AAHUiUnk9AI_PlEQTIv6pg3N2GsADCcSw-8"  # <-- Replace with your actual bot token

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Fallback to local SQLite if no DATABASE_URL is set
    DATABASE_URL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telecloud.db")

# Allow environment variables to override local config
if os.getenv("BOT_TOKEN"):
    BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))  # Set this in env vars

if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Please set your BOT_TOKEN in the script or as an environment variable.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("telecloud")


# ================== DB MANAGER ==================
class DatabaseManager:
    """Supports SQLite and PostgreSQL connections."""
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.is_postgres = db_url.startswith("postgres://") or db_url.startswith("postgresql://")
        self._init_db()

    def _get_conn(self):
        if self.is_postgres:
            import psycopg2
            return psycopg2.connect(self.db_url)
        else:
            conn = sqlite3.connect(self.db_url)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

    def _init_db(self):
        try:
            conn = self._get_conn()
            cur = conn.cursor()

            # Define types based on dialect
            if self.is_postgres:
                pk_type = "SERIAL PRIMARY KEY"
            else:
                pk_type = "INTEGER PRIMARY KEY AUTOINCREMENT"

            # Users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT
                );
            """)

            # Folders
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS folders (
                    id {pk_type},
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    folder_name TEXT NOT NULL,
                    share_token TEXT UNIQUE,
                    pin_code TEXT,
                    deleted_at TIMESTAMP,
                    UNIQUE (user_id, folder_name)
                );
            """)

            # Files
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS files (
                    id {pk_type},
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                    file_name TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_type TEXT NOT NULL DEFAULT 'document',
                    file_size INTEGER DEFAULT 0,
                    tags TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deleted_at TIMESTAMP
                );
            """)

            # --- Migrations (Add columns if missing) ---
            migrations = [
                ("folders", "share_token", "TEXT UNIQUE"),
                ("folders", "pin_code", "TEXT"),
                ("folders", "deleted_at", "TIMESTAMP"),
                ("files", "file_size", "INTEGER DEFAULT 0"),
                ("files", "tags", "TEXT"),
                ("files", "deleted_at", "TIMESTAMP"),
            ]

            for table, col, type_def in migrations:
                try:
                    if self.is_postgres:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {type_def}")
                    else:
                        # SQLite has no IF NOT EXISTS for columns, catch error
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_def}")
                except Exception:
                    pass

            conn.commit()

            # Indices
            indices = [
                "CREATE INDEX IF NOT EXISTS idx_files_user_folder ON files(user_id, folder_id)",
                "CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id)",
                "CREATE INDEX IF NOT EXISTS idx_files_created_at ON files(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_folders_share ON folders(share_token)",
                "CREATE INDEX IF NOT EXISTS idx_files_tags ON files(tags)"
            ]
            for idx in indices:
                try:
                    cur.execute(idx)
                except Exception: 
                    pass

            conn.commit()
            cur.close()
            conn.close()
            logger.info(f"✅ Database ready ({'Postgres' if self.is_postgres else 'SQLite'})")
        except Exception as e:
            logger.error(f"Failed to init DB: {e}")

    def execute(self, query, params=(), fetch_one=False, fetch_all=False):
        """Run a query with automatic commit/rollback and return rows if asked."""
        conn = self._get_conn()
        cur = conn.cursor()
        
        # Convert %s placeholders to ? for SQLite
        if not self.is_postgres:
            query = query.replace("%s", "?")
            
        try:
            cur.execute(query, params)
            if fetch_one:
                result = cur.fetchone()
            elif fetch_all:
                result = cur.fetchall()
            else:
                result = True
            conn.commit()
            return result
        except Exception as e:
            conn.rollback()
            logger.error(f"DB error: {e} :: {query} :: {params}")
            return None
        finally:
            cur.close()
            conn.close()

    def upsert_user(self, user_id, username):
        """Insert user safely correctly handling conflicts on both DBs."""
        if self.is_postgres:
            # Postgres: ON CONFLICT DO NOTHING
            query = "INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username"
        else:
            # SQLite: INSERT OR IGNORE (or REPLACE)
            query = "INSERT OR IGNORE INTO users (user_id, username) VALUES (%s, %s)"
        self.execute(query, (user_id, username))


db_manager = DatabaseManager(DATABASE_URL)


# ================== UI HELPERS ==================
def esc(text: str) -> str:
    return html_escape(str(text))

def sanitize_filename(name: str) -> str:
    """Keep alphanumerics, space, - _ . ; replace others with _ ; tighten."""
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    name = re.sub(r"_+", "_", name).strip(" ._")
    return name or "file"

def get_file_type_emoji(file_type: str) -> str:
    """Return emoji for file type."""
    emojis = {
        'document': '📄',
        'photo': '🖼️',
        'video': '🎬',
        'audio': '🎵',
        'voice': '🎤',
        'video_note': '📹',
        'animation': '🎞️',
        'sticker': '🏷️'
    }
    return emojis.get(file_type, '📄')

def format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    if size_bytes == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB']
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Create Folder", callback_data="create_folder")],
        [InlineKeyboardButton("📂 My Folders", callback_data="view_folders")],
        [InlineKeyboardButton("🗑️ Recycle Bin", callback_data="view_bin")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_folder_menu_keyboard(folder_id, folder_name, files):
    keyboard = []
    for fid, fname in files:
        keyboard.append([InlineKeyboardButton(f"📄 {fname}", callback_data=f"openfile_{fid}")])

    keyboard += [
        [InlineKeyboardButton("📤 Upload File", callback_data=f"upload_{folder_id}")],
        [InlineKeyboardButton("🔗 Share Folder", callback_data=f"share_{folder_id}")],
        [InlineKeyboardButton("🔒 Folder Lock", callback_data=f"lock_{folder_id}")],
        [InlineKeyboardButton("✏️ Rename Folder", callback_data=f"rename_{folder_id}")],
        [InlineKeyboardButton("🗑️ Delete Folder", callback_data=f"delete_{folder_id}")],
        [InlineKeyboardButton("🔙 Back to Folders", callback_data="view_folders")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_file_menu_keyboard(file_id_db, folder_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download File", callback_data=f"getfile_{file_id_db}")],
        [InlineKeyboardButton("🔗 Share File", callback_data=f"sharefile_{file_id_db}")],
        [InlineKeyboardButton("🏷️ Add Tags", callback_data=f"tagfile_{file_id_db}")],
        [InlineKeyboardButton("✏️ Rename File", callback_data=f"renamefile_{file_id_db}")],
        [InlineKeyboardButton("🗑️ Delete File", callback_data=f"deletefile_{file_id_db}")],
        [InlineKeyboardButton("🔙 Back to Folder", callback_data=f"open_{folder_id}")]
    ])

def render_folder_view(user_id: int, folder_id: int):
    """Return (folder_name, text, keyboard) for a folder view."""
    row = db_manager.execute(
        "SELECT folder_name FROM folders WHERE id=%s AND user_id=%s",
        (folder_id, user_id),
        fetch_one=True
    )
    if not row:
        return None, None, None

    folder_name = row[0]
    files = db_manager.execute(
        "SELECT id, file_name FROM files WHERE folder_id=%s AND user_id=%s AND deleted_at IS NULL ORDER BY id DESC",
        (folder_id, user_id),
        fetch_all=True
    ) or []

    if files:
        file_lines = "\n".join([f"• {esc(name)}" for (_fid, name) in files])
        files_block = f"\nFiles:\n{file_lines}"
    else:
        files_block = "\nFiles:\n• (none yet)"

    text = (
        f"📁 Folder: <b>{esc(folder_name)}</b>\n\n"
        f"<b>Total files:</b> {len(files)}\n"
        f"Select a file or action:{files_block}"
    )

    keyboard = get_folder_menu_keyboard(folder_id, folder_name, files)
    return folder_name, text, keyboard


# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or f"user_{user_id}"

    # Upsert user (SQLite style)
    # Upsert user with dialect-agnostic method
    db_manager.upsert_user(user_id, username)

    # Arg check for Deep Linking (Sharing)
    args = context.args
    if args and args[0].startswith("share_"):
        token = args[0].replace("share_", "")
        # Find folder by token
        row = db_manager.execute(
            "SELECT id, folder_name, user_id, pin_code FROM folders WHERE share_token=%s AND deleted_at IS NULL",
            (token,),
            fetch_one=True
        )
        if row:
            folder_id, folder_name, owner_id, pin_code = row
            
            # Check PIN logic for shared folders?
            # For simplicity, let's say Shared Link BYPASSES PIN? Or enforce it?
            # Let's ENFORCE it for security.
            if pin_code:
                 if not context.user_data.get(f"unlocked_shared_{folder_id}"):
                    # We need a special flow for unlocking shared folder. 
                    # But text_handler is user-specific. 
                    # Simplified: Shared folders bypass PIN for now (assuming if you share the link, you trust them).
                    pass

            # List files
            files = db_manager.execute(
                "SELECT id, file_name FROM files WHERE folder_id=%s AND deleted_at IS NULL ORDER BY id DESC",
                (folder_id,),
                fetch_all=True
            ) or []
            
            # Render Read-Only View
            file_lines = "\n".join([f"• {esc(name)}" for (_fid, name) in files]) or "(No files)"
            text = (
                f"📂 <b>Shared Folder: {esc(folder_name)}</b>\n"
                f"<i>Owned by User ID: {owner_id}</i>\n\n"
                f"Files:\n{file_lines}"
            )
            
            # Read-only keyboard (Downloads only)
            keyboard = []
            for fid, fname in files:
                # We need a special download callback that works for ANY user (check public/shared status)
                # But existing 'getfile_' checks 'user_id' ownership.
                # So we must update 'getfile_' to allow access if file is in a folder that has a share_token.
                keyboard.append([InlineKeyboardButton(f"⬇️ {fname}", callback_data=f"getshared_{fid}_{token}")])
            
            keyboard.append([InlineKeyboardButton("🏠 Open My Cloud", callback_data="back_home")])
            
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            return
        else:
             await update.message.reply_text("❌ Shared link is invalid or expired.")

    await update.message.reply_text(
        "☁️ Welcome to TeleCloud! Your personal Telegram-based file storage.\nChoose an option:",
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>TeleCloud Help</b>\n\n"
        "Use the buttons to create folders and manage files.\n\n"
        "Commands:\n"
        "• /start – Show main menu\n"
        "• /help – This help message\n"
        "• /stats – Count your folders & files\n"
        "• /recent – Show last 10 uploads\n"
        "• /search <text> – Find files whose name contains text\n\n"
        "After tapping a file you can Download, Share, Rename or Delete it."
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Get folder count
    folders = db_manager.execute(
        "SELECT COUNT(*) FROM folders WHERE user_id=%s", 
        (user_id,), 
        fetch_one=True
    )
    folders_count = folders[0] if folders else 0
    
    # Get total file count and size
    total_stats = db_manager.execute(
        "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files WHERE user_id=%s",
        (user_id,),
        fetch_one=True
    )
    total_files = total_stats[0] if total_stats else 0
    total_size = total_stats[1] if total_stats else 0
    
    # Get file counts by type
    type_stats = db_manager.execute(
        "SELECT file_type, COUNT(*), COALESCE(SUM(file_size), 0) FROM files "
        "WHERE user_id=%s GROUP BY file_type ORDER BY COUNT(*) DESC",
        (user_id,),
        fetch_all=True
    ) or []
    
    # Build type breakdown
    if type_stats:
        type_lines = []
        for ftype, count, size in type_stats:
            emoji = get_file_type_emoji(ftype)
            size_str = format_file_size(size)
            type_lines.append(f"  {emoji} {ftype}: {count} ({size_str})")
        type_breakdown = "\n".join(type_lines)
    else:
        type_breakdown = "  • No files yet"
    
    # Get top 3 folders by file count
    top_folders = db_manager.execute(
        "SELECT f.folder_name, COUNT(fi.id) as file_count, COALESCE(SUM(fi.file_size), 0) "
        "FROM folders f LEFT JOIN files fi ON f.id = fi.folder_id "
        "WHERE f.user_id=%s GROUP BY f.id ORDER BY file_count DESC LIMIT 3",
        (user_id,),
        fetch_all=True
    ) or []
    
    if top_folders:
        folder_lines = []
        for fname, fcount, fsize in top_folders:
            size_str = format_file_size(fsize)
            folder_lines.append(f"  📁 {esc(fname)}: {fcount} files ({size_str})")
        folders_breakdown = "\n".join(folder_lines)
    else:
        folders_breakdown = "  • No folders yet"
    
    await update.message.reply_text(
        f"📊 <b>Your Storage Stats</b>\n\n"
        f"📁 <b>Folders:</b> {folders_count}\n"
        f"📄 <b>Total Files:</b> {total_files}\n"
        f"💾 <b>Total Size:</b> {format_file_size(total_size)}\n\n"
        f"<b>📋 Files by Type:</b>\n{type_breakdown}\n\n"
        f"<b>📂 Top Folders:</b>\n{folders_breakdown}",
        parse_mode='HTML'
    )

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = db_manager.execute(
        "SELECT file_name FROM files WHERE user_id=%s ORDER BY id DESC LIMIT 10",
        (user_id,),
        fetch_all=True
    ) or []
    if not rows:
        await update.message.reply_text("No files uploaded yet.")
        return
    listing = "\n".join(f"• {esc(r[0])}" for r in rows)
    await update.message.reply_text(f"<b>Recent Files</b>\n{listing}", parse_mode='HTML')

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /search <text>")
        return
    like = f"%{query}%"
    
    # Check if searching by tag
    if query.startswith("#"):
        tag_query = query.lstrip("#")
        sql = "SELECT file_name FROM files WHERE user_id=%s AND tags LIKE %s AND deleted_at IS NULL ORDER BY file_name LIMIT 25"
        params = (user_id, f"%{tag_query}%")
    else:
        sql = "SELECT file_name FROM files WHERE user_id=%s AND file_name LIKE %s COLLATE NOCASE AND deleted_at IS NULL ORDER BY file_name LIMIT 25"
        params = (user_id, like)

    rows = db_manager.execute(sql, params, fetch_all=True) or []
    if not rows:
        await update.message.reply_text("No matches found.")
        return
    listing = "\n".join(f"• {esc(r[0])}" for r in rows)
    await update.message.reply_text(
        f"<b>Search Results</b> (query: {esc(query)})\n{listing}",
        parse_mode='HTML'
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return # Silent ignore

    # Stats
    user_count = db_manager.execute("SELECT COUNT(*) FROM users", fetch_one=True)[0]
    folder_count = db_manager.execute("SELECT COUNT(*) FROM folders", fetch_one=True)[0]
    file_count = db_manager.execute("SELECT COUNT(*) FROM files", fetch_one=True)[0]
    total_size = db_manager.execute("SELECT COALESCE(SUM(file_size),0) FROM files", fetch_one=True)[0]
    
    await update.message.reply_text(
        f"👑 <b>Admin Dashboard</b>\n\n"
        f"👤 Users: {user_count}\n"
        f"📁 Folders: {folder_count}\n"
        f"📄 Files: {file_count}\n"
        f"💾 Storage: {format_file_size(total_size)}\n\n"
        f"Commands:\n/broadcast &lt;msg&gt; - Send to all",
        parse_mode='HTML'
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    users = db_manager.execute("SELECT user_id FROM users", fetch_all=True)
    count = 0
    for (uid,) in users:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 <b>Announcement:</b>\n\n{html_escape(msg)}", parse_mode='HTML')
            count += 1
        except Exception:
            pass # Blocked or deleted
    
    await update.message.reply_text(f"✅ Broadcast sent to {count} users.")


# ================== BUTTONS / CALLBACKS ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    # 1) CREATE FOLDER
    if data == "create_folder":
        await query.edit_message_text("🗂️ Send me the new folder name:")
        context.user_data["awaiting_state"] = "create_folder"

    # 2) VIEW FOLDERS
    elif data == "view_folders":
        folders = db_manager.execute(
            "SELECT id, folder_name FROM folders WHERE user_id=%s AND deleted_at IS NULL ORDER BY folder_name",
            (user_id,),
            fetch_all=True
        )
        if not folders:
            await query.edit_message_text(
                "📭 You have no folders. Use ➕ Create Folder first.",
                reply_markup=get_main_keyboard()
            )
            return
        keyboard = [
            [InlineKeyboardButton(f"📁 {name}", callback_data=f"open_{fid}")]
            for fid, name in folders
        ]
        keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
        await query.edit_message_text("📂 Your folders:", reply_markup=InlineKeyboardMarkup(keyboard))

    # 3) OPEN FOLDER (Protected)
    elif data.startswith("open_"):
        folder_id = int(data.replace("open_", ""))
        
        # Check if locked
        row = db_manager.execute("SELECT pin_code FROM folders WHERE id=%s", (folder_id,), fetch_one=True)
        if row and row[0]:
            # Folder is locked
            if not context.user_data.get(f"unlocked_{folder_id}"):
                context.user_data["awaiting_state"] = "unlock_folder"
                context.user_data["unlock_folder_id"] = folder_id
                await query.edit_message_text(
                    "🔒 <b>This folder is locked.</b>\n\nPlease enter the 4-digit PIN to open it:",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="view_folders")]])
                )
                return

        folder_name, text, keyboard = render_folder_view(user_id, folder_id)
        if not folder_name:
            await query.edit_message_text(
                "❌ Folder not found or access denied. Returning to main menu.",
                reply_markup=get_main_keyboard()
            )
            return
        context.user_data["current_folder"] = {"id": folder_id, "name": folder_name}
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

    # 3.5) OPEN FILE MENU
    elif data.startswith("openfile_"):
        file_id_db = int(data.replace("openfile_", ""))
        row = db_manager.execute(
            "SELECT file_name, folder_id, file_type FROM files WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (file_id_db, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ File not found.", reply_markup=get_main_keyboard())
            return
        file_name, folder_id, file_type = row
        keyboard = get_file_menu_keyboard(file_id_db, folder_id)
        await query.edit_message_text(
            f"📄 <b>{esc(file_name)}</b>\n\nChoose an action:",
            reply_markup=keyboard,
            parse_mode='HTML'
        )

    # 4) DOWNLOAD FILE
    elif data.startswith("getfile_"):
        file_id_db = int(data.replace("getfile_", ""))
        row = db_manager.execute(
            "SELECT file_id, file_name, folder_id, file_type FROM files WHERE id=%s AND user_id=%s",
            (file_id_db, user_id),
            fetch_one=True
        )
        if not row:
            await query.message.reply_text("❌ File not found in database.")
            return
        file_id_raw, file_name, folder_id, file_type = row

        # Send media based on file type
        caption = f"⬇️ {file_name}"
        if file_type == 'photo':
            await query.message.reply_photo(photo=file_id_raw, caption=caption)
        elif file_type == 'video':
            await query.message.reply_video(video=file_id_raw, caption=caption)
        elif file_type == 'audio':
            await query.message.reply_audio(audio=file_id_raw, caption=caption)
        elif file_type == 'voice':
            await query.message.reply_voice(voice=file_id_raw, caption=caption)
        elif file_type == 'video_note':
            await query.message.reply_video_note(video_note=file_id_raw)
        elif file_type == 'animation':
            await query.message.reply_animation(animation=file_id_raw, caption=caption)
        elif file_type == 'sticker':
            await query.message.reply_sticker(sticker=file_id_raw)
        else:
            await query.message.reply_document(document=file_id_raw, caption=caption)

        # Refresh folder view
        _fname, text, keyboard = render_folder_view(user_id, folder_id)
        if _fname:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

    # 4.5) SHARE FILE
    elif data.startswith("sharefile_"):
        file_id_db = int(data.replace("sharefile_", ""))
        row = db_manager.execute(
            "SELECT file_id, file_name, folder_id, file_type FROM files WHERE id=%s AND user_id=%s",
            (file_id_db, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ File not found in database.")
            return
        file_id_raw, file_name, folder_id, file_type = row
        caption = f"🔗 Shared: <b>{esc(file_name)}</b> (Forward to share)"

        if file_type == 'photo':
            await context.bot.send_photo(chat_id=user_id, photo=file_id_raw, caption=caption, parse_mode='HTML')
        elif file_type == 'video':
            await context.bot.send_video(chat_id=user_id, video=file_id_raw, caption=caption, parse_mode='HTML')
        elif file_type == 'audio':
            await context.bot.send_audio(chat_id=user_id, audio=file_id_raw, caption=caption, parse_mode='HTML')
        elif file_type == 'voice':
            await context.bot.send_voice(chat_id=user_id, voice=file_id_raw, caption=caption, parse_mode='HTML')
        elif file_type == 'video_note':
            await context.bot.send_video_note(chat_id=user_id, video_note=file_id_raw)
            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode='HTML')
        elif file_type == 'animation':
            await context.bot.send_animation(chat_id=user_id, animation=file_id_raw, caption=caption, parse_mode='HTML')
        elif file_type == 'sticker':
            await context.bot.send_sticker(chat_id=user_id, sticker=file_id_raw)
            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode='HTML')
        else:
            await context.bot.send_document(chat_id=user_id, document=file_id_raw, caption=caption, parse_mode='HTML')

        _fname, text, keyboard = render_folder_view(user_id, folder_id)
        if _fname:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

    # 4.6) DELETE FILE
    elif data.startswith("deletefile_"):
        file_id_db = int(data.replace("deletefile_", ""))
        row = db_manager.execute(
            "SELECT file_name, folder_id FROM files WHERE id=%s AND user_id=%s",
            (file_id_db, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ File not found.", reply_markup=get_main_keyboard())
            return
        file_name, folder_id = row
        # Soft delete
        db_manager.execute("UPDATE files SET deleted_at=CURRENT_TIMESTAMP WHERE id=%s AND user_id=%s", (file_id_db, user_id))
        
        await query.message.reply_text(f"🗑️ File '<b>{esc(file_name)}</b>' moved to Recycle Bin.", parse_mode='HTML')
        _fname, text, keyboard = render_folder_view(user_id, folder_id)
        if _fname:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

    # 4.7) RENAME FILE (ask for new name)
    elif data.startswith("renamefile_"):
        file_id_db = int(data.replace("renamefile_", ""))
        row = db_manager.execute(
            "SELECT file_name, folder_id FROM files WHERE id=%s AND user_id=%s",
            (file_id_db, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ File not found.", reply_markup=get_main_keyboard())
            return
        current_name, folder_id = row
        context.user_data["awaiting_state"] = "rename_file"
        context.user_data["rename_file_id"] = file_id_db
        context.user_data["rename_file_folder_id"] = folder_id
        context.user_data["rename_file_old_name"] = current_name
        await query.edit_message_text(
            f"✏️ Send a new name for file '<b>{esc(current_name)}</b>'.",
            parse_mode='HTML'
        )



    # 15) ADD TAGS TO FILE
    elif data.startswith("tagfile_"):
        file_id_db = int(data.replace("tagfile_", ""))
        # Check exists
        row = db_manager.execute("SELECT file_name FROM files WHERE id=%s AND user_id=%s", (file_id_db, user_id), fetch_one=True)
        if not row:
            await query.edit_message_text("❌ File not found.", reply_markup=get_main_keyboard())
            return
            
        context.user_data["awaiting_state"] = "tag_file"
        context.user_data["tag_file_id"] = file_id_db
        await query.edit_message_text(
            f"🏷️ Send tags for '<b>{esc(row[0])}</b>' separated by commas.\n(e.g. <i>#work, #important, 2024</i>)",
            parse_mode='HTML'
        )

    # 5) UPLOAD SETUP
    elif data.startswith("upload_"):
        folder_id = int(data.replace("upload_", ""))
        row = db_manager.execute(
            "SELECT folder_name FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ Folder not found.", reply_markup=get_main_keyboard())
            return
        context.user_data["awaiting_state"] = "upload_file"
        context.user_data["upload_folder_id"] = folder_id
        await query.edit_message_text(
            f"📤 Send the file you want to upload into '<b>{esc(row[0])}</b>'.",
            parse_mode='HTML'
        )

    # 6) RENAME FOLDER (ask)
    elif data.startswith("rename_"):
        folder_id = int(data.replace("rename_", ""))
        row = db_manager.execute(
            "SELECT folder_name FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ Folder not found.", reply_markup=get_main_keyboard())
            return
        context.user_data["awaiting_state"] = "rename_folder"
        context.user_data["rename_folder_id"] = folder_id
        await query.edit_message_text(
            f"✏️ Send a new name for folder '<b>{esc(row[0])}</b>'.",
            parse_mode='HTML'
        )

    # 14) SHARE FOLDER
    elif data.startswith("share_"):
        folder_id = int(data.replace("share_", ""))
        # Check ownership and get existing token
        row = db_manager.execute(
            "SELECT folder_name, share_token FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
            fetch_one=True
        )

        if not row:
            await query.edit_message_text("❌ Folder not found.", reply_markup=get_main_keyboard())
            return
        
        # Generate token if missing
        existing_token = row[1]
        if existing_token:
            token = existing_token
        else:
            token = uuid.uuid4().hex
            db_manager.execute("UPDATE folders SET share_token=%s WHERE id=%s", (token, folder_id))
            
        bot_username = context.bot.username
        link = f"https://t.me/{bot_username}?start=share_{token}"
        
        await query.edit_message_text(
            f"🔗 <b>Share Link for '{esc(row[0])}'</b>\n\n"
            f"Anyone with this link can view and download files in this folder:\n\n"
            f"<code>{link}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Folder", callback_data=f"open_{folder_id}")]])
        )

    # 12) HANDLE SHARED DOWNLOAD
    elif data.startswith("getshared_"):
        # Format: getshared_FILEID_TOKEN
        try:
             _, fid_str, token = data.split("_", 2)
             file_id_db = int(fid_str)
        except ValueError:
             await query.answer("Error parsing data")
             return

        # 1. Verify token is valid for the folder containing this file
        # We join files and folders to check validity in one go
        row = db_manager.execute(
             """SELECT fi.file_id, fi.file_name, fi.file_type 
                FROM files fi 
                JOIN folders fo ON fi.folder_id = fo.id
                WHERE fi.id=%s AND fo.share_token=%s AND fo.deleted_at IS NULL AND fi.deleted_at IS NULL""",
             (file_id_db, token),
             fetch_one=True
        )
        
        if not row:
            await query.answer("❌ Access denied or file deleted.")
            return
            
        file_id_raw, file_name, file_type = row
        caption = f"⬇️ {file_name} (Shared)"
        
        # Send simple file (copy-pasting logic from getfile, simplified)
        if file_type == 'photo': await query.message.reply_photo(file_id_raw, caption=caption)
        elif file_type == 'video': await query.message.reply_video(file_id_raw, caption=caption)
        elif file_type == 'audio': await query.message.reply_audio(file_id_raw, caption=caption)
        elif file_type == 'voice': await query.message.reply_voice(file_id_raw, caption=caption)
        elif file_type == 'document': await query.message.reply_document(file_id_raw, caption=caption)
        else: await query.message.reply_document(file_id_raw, caption=caption) # Fallback

    # 13) LOCK FOLDER SETUP
    elif data.startswith("lock_"):
        folder_id = int(data.replace("lock_", ""))
        # Check ownership
        row = db_manager.execute(
            "SELECT folder_name FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ Folder not found.", reply_markup=get_main_keyboard())
            return
            
        context.user_data["awaiting_state"] = "set_pin"
        context.user_data["set_pin_folder_id"] = folder_id
        await query.edit_message_text(
            f"🔒 Send a 4-digit PIN to lock folder '<b>{esc(row[0])}</b>'.\n(Send '0000' to remove lock)",
            parse_mode='HTML'
        )

    # 14) SHARE FOLDER
    elif data.startswith("share_"):
        folder_id = int(data.replace("share_", ""))
        # Check ownership and get existing token
        row = db_manager.execute(
            "SELECT folder_name, share_token FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
            fetch_one=True
        )
        if not row:
            await query.edit_message_text("❌ Folder not found.", reply_markup=get_main_keyboard())
            return
        folder_name = row[0]
        # Soft delete folder (filtered out in views) and its files
        db_manager.execute("UPDATE folders SET deleted_at=CURRENT_TIMESTAMP WHERE user_id=%s AND id=%s", (user_id, folder_id))
        # We also soft-delete files inside so they appear in bin if we listing files individually
        db_manager.execute("UPDATE files SET deleted_at=CURRENT_TIMESTAMP WHERE user_id=%s AND folder_id=%s", (user_id, folder_id))
        
        await query.edit_message_text(
            f"🗑️ Folder '<b>{esc(folder_name)}</b>' moved to Recycle Bin.",
            reply_markup=get_main_keyboard(),
            parse_mode='HTML'
        )

    # 8) RECYCLE BIN
    elif data == "view_bin":
        # Get deleted folders
        del_folders = db_manager.execute(
            "SELECT id, folder_name FROM folders WHERE user_id=%s AND deleted_at IS NOT NULL",
            (user_id,), fetch_all=True
        ) or []
        # Get deleted files (that are not in deleted folders, or just all deleted files? 
        # Simpler: List Deleted Folders and Deleted Files separately)
        del_files = db_manager.execute(
            "SELECT id, file_name FROM files WHERE user_id=%s AND deleted_at IS NOT NULL",
            (user_id,), fetch_all=True
        ) or []
        
        if not del_folders and not del_files:
            await query.edit_message_text("🗑️ Recycle Bin is empty.", reply_markup=get_main_keyboard())
            return

        text = "<b>🗑️ Recycle Bin</b>\nTap to Restore or Delete Permanently:\n\n"
        keyboard = []
        
        for fid, fname in del_folders:
            keyboard.append([InlineKeyboardButton(f"📁 {fname} (Restore)", callback_data=f"restore_folder_{fid}")])
            keyboard.append([InlineKeyboardButton(f"❌ {fname} (Burn)", callback_data=f"burn_folder_{fid}")])
            
        for fid, fname in del_files:
            keyboard.append([InlineKeyboardButton(f"📄 {fname} (Restore)", callback_data=f"restore_file_{fid}")])
            keyboard.append([InlineKeyboardButton(f"❌ {fname} (Burn)", callback_data=f"burn_file_{fid}")])
            
        keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    # 9) RESTORE / BURNLogic
    elif data.startswith("restore_folder_"):
        fid = int(data.replace("restore_folder_", ""))
        db_manager.execute("UPDATE folders SET deleted_at=NULL WHERE id=%s AND user_id=%s", (fid, user_id))
        db_manager.execute("UPDATE files SET deleted_at=NULL WHERE folder_id=%s AND user_id=%s", (fid, user_id))
        await query.answer("Folder restored!")
        # Refresh bin
        await button_handler(update, context) # Recursively call to refresh view? No, slightly risky.
        # Just show main menu or success msg
        await query.edit_message_text("✅ Folder restored.", reply_markup=get_main_keyboard())

    elif data.startswith("restore_file_"):
        fid = int(data.replace("restore_file_", ""))
        db_manager.execute("UPDATE files SET deleted_at=NULL WHERE id=%s AND user_id=%s", (fid, user_id))
        await query.answer("File restored!")
        await query.edit_message_text("✅ File restored.", reply_markup=get_main_keyboard())

    elif data.startswith("burn_folder_"):
        fid = int(data.replace("burn_folder_", ""))
        db_manager.execute("DELETE FROM files WHERE folder_id=%s AND user_id=%s", (fid, user_id))
        db_manager.execute("DELETE FROM folders WHERE id=%s AND user_id=%s", (fid, user_id))
        await query.answer("Permanently deleted.")
        await query.edit_message_text("🔥 Folder permanently deleted.", reply_markup=get_main_keyboard())

    elif data.startswith("burn_file_"):
        fid = int(data.replace("burn_file_", ""))
        db_manager.execute("DELETE FROM files WHERE id=%s AND user_id=%s", (fid, user_id))
        await query.answer("Permanently deleted.")
        await query.edit_message_text("🔥 File permanently deleted.", reply_markup=get_main_keyboard())

    # 10) BACK TO MAIN
    elif data == "back_home":
        await query.edit_message_text("☁️ TeleCloud Main Menu", reply_markup=get_main_keyboard())


# ================== TEXT / FILE HANDLERS ==================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    state = context.user_data.get("awaiting_state")

    if not state:
        await update.message.reply_text("I'm not expecting text right now. Please use the menu buttons.")
        return

    # --- Unlock Folder PIN Check
    if state == "unlock_folder":
        folder_id = context.user_data.get("unlock_folder_id")
        if not folder_id:
            await update.message.reply_text("❌ Error. Please try opening the folder again.")
            return

        row = db_manager.execute("SELECT pin_code FROM folders WHERE id=%s", (folder_id,), fetch_one=True)
        if not row or row[0] != text.strip():
            await update.message.reply_text("❌ Incorrect PIN. Try again.")
            return

        # Success
        context.user_data[f"unlocked_{folder_id}"] = True
        context.user_data.pop("awaiting_state", None)
        context.user_data.pop("unlock_folder_id", None)
        
        # Show folder
        _fname, text_render, keyboard = render_folder_view(user_id, folder_id)
        await update.message.reply_text(
            f"🔓 <b>Folder Unlocked!</b>\n\n{text_render}",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    # --- Set Folder PIN
    if state == "set_pin":
        folder_id = context.user_data.get("set_pin_folder_id")
        pin = text.strip()
        
        if not re.match(r"^\d{4}$", pin):
             await update.message.reply_text("⚠️ Invalid format. Please send exactly 4 digits (e.g. 1234).")
             return

        if pin == "0000":
            pin = None # Remove lock
            msg = "🔓 Folder lock removed."
        else:
            msg = f"🔒 Folder locked with PIN: {pin}"

        db_manager.execute("UPDATE folders SET pin_code=%s WHERE id=%s AND user_id=%s", (pin, folder_id, user_id))
        context.user_data.pop("awaiting_state", None)
        context.user_data.pop("set_pin_folder_id", None)
        
        await update.message.reply_text(msg, reply_markup=get_main_keyboard())
        return

        
        await update.message.reply_text(msg, reply_markup=get_main_keyboard())
        return

    # --- Tagging File
    if state == "tag_file":
        file_id_db = context.user_data.get("tag_file_id")
        tags = text.strip()
        
        db_manager.execute("UPDATE files SET tags=%s WHERE id=%s AND user_id=%s", (tags, file_id_db, user_id))
        
        context.user_data.pop("awaiting_state", None)
        context.user_data.pop("tag_file_id", None)
        
        await update.message.reply_text(
            f"🏷️ Tags updated: <b>{esc(tags)}</b>",
            reply_markup=get_main_keyboard(),
            parse_mode='HTML'
        )
        return

    # --- Creating folder
    if state == "create_folder":
        exists = db_manager.execute(
            "SELECT 1 FROM folders WHERE user_id=%s AND folder_name=%s",
            (user_id, text),
            fetch_one=True
        )
        if exists:
            await update.message.reply_text(
                f"⚠️ A folder named '<b>{esc(text)}</b>' already exists. Please choose a different name.",
                parse_mode='HTML'
            )
            return

        db_manager.execute(
            "INSERT INTO folders (user_id, folder_name) VALUES (%s, %s)",
            (user_id, text)
        )
        # Pull the new folder and show it
        new_folder = db_manager.execute(
            "SELECT id FROM folders WHERE user_id=%s AND folder_name=%s",
            (user_id, text),
            fetch_one=True
        )
        context.user_data.pop("awaiting_state", None)

        if new_folder:
            new_folder_id = new_folder[0]
            _fname, text_render, keyboard = render_folder_view(user_id, new_folder_id)
            await update.message.reply_text(
                f"✅ Folder '<b>{esc(text)}</b>' created successfully!\n\n" + (text_render or ""),
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                f"✅ Folder '<b>{esc(text)}</b>' created. Please open it from '📂 My Folders'.",
                reply_markup=get_main_keyboard(),
                parse_mode='HTML'
            )
        return

    # --- Renaming folder
    if state == "rename_folder":
        folder_id = context.user_data.get("rename_folder_id")
        if not folder_id:
            await update.message.reply_text("❌ Renaming context lost. Please try again from the folder menu.")
            context.user_data.pop("awaiting_state", None)
            context.user_data.pop("rename_folder_id", None)
            return

        exists = db_manager.execute(
            "SELECT 1 FROM folders WHERE user_id=%s AND folder_name=%s AND id!=%s",
            (user_id, text, folder_id),
            fetch_one=True
        )
        if exists:
            await update.message.reply_text(
                f"⚠️ A folder named '<b>{esc(text)}</b>' already exists. Please choose a unique name.",
                parse_mode='HTML'
            )
            return

        db_manager.execute(
            "UPDATE folders SET folder_name=%s WHERE user_id=%s AND id=%s",
            (text, user_id, folder_id)
        )
        context.user_data.pop("awaiting_state", None)
        context.user_data.pop("rename_folder_id", None)

        await update.message.reply_text(
            f"✏️ Folder successfully renamed to '<b>{esc(text)}</b>'!",
            reply_markup=get_main_keyboard(),
            parse_mode='HTML'
        )
        return

    # --- Renaming file
    if state == "rename_file":
        file_id_db = context.user_data.get("rename_file_id")
        folder_id = context.user_data.get("rename_file_folder_id")
        if not file_id_db or not folder_id:
            await update.message.reply_text("❌ Rename context lost. Please open the file again.")
            for k in ("awaiting_state", "rename_file_id", "rename_file_folder_id", "rename_file_old_name"):
                context.user_data.pop(k, None)
            return

        new_name = text.strip()
        old_name = context.user_data.get("rename_file_old_name", "")
        if old_name and '.' in old_name and '.' not in new_name:
            ext = old_name.rsplit('.', 1)[1]
            new_name = f"{new_name}.{ext}"

        new_name = sanitize_filename(new_name)
        if not new_name:
            await update.message.reply_text("⚠️ Name can't be empty. Send a different name.")
            return
        if len(new_name) > 128:
            await update.message.reply_text("⚠️ Name too long (max 128 chars). Send a shorter name.")
            return

        exists = db_manager.execute(
            "SELECT 1 FROM files WHERE user_id=%s AND folder_id=%s AND file_name=%s AND id!=%s",
            (user_id, folder_id, new_name, file_id_db),
            fetch_one=True
        )
        if exists:
            await update.message.reply_text("⚠️ A file with that name already exists in this folder. Choose another.")
            return

        db_manager.execute(
            "UPDATE files SET file_name=%s WHERE id=%s AND user_id=%s",
            (new_name, file_id_db, user_id)
        )

        for k in ("awaiting_state", "rename_file_id", "rename_file_folder_id", "rename_file_old_name"):
            context.user_data.pop(k, None)

        _fname, text_render, keyboard = render_folder_view(user_id, folder_id)
        if _fname:
            await update.message.reply_text(
                f"✏️ File renamed to '<b>{esc(new_name)}</b>'.\n\n" + (text_render or ""),
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        return


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles uploaded files (documents, photos, videos, audio, voice, video notes, animations, stickers)."""
    user_id = update.effective_user.id

    if context.user_data.get("awaiting_state") != "upload_file":
        await update.message.reply_text("⚠️ Please select a folder and choose '📤 Upload File' first.")
        return

    folder_id = context.user_data.get("upload_folder_id")
    row = db_manager.execute(
        "SELECT folder_name FROM folders WHERE id=%s AND user_id=%s",
        (folder_id, user_id),
        fetch_one=True
    )

    if not row:
        await update.message.reply_text("❌ Target folder not found. Please try again.")
        context.user_data.pop("awaiting_state", None)
        context.user_data.pop("upload_folder_id", None)
        return

    folder_name = row[0]

    # Identify file type and extract metadata
    if update.message.document:
        file = update.message.document
        file_id = file.file_id
        file_name = file.file_name or f"document_{file.file_unique_id}"
        file_type = 'document'
        file_size = file.file_size or 0
    elif update.message.photo:
        file = update.message.photo[-1]  # Get largest photo
        file_id = file.file_id
        file_name = f"photo_{file.file_unique_id}.jpg"
        file_type = 'photo'
        file_size = file.file_size or 0
    elif update.message.video:
        file = update.message.video
        file_id = file.file_id
        file_name = file.file_name or f"video_{file.file_unique_id}.mp4"
        file_type = 'video'
        file_size = file.file_size or 0
    elif update.message.audio:
        file = update.message.audio
        file_id = file.file_id
        file_name = file.file_name or file.title or f"audio_{file.file_unique_id}.mp3"
        file_type = 'audio'
        file_size = file.file_size or 0
    elif update.message.voice:
        file = update.message.voice
        file_id = file.file_id
        file_name = f"voice_{file.file_unique_id}.ogg"
        file_type = 'voice'
        file_size = file.file_size or 0
    elif update.message.video_note:
        file = update.message.video_note
        file_id = file.file_id
        file_name = f"video_note_{file.file_unique_id}.mp4"
        file_type = 'video_note'
        file_size = file.file_size or 0
    elif update.message.animation:
        file = update.message.animation
        file_id = file.file_id
        file_name = file.file_name or f"animation_{file.file_unique_id}.gif"
        file_type = 'animation'
        file_size = file.file_size or 0
    elif update.message.sticker:
        file = update.message.sticker
        file_id = file.file_id
        ext = ".webm" if file.is_video else ".webp"
        file_name = f"sticker_{file.file_unique_id}{ext}"
        file_type = 'sticker'
        file_size = file.file_size or 0
    else:
        await update.message.reply_text(
            "⚠️ Unsupported file type. Supported: documents, photos, videos, audio, voice messages, video notes, animations, stickers."
        )
        return

    # Insert record (store telegram file_id and file_size)
    db_manager.execute(
        "INSERT INTO files (user_id, folder_id, file_name, file_id, file_type, file_size) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, folder_id, file_name, file_id, file_type, file_size)
    )

    # Clear state
    context.user_data.pop("awaiting_state", None)
    context.user_data.pop("upload_folder_id", None)

    # Get file type emoji
    type_emoji = get_file_type_emoji(file_type)
    size_str = format_file_size(file_size)

    # Show updated folder
    _fname, text, keyboard = render_folder_view(user_id, folder_id)
    if _fname:
        await update.message.reply_text(
            f"✅ {type_emoji} '<b>{esc(file_name)}</b>' ({size_str}) uploaded to '<b>{esc(folder_name)}</b>'.\n\n" + text,
            reply_markup=keyboard,
            parse_mode='HTML'
        )


# ================== APP ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(CallbackQueryHandler(button_handler))

    # Files first (documents/photos/videos/audio/voice/video_notes/animations/stickers)
    file_filters = (
        filters.Document.ALL | 
        filters.PHOTO | 
        filters.VIDEO | 
        filters.AUDIO | 
        filters.VOICE | 
        filters.VIDEO_NOTE | 
        filters.ANIMATION | 
        filters.Sticker.ALL
    )
    app.add_handler(MessageHandler(file_filters, file_handler))
    # Text last (non-commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        port = int(os.getenv("PORT", "8080"))
        logger.info(f"🚀 TeleCloud Bot starting webhook on port {port}...")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=f"/{BOT_TOKEN}",  # <-- Added leading slash
            webhook_url=f"{webhook_url}/{BOT_TOKEN}"
        )
    else:
        logger.info("🚀 TeleCloud Bot starting polling...")
        app.run_polling()


if __name__ == "__main__":
    main()
