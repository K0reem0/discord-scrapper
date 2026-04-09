import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio
import os
import time
import base64
import re
import shutil
import gc
import urllib.parse
import json
from typing import Literal
from PIL import Image

# مكتبة تجميع الـ PDF
from reportlab.pdfgen import canvas

# مكتبات قاعدة البيانات وجوجل
import asyncpg
from aiohttp import web
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# استيرادات Selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, StaleElementReferenceException

# --- الإعدادات ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
HEROKU_BASE_URL = "https://discord-scrap-f0a38eba4b1c.herokuapp.com" 
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# إعدادات جوجل درايف 
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{HEROKU_BASE_URL}/auth/google/callback"]
        }
    }
else:
    client_config = None

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# قواميس التتبع السحابية
expiration_times = {}
auth_sessions = {}
pending_logins = {} # لحفظ رسالة الديسكورد وتحديثها لاحقاً

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

db_pool = None

# --- إعدادات قاعدة البيانات ---
async def init_db():
    global db_pool
    if not DATABASE_URL:
        print("[WARNING] DATABASE_URL not found. Database features will be disabled.")
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_tokens (
                    discord_id BIGINT PRIMARY KEY,
                    token TEXT,
                    refresh_token TEXT,
                    token_uri TEXT,
                    client_id TEXT,
                    client_secret TEXT,
                    scopes TEXT,
                    google_email TEXT,
                    files_extracted INTEGER DEFAULT 0,
                    files_uploaded INTEGER DEFAULT 0
                )
            """)
            
            # تحديث الجدول إذا كان قديماً
            try:
                await conn.execute("ALTER TABLE user_tokens ADD COLUMN google_email TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE user_tokens ADD COLUMN files_extracted INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE user_tokens ADD COLUMN files_uploaded INTEGER DEFAULT 0")
            except Exception:
                pass
            
        print("[INFO] Database connected and table verified.")
    except Exception as e:
        print(f"[ERROR] Database connection failed: {e}")

# --- دوال جوجل درايف ---
def get_user_credentials(token_data):
    return Credentials(
        token=token_data['token'],
        refresh_token=token_data['refresh_token'],
        token_uri=token_data['token_uri'],
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=json.loads(token_data['scopes'])
    )

def upload_to_drive_sync(creds, file_path, filename):
    try:
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': filename}
        media = MediaFileUpload(file_path, mimetype='application/pdf', resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        return {"success": True, "link": file.get('webViewLink')}
    except Exception as e:
        return {"success": False, "error": str(e)}

# --- إعدادات خادم الويب (مسارات الملفات وتسجيل الدخول) ---
async def download_file_handler(request):
    folder_id = request.match_info.get('folder_id')
    filename = request.match_info.get('filename')
    
    decoded_filename = urllib.parse.unquote(filename)
    file_path = os.path.join(DOWNLOADS_DIR, folder_id, decoded_filename)
    
    if not os.path.exists(file_path):
        return web.Response(status=404, text="❌ الملف غير موجود أو تم حذفه لانتهاء صلاحيته.")
    
    return web.FileResponse(file_path, headers={'Content-Disposition': f'attachment; filename="{decoded_filename}"'})

async def auth_login_handler(request):
    discord_id = request.match_info.get('discord_id')
    if not client_config:
        return web.Response(text="❌ Google OAuth is not configured on the server.")
    
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = f"{HEROKU_BASE_URL}/auth/google/callback"
    
    auth_url, state = flow.authorization_url(prompt='consent', access_type='offline', state=discord_id)
    
    if hasattr(flow, 'code_verifier'):
        auth_sessions[state] = flow.code_verifier
        
    return web.HTTPFound(auth_url)

async def auth_callback_handler(request):
    state = request.query.get('state')
    code = request.query.get('code')
    
    if not state or not code:
        return web.Response(text="❌ فشل تسجيل الدخول: بيانات مفقودة.", status=400)
    
    try:
        flow = Flow.from_client_config(client_config, scopes=SCOPES)
        flow.redirect_uri = f"{HEROKU_BASE_URL}/auth/google/callback"
        
        if state in auth_sessions:
            flow.code_verifier = auth_sessions.pop(state)
            
        flow.fetch_token(code=code)
        creds = flow.credentials
        
        oauth2_service = build('oauth2', 'v2', credentials=creds)
        user_info = oauth2_service.userinfo().get().execute()
        google_email = user_info.get('email', 'غير معروف')
        
        discord_id = int(state)
        scopes_json = json.dumps(creds.scopes)
        
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_tokens (discord_id, token, refresh_token, token_uri, client_id, client_secret, scopes, google_email)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (discord_id) DO UPDATE SET
                    token = EXCLUDED.token,
                    refresh_token = COALESCE(EXCLUDED.refresh_token, user_tokens.refresh_token),
                    token_uri = EXCLUDED.token_uri,
                    client_id = EXCLUDED.client_id,
                    client_secret = EXCLUDED.client_secret,
                    scopes = EXCLUDED.scopes,
                    google_email = EXCLUDED.google_email
            """, discord_id, creds.token, creds.refresh_token, creds.token_uri, creds.client_id, creds.client_secret, scopes_json, google_email)
            
        # تحديث رسالة الديسكورد الأصلية لتقول "تم تسجيل الدخول"
        interaction = pending_logins.pop(discord_id, None)
        if interaction:
            try:
                success_embed = discord.Embed(
                    title="✅ تم تسجيل الدخول بنجاح!", 
                    description=f"تم ربط حساب جوجل (`{google_email}`) بحسابك في ديسكورد.\nيمكنك الآن استخدام البوت لرفع الملفات مباشرة، أو استخدم أمر `/profile` لعرض ملفك.", 
                    color=discord.Color.green()
                )
                await interaction.edit_original_response(embed=success_embed, view=None)
            except Exception as e:
                print(f"[WARNING] Could not update discord message: {e}")

        success_html = """
        <html>
            <head><title>Success</title><meta charset="utf-8"></head>
            <body style="display:flex; justify-content:center; align-items:center; height:100vh; background-color:#2f3136; color:white; font-family:sans-serif; text-align:center;">
                <div>
                    <h1 style="color:#43b581;">✅ تم التسجيل بنجاح!</h1>
                    <p>تم ربط حساب جوجل درايف بحساب الديسكورد الخاص بك.</p>
                    <p>يمكنك إغلاق هذه الصفحة والعودة للديسكورد الآن.</p>
                </div>
            </body>
        </html>
        """
        return web.Response(text=success_html, content_type="text/html", charset="utf-8")
    except Exception as e:
        return web.Response(text=f"❌ حدث خطأ أثناء ربط الحساب: {e}", content_type="text/html", charset="utf-8")

async def health_check_handler(request):
    return web.Response(text="✅ خادم الملفات ونظام التسجيل يعمل بنجاح!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check_handler)
    app.router.add_get(f'/{DOWNLOADS_DIR}/{{folder_id}}/{{filename}}', download_file_handler)
    app.router.add_get('/auth/login/{discord_id}', auth_login_handler)
    app.router.add_get('/auth/google/callback', auth_callback_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"[INFO] Web server started on port {port}")

# --- دالة الحذف الذكية ---
async def background_cleanup_task(file_path):
    while file_path in expiration_times:
        current_time = time.time()
        if current_time >= expiration_times[file_path]:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    print(f"[INFO] 🗑️ تم حذف الملف تلقائياً: {file_path}")
                    
                    folder_path = os.path.dirname(file_path)
                    if os.path.exists(folder_path) and not os.listdir(folder_path):
                        os.rmdir(folder_path)
                except Exception:
                    pass
            expiration_times.pop(file_path, None)
            break
        await asyncio.sleep(30)

# --- كلاس الأزرار ---
class FileManagementView(ui.View):
    def __init__(self, file_path, download_url, display_name):
        super().__init__(timeout=None)
        self.file_path = file_path
        self.download_url = download_url
        self.display_name = display_name
        
        self.add_item(ui.Button(label="تحميل الملف", url=self.download_url, style=discord.ButtonStyle.link, emoji="📥", row=0))

    @ui.button(label="تمديد الوقت (+10د)", style=discord.ButtonStyle.primary, emoji="⏳", row=0)
    async def extend_timer(self, interaction: discord.Interaction, button: ui.Button):
        if self.file_path in expiration_times:
            expiration_times[self.file_path] += 600
            new_expire_time = int(expiration_times[self.file_path])
            
            embed = interaction.message.embeds[0]
            embed.set_footer(text=f"⏳ تم التمديد بنجاح! سيتم الحذف عند: {time.strftime('%H:%M:%S', time.localtime(new_expire_time))}")
            
            await interaction.response.edit_message(embed=embed)
        else:
            await interaction.response.send_message("❌ عذراً، يبدو أن الملف قد تم حذفه بالفعل.", ephemeral=True)

    @ui.button(label="حذف الآن", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_now(self, interaction: discord.Interaction, button: ui.Button):
        if os.path.exists(self.file_path):
            try: 
                os.remove(self.file_path)
                folder_path = os.path.dirname(self.file_path)
                if os.path.exists(folder_path) and not os.listdir(folder_path):
                    os.rmdir(folder_path)
            except Exception: 
                pass
            
            expiration_times.pop(self.file_path, None)
            
            final_embed = discord.Embed(
                title="🗑️ تم حذف الملف", 
                description=f"تم حذف الملف `{self.display_name}` بناءً على طلبك لتوفير المساحة.", 
                color=discord.Color.light_grey()
            )
            await interaction.response.edit_message(embed=final_embed, view=None)
        else:
            await interaction.response.send_message("❌ الملف غير موجود بالفعل.", ephemeral=True)

# --- إعدادات سيلينيوم والاستخراج ---
def init_driver(scale_factor: float, window_size: str):
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    
    chrome_options.add_argument(f"--window-size={window_size}")
    chrome_options.add_argument(f"--force-device-scale-factor={scale_factor}") 
    chrome_options.add_argument("--high-dpi-support=1")
    
    chrome_options.add_argument("--disable-site-isolation-trials") 
    chrome_options.add_argument("--disable-application-cache")
    chrome_options.add_argument("--js-flags=--expose-gc")
    
    chrome_options.binary_location = os.environ.get("GOOGLE_CHROME_BIN")

    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(60)
        return driver
    except WebDriverException as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver: {e}")
        return None

def extract_pdf_via_canvas(url: str, output_id: str, progress_state: dict, img_format: str, img_quality: float, img_ext: str, scale_factor: float, window_size: str, max_dim: int, img_sleep: float, scroll_sleep: float):
    driver = init_driver(scale_factor, window_size)
    if not driver:
        progress_state["error"] = "فشل في تشغيل المتصفح."
        return {"success": False, "error": progress_state["error"]}
    
    processed_urls = set()
    saved_images_paths = []
    
    user_dir = os.path.join(DOWNLOADS_DIR, output_id)
    os.makedirs(user_dir, exist_ok=True)
    
    temp_dir = os.path.join(user_dir, "temp_images")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        progress_state["status"] = "جاري فتح الصفحة وجلب المعلومات..."
        driver.get(url)
        
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, 'img')))
        time.sleep(2) 
        
        raw_title = driver.title.replace(" - Google Drive", "").strip()
        clean_title = re.sub(r'[\\/*?:"<>|]', "", raw_title)
        
        if not clean_title:
            clean_title = f"drive_doc_{output_id}"
            
        if not clean_title.lower().endswith(".pdf"):
            clean_title += ".pdf"
            
        progress_state["title"] = clean_title
        progress_state["status"] = "جاري سحب الصفحات..."
        
        scroll_attempts = 0
        max_attempts = 2000
        empty_scrolls = 0
        progress_state["start_time"] = time.time()
        
        while scroll_attempts < max_attempts:
            img_elements = driver.find_elements(By.TAG_NAME, 'img')
            extracted_in_this_pass = False
            
            for img in img_elements:
                try:
                    src = img.get_attribute('src')
                    check_url_string = "blob:https://drive.google.com/"
                    
                    if src and src.startswith(check_url_string) and src not in processed_urls:
                        driver.execute_script("arguments[0].scrollIntoView(true);", img)
                        time.sleep(img_sleep) 
                        
                        b64_data = driver.execute_script("""
                            var img = arguments[0];
                            var format = arguments[1];
                            var quality = arguments[2];
                            var max_limit = arguments[3];
                            
                            if (img.naturalWidth === 0) return null;
                            
                            var w = img.naturalWidth;
                            var h = img.naturalHeight;
                            
                            if (w > max_limit || h > max_limit) {
                                var ratio = Math.min(max_limit / w, max_limit / h);
                                w = Math.round(w * ratio);
                                h = Math.round(h * ratio);
                            }
                            
                            var canvasElement = document.createElement("canvas");
                            var con = canvasElement.getContext("2d");
                            canvasElement.width = w;
                            canvasElement.height = h;
                            
                            con.fillStyle = "#FFFFFF";
                            con.fillRect(0, 0, w, h);
                            con.drawImage(img, 0, 0, w, h);
                            
                            var data = canvasElement.toDataURL(format, quality);
                            
                            con.clearRect(0, 0, w, h);
                            canvasElement.width = 0;
                            canvasElement.height = 0;
                            canvasElement = null;
                            
                            return data;
                        """, img, img_format, img_quality, max_dim)
                        
                        if b64_data:
                            b64_string = b64_data.split(",")[1] if "," in b64_data else b64_data
                            img_bytes = base64.b64decode(b64_string)
                            
                            page_path = os.path.join(temp_dir, f"page_{len(saved_images_paths):04d}.{img_ext}")
                            with open(page_path, "wb") as f:
                                f.write(img_bytes)
                                
                            saved_images_paths.append(page_path)
                            processed_urls.add(src)
                            
                            progress_state["pages"] = len(saved_images_paths)
                            extracted_in_this_pass = True
                            empty_scrolls = 0
                            
                            del b64_data, b64_string, img_bytes
                            gc.collect() 
                            break 
                            
                except StaleElementReferenceException:
                    break
            
            if not extracted_in_this_pass:
                driver.execute_script("window.scrollBy(0, window.innerHeight);")
                time.sleep(scroll_sleep)
                empty_scrolls += 1
                driver.execute_script("window.gc && window.gc();") 
            else:
                empty_scrolls = 0
            
            if empty_scrolls >= 6:
                break
                
            scroll_attempts += 1

        if not saved_images_paths:
            progress_state["error"] = "لم يتم العثور على أي محتوى مطابق."
            return {"success": False, "error": progress_state["error"]}
        
        progress_state["extracting"] = False 
        progress_state["status"] = "جاري تجميع الملف وتحويله لـ PDF (بدون استهلاك للذاكرة)..."
        
        pdf_path = os.path.join(user_dir, clean_title)
        
        c = canvas.Canvas(pdf_path)
        for img_path in saved_images_paths:
            try:
                with Image.open(img_path) as img:
                    w, h = img.size
                
                c.setPageSize((w, h))
                c.drawImage(img_path, 0, 0, width=w, height=h)
                c.showPage()
                gc.collect()
            except Exception as e:
                print(f"[WARNING] Skipping image: {e}")
                
        c.save() 
        
        return {
            "success": True, 
            "file_path": pdf_path, 
            "filename": clean_title, 
            "folder_id": output_id, 
            "display_name": clean_title
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        progress_state["done"] = True
        if driver:
            driver.quit()
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
    await init_db()
    bot.loop.create_task(start_web_server())
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Sync error: {e}")

def create_progress_bar(current, total, length=15):
    if total is None or total <= 0:
        return "[▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓] (غير محدد)"
    
    percent = min(int((current / total) * 100), 100)
    filled_length = int(length * current // total)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percent}%"

# --- أوامر تسجيل الدخول، الخروج، والملف الشخصي ---

@bot.tree.command(name="login", description="تسجيل الدخول لحساب جوجل لرفع الملفات مباشرة للدرايف")
async def login_command(interaction: discord.Interaction):
    if not GOOGLE_CLIENT_ID:
        await interaction.response.send_message("❌ ميزة الرفع السحابي غير مفعلة في السيرفر حالياً.", ephemeral=True)
        return
    
    # حفظ التفاعل لتحديث الرسالة لاحقاً
    pending_logins[interaction.user.id] = interaction
    
    login_url = f"{HEROKU_BASE_URL}/auth/login/{interaction.user.id}"
    
    embed = discord.Embed(
        title="🔐 تسجيل الدخول لجوجل درايف", 
        description="اضغط على الزر أدناه لتسجيل الدخول بأمان وتخويل البوت برفع الملفات لحسابك.", 
        color=discord.Color.blue()
    )
    
    view = ui.View()
    view.add_item(ui.Button(label="تسجيل الدخول هنا", url=login_url, style=discord.ButtonStyle.link))
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="logout", description="تسجيل الخروج وحذف بيانات ربط جوجل من البوت")
async def logout_command(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❌ قاعدة البيانات غير متصلة.", ephemeral=True)
        return
    
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM user_tokens WHERE discord_id = $1", interaction.user.id)
        
        if result == "DELETE 1":
            await interaction.response.send_message("✅ تم تسجيل الخروج بنجاح. تم حذف جميع مفاتيح الربط الخاصة بك.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ حسابك غير مربوط بجوجل أساساً.", ephemeral=True)

@bot.tree.command(name="profile", description="عرض الملف الشخصي وإحصائيات الاستخدام الخاصة بك")
async def profile_command(interaction: discord.Interaction):
    if not db_pool:
        await interaction.response.send_message("❌ قاعدة البيانات غير متصلة.", ephemeral=True)
        return
        
    async with db_pool.acquire() as conn:
        user_data = await conn.fetchrow("SELECT * FROM user_tokens WHERE discord_id = $1", interaction.user.id)
        
    if not user_data:
        await interaction.response.send_message("❌ أنت غير مسجل! استخدم أمر `/login` لربط حساب جوجل وإنشاء ملف شخصي.", ephemeral=True)
        return
        
    email = user_data.get('google_email') or "غير متوفر"
    extracted = user_data.get('files_extracted') or 0
    uploaded = user_data.get('files_uploaded') or 0
    
    avatar_url = interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url
    
    embed = discord.Embed(title="👤 الملف الشخصي والإحصائيات", color=discord.Color.gold())
    embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="الاسم في ديسكورد:", value=f"`{interaction.user.name}`", inline=True)
    embed.add_field(name="حساب جوجل المربوط:", value=f"`{email}`", inline=False)
    embed.add_field(name="📥 الملفات المستخرجة:", value=f"`{extracted}` ملف", inline=True)
    embed.add_field(name="☁️ المرفوعة لدرايف:", value=f"`{uploaded}` ملف", inline=True)
    embed.set_footer(text="شكراً لثقتك واستخدامك للبوت!")
    
    await interaction.response.send_message(embed=embed)

# --- أمر السلاش الرئيسي ---
@bot.tree.command(name="fetchpdf", description="استخراج ملف من جوجل درايف")
@app_commands.describe(
    url="رابط جوجل درايف للملف",
    expected_pages="عدد الصفحات (اختياري)",
    quality="اختر جودة الصور المستخرجة",
    speed="اختر سرعة عملية السحب",
    save_to_drive="هل ترغب برفع الملف مباشرة لحسابك في درايف؟ (يجب استخدام أمر /login أولاً)"
)
async def fetch_pdf(
    interaction: discord.Interaction, 
    url: str, 
    expected_pages: int = None,
    quality: Literal["عالية (دقة ممتازة - حجم كبير)", "متوسطة (موصى به - متوازن)", "منخفضة (سريعة - حجم صغير)"] = "متوسطة (موصى به - متوازن)",
    speed: Literal["ممتازة/بطيئة (تضمن عدم ضياع الصفحات)", "متوسطة (توازن بين الأمان والوقت)", "سريعة جداً (قد تفقد بعض الصفحات وتكون مشوشة)"] = "ممتازة/بطيئة (تضمن عدم ضياع الصفحات)",
    save_to_drive: bool = False
):
    await interaction.response.defer(ephemeral=False)
    
    user_creds_data = None
    if save_to_drive:
        if not db_pool:
            await interaction.edit_original_response(content="❌ عذراً، ميزة الرفع السحابي معطلة حالياً.")
            return
            
        async with db_pool.acquire() as conn:
            user_creds_data = await conn.fetchrow("SELECT * FROM user_tokens WHERE discord_id = $1", interaction.user.id)
            
        if not user_creds_data:
            await interaction.edit_original_response(content="❌ **يجب عليك تسجيل الدخول أولاً!** استخدم أمر `/login`.")
            return

    # إعدادات الجودة والتحجيم
    if "عالية" in quality:
        img_format = "image/jpeg"
        img_quality = 1.0
        img_ext = "jpg"
        scale_factor = 2.0
        window_size = "1920,1080"
        max_dim = 3500
    elif "منخفضة" in quality:
        img_format = "image/jpeg"
        img_quality = 0.5
        img_ext = "jpg"
        scale_factor = 1.0
        window_size = "800,600"
        max_dim = 1500
    else:
        img_format = "image/jpeg"
        img_quality = 0.8
        img_ext = "jpg"
        scale_factor = 1.5
        window_size = "1280,720"
        max_dim = 2500
        
    # إعدادات السرعة والتمرير
    if "بطيئة" in speed:
        img_sleep = 1.2
        scroll_sleep = 1.5
    elif "سريعة" in speed:
        img_sleep = 0.3
        scroll_sleep = 0.8
    else:
        img_sleep = 0.8
        scroll_sleep = 1.2

    progress_state = {
        "status": "تهيئة...",
        "pages": 0,
        "title": "جاري التعرف...",
        "start_time": None,
        "extracting": True,
        "done": False,
        "error": None
    }
    
    task = asyncio.create_task(
        asyncio.to_thread(extract_pdf_via_canvas, url, str(interaction.id), progress_state, img_format, img_quality, img_ext, scale_factor, window_size, max_dim, img_sleep, scroll_sleep)
    )
    
    original_response = await interaction.original_response()
    try:
        current_message = await interaction.channel.fetch_message(original_response.id)
    except Exception:
        current_message = original_response
        
    message_creation_time = time.time()
    
    while not task.done():
        status_msg = progress_state["status"]
        current_pages = progress_state["pages"]
        title = progress_state["title"]
        start_time = progress_state["start_time"]
        
        p_bar = create_progress_bar(current_pages, expected_pages)
        pages_text = f"{current_pages} / {expected_pages}" if expected_pages else f"{current_pages} (لم يتم إدخال الإجمالي)"
        
        if progress_state.get("extracting", True):
            eta_text = "جاري الحساب..."
            if start_time and current_pages > 0 and expected_pages:
                eta_seconds = int(((time.time() - start_time) / current_pages) * max(0, expected_pages - current_pages))
                mins, secs = divmod(eta_seconds, 60)
                
                if mins > 0:
                    eta_text = f"حوالي {mins} دقيقة و {secs} ثانية"
                else:
                    eta_text = f"حوالي {secs} ثانية"
            elif not expected_pages:
                eta_text = "غير معروف"
        else:
            eta_text = "يرجى الانتظار، جاري تجهيز الملف للتحميل ⏳"
        
        embed = discord.Embed(title="📥 جاري استخراج الملف", color=discord.Color.blue())
        embed.add_field(name="الاسم الأصلي:", value=f"`{title}`", inline=False)
        embed.add_field(name="الحالة:", value=f"**{status_msg}**", inline=False)
        embed.add_field(name="التقدم:", value=f"`{p_bar}`", inline=False)
        embed.add_field(name="الصفحات المسحوبة:", value=f"`{pages_text}`", inline=True)
        embed.add_field(name="الوقت المقدر (ETA):", value=f"`{eta_text}`", inline=True)
        embed.set_footer(text=f"⚙️ الجودة: {quality.split(' ')[0]} | السرعة: {speed.split(' ')[0]}")
        
        time_elapsed_since_creation = time.time() - message_creation_time
        if time_elapsed_since_creation >= 840: 
            try:
                new_message = await interaction.channel.send(embed=embed)
                await current_message.delete()
                current_message = new_message
                message_creation_time = time.time()
            except Exception:
                pass
        else:
            try:
                await current_message.edit(embed=embed)
            except Exception:
                pass 
                
        await asyncio.sleep(5) 
    
    result = await task
    
    if result.get("success"):
        file_path = result["file_path"]
        filename = result["filename"]
        folder_id = result["folder_id"]
        display_name = result["display_name"]
        
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        if db_pool:
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE user_tokens SET files_extracted = files_extracted + 1 WHERE discord_id = $1", interaction.user.id)
            except Exception:
                pass

        final_embed = discord.Embed(
            title="✅ اكتملت المعالجة!", 
            description=f"تم استخراج جميع الصفحات لملف **{display_name}**.",
            color=discord.Color.green()
        )
        final_embed.add_field(name="حجم الملف:", value=f"`{file_size_mb:.2f} MB`", inline=True)
        final_embed.add_field(name="عدد الصفحات:", value=f"`{progress_state['pages']}`", inline=True)
        final_embed.add_field(name="\u200B", value="\u200B", inline=True)
        
        upload_result = {}
        
        if save_to_drive and user_creds_data:
            uploading_embed = discord.Embed(title="☁️ جاري الرفع لجوجل درايف...", color=discord.Color.gold())
            await current_message.edit(embed=uploading_embed)
            
            try:
                creds = get_user_credentials(user_creds_data)
                upload_result = await asyncio.to_thread(upload_to_drive_sync, creds, file_path, display_name)
                
                if upload_result.get("success"):
                    final_embed.add_field(name="☁️ تم الرفع لحسابك بنجاح!", value=f"[اضغط هنا لفتح الملف في جوجل درايف الخاص بك]({upload_result['link']})", inline=False)
                    final_embed.set_footer(text="تم الحفظ بنجاح في حساب جوجل درايف المربوط.")
                    
                    if db_pool:
                        try:
                            async with db_pool.acquire() as conn:
                                await conn.execute("UPDATE user_tokens SET files_uploaded = files_uploaded + 1 WHERE discord_id = $1", interaction.user.id)
                        except Exception:
                            pass
                    
                    expiration_times[file_path] = time.time() + 5 
                    asyncio.create_task(background_cleanup_task(file_path))
                    await current_message.edit(embed=final_embed, view=None)
                else:
                    final_embed.add_field(name="⚠️ فشل الرفع للدرايف:", value=f"```\n{upload_result['error']}\n```", inline=False)
            except Exception as e:
                final_embed.add_field(name="⚠️ حدث خطأ غير متوقع أثناء الرفع:", value=str(e), inline=False)

        if not save_to_drive or not upload_result.get("success"):
            encoded_filename = urllib.parse.quote(filename)
            direct_link = f"{HEROKU_BASE_URL}/{DOWNLOADS_DIR}/{folder_id}/{encoded_filename}"
            expiration_times[file_path] = time.time() + 900 
            
            final_embed.add_field(name="💡 معلومة مفيدة:", value="يتيح لك زر **(تمديد الوقت)** زيادة وقت بقاء الملف في السيرفر لمدة 10 دقائق إضافية.", inline=False)
            final_embed.set_footer(text="⚠️ سيتم حذف الملف تلقائياً من السيرفر بعد 15 دقيقة.")
            
            view = FileManagementView(file_path, direct_link, display_name)
            await current_message.edit(embed=final_embed, view=view)
            asyncio.create_task(background_cleanup_task(file_path))
            
    else:
        err_embed = discord.Embed(title="❌ فشل العملية", description=result.get('error'), color=discord.Color.red())
        await current_message.edit(embed=err_embed)

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("[CRITICAL ERROR] Token not found in environment variables.")
