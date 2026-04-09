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
from typing import Literal
from io import BytesIO
from PIL import Image

# استيرادات خادم الويب
from aiohttp import web

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

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# قاموس لتتبع أوقات حذف الملفات (لتمديد الوقت)
# المفتاح: مسار الملف، القيمة: توقيت الحذف (Timestamp)
expiration_times = {}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- إعدادات خادم الويب ---
async def download_file_handler(request):
    filename = request.match_info.get('filename')
    file_path = os.path.join(DOWNLOADS_DIR, filename)
    if not os.path.exists(file_path):
        return web.Response(status=404, text="❌ الملف غير موجود أو تم حذفه.")
    return web.FileResponse(file_path, headers={'Content-Disposition': f'attachment; filename="{filename}"'})

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="✅ خادم الملفات يعمل!"))
    app.router.add_get(f'/{DOWNLOADS_DIR}/{{filename}}', download_file_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, '0.0.0.0', port).start()

# --- دالة الحذف الذكية (تدعم التمديد) ---
async def background_cleanup_task(file_path):
    """مهمة تعمل في الخلفية تراقب توقيت الحذف"""
    while file_path in expiration_times:
        current_time = time.time()
        if current_time >= expiration_times[file_path]:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"[INFO] 🗑️ تم حذف الملف بعد انتهاء الوقت: {file_path}")
            expiration_times.pop(file_path, None)
            break
        await asyncio.sleep(30) # التحقق كل 30 ثانية لتوفير الموارد

# --- كلاس الأزرار (View) ---
class FileManagementView(ui.View):
    def __init__(self, file_path, download_url, display_name):
        super().__init__(timeout=None) # الأزرار تبقى تعمل حتى يختفي الملف
        self.file_path = file_path
        self.download_url = download_url
        self.display_name = display_name
        
        # إضافة زر التحميل كرابط مباشر
        self.add_item(ui.Button(label="تحميل الملف", url=self.download_url, style=discord.ButtonStyle.link))

    @ui.button(label="تمديد الوقت (10 د)", style=discord.ButtonStyle.primary, emoji="⏳")
    async def extend_timer(self, interaction: discord.Interaction, button: ui.Button):
        if self.file_path in expiration_times:
            expiration_times[self.file_path] += 600 # إضافة 600 ثانية (10 دقائق)
            new_expire_time = int(expiration_times[self.file_path])
            
            embed = interaction.message.embeds[0]
            embed.set_footer(text=f"⏳ تم التمديد! سيتم الحذف عند: {time.strftime('%H:%M:%S', time.localtime(new_expire_time))}")
            
            await interaction.response.edit_message(embed=embed)
        else:
            await interaction.response.send_message("❌ عذراً، يبدو أن الملف قد تم حذفه بالفعل.", ephemeral=True)

    @ui.button(label="حذف الآن", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_now(self, interaction: discord.Interaction, button: ui.Button):
        if os.path.exists(self.file_path):
            os.remove(self.file_path)
            expiration_times.pop(self.file_path, None)
            
            final_embed = discord.Embed(title="🗑️ تم حذف الملف", description=f"تم حذف الملف `{self.display_name}` بناءً على طلبك.", color=discord.Color.light_grey())
            await interaction.response.edit_message(embed=final_embed, view=None)
        else:
            await interaction.response.send_message("❌ الملف غير موجود بالفعل.", ephemeral=True)

# --- دالة Selenium (نفس إصلاحات الرام السابقة) ---
def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-site-isolation-trials") 
    chrome_options.add_argument("--js-flags=--expose-gc")
    chrome_options.binary_location = os.environ.get("GOOGLE_CHROME_BIN")
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(60)
        return driver
    except WebDriverException: return None

def extract_pdf_via_canvas(url, output_id, progress_state, img_quality, img_sleep, scroll_sleep):
    driver = init_driver()
    if not driver: return {"success": False, "error": "خطأ في المتصفح."}
    processed_urls, saved_images_paths = set(), []
    temp_dir = os.path.join(DOWNLOADS_DIR, f"temp_{output_id}")
    os.makedirs(temp_dir, exist_ok=True)
    try:
        driver.get(url)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, 'img')))
        time.sleep(2)
        raw_title = driver.title.replace(" - Google Drive", "").strip()
        clean_title = re.sub(r'[\\/*?:"<>|]', "", raw_title) or f"doc_{output_id}"
        if not clean_title.lower().endswith(".pdf"): clean_title += ".pdf"
        progress_state.update({"title": clean_title, "status": "جاري سحب الصفحات...", "start_time": time.time()})
        
        scroll_attempts, empty_scrolls = 0, 0
        while scroll_attempts < 2000:
            img_elements = driver.find_elements(By.TAG_NAME, 'img')
            extracted = False
            for img in img_elements:
                try:
                    src = img.get_attribute('src')
                    if src and src.startswith("blob:https://drive.google.com/") and src not in processed_urls:
                        driver.execute_script("arguments[0].scrollIntoView(true);", img)
                        time.sleep(img_sleep)
                        b64 = driver.execute_script("var img=arguments[0], q=arguments[1]; var c=document.createElement('canvas'), ctx=c.getContext('2d'); c.width=img.naturalWidth; c.height=img.naturalHeight; ctx.drawImage(img,0,0); var d=c.toDataURL('image/jpeg',q); c.width=0; c.height=0; return d;", img, img_quality)
                        if b64:
                            img_bytes = base64.b64decode(b64.split(",")[1])
                            p = os.path.join(temp_dir, f"p_{len(saved_images_paths):04d}.jpg")
                            with open(p, "wb") as f: f.write(img_bytes)
                            saved_images_paths.append(p)
                            processed_urls.add(src)
                            progress_state["pages"] = len(saved_images_paths)
                            extracted = True
                            gc.collect()
                            break
                except StaleElementReferenceException: break
            if not extracted:
                driver.execute_script("window.scrollBy(0, window.innerHeight);")
                time.sleep(scroll_sleep)
                empty_scrolls += 1
            else: empty_scrolls = 0
            if empty_scrolls >= 6: break
            scroll_attempts += 1
        if not saved_images_paths: return {"success": False, "error": "لم يتم العثور على صور."}
        progress_state["status"] = "جاري تجميع الـ PDF..."
        safe_name = f"{output_id}_{clean_title}"
        pdf_path = os.path.join(os.getcwd(), DOWNLOADS_DIR, safe_name)
        img1 = Image.open(saved_images_paths[0]).convert('RGB')
        def gen():
            for p in saved_images_paths[1:]:
                with Image.open(p) as i: yield i.convert('RGB')
        img1.save(pdf_path, save_all=True, append_images=gen(), resolution=100.0)
        return {"success": True, "file_path": pdf_path, "filename": safe_name, "display_name": clean_title}
    except Exception as e: return {"success": False, "error": str(e)}
    finally:
        progress_state["done"] = True
        if driver: driver.quit()
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir, ignore_errors=True)

# --- أمر السلاش كوماند الرئيسي ---
@bot.tree.command(name="fetchpdf", description="استخراج ملف من جوجل درايف")
async def fetch_pdf(interaction: discord.Interaction, url: str, expected_pages: int = None,
                    quality: Literal["عالية", "متوسطة", "منخفضة"] = "متوسطة",
                    speed: Literal["ممتازة (بطيئة)", "متوسطة", "سريعة جداً"] = "متوسطة"):
    await interaction.response.defer()
    
    q_map = {"عالية": 0.9, "متوسطة": 0.7, "منخفضة": 0.5}
    s_map = {"ممتازة (بطيئة)": (1.2, 1.5), "متوسطة": (0.8, 1.2), "سريعة جداً": (0.3, 0.8)}
    
    img_q = q_map[quality]
    img_s, sc_s = s_map[speed]

    ps = {"status": "تهيئة...", "pages": 0, "title": "جاري الفحص...", "start_time": None, "done": False, "error": None}
    task = asyncio.create_task(asyncio.to_thread(extract_pdf_via_canvas, url, str(interaction.id), ps, img_q, img_s, sc_s))
    
    orig = await interaction.original_response()
    curr_msg = await interaction.channel.fetch_message(orig.id)
    msg_time = time.time()
    
    while not task.done():
        # (نفس منطق التحديث وشريط التقدم والـ 14 دقيقة السابق ذكره في الكود السابق)
        # سيتم تحديث الرسالة بانتظام لتعرض التقدم...
        eta = "جاري الحساب..."
        if ps["start_time"] and ps["pages"] > 0 and expected_pages:
            sec_per_page = (time.time() - ps["start_time"]) / ps["pages"]
            eta = f"{int(sec_per_page * (expected_pages - ps["pages"]))} ثانية"
        
        embed = discord.Embed(title="📥 معالجة الملف", color=discord.Color.blue())
        embed.add_field(name="الاسم:", value=f"`{ps['title']}`")
        embed.add_field(name="التقدم:", value=f"`{ps['pages']} / {expected_pages or '?'}` ({quality}/{speed})")
        embed.add_field(name="الوقت المتبقي:", value=f"`{eta}`")
        
        if time.time() - msg_time >= 840:
            new_m = await interaction.channel.send(embed=embed)
            await curr_msg.delete(); curr_msg = new_m; msg_time = time.time()
        else:
            try: await curr_msg.edit(embed=embed)
            except: pass
        await asyncio.sleep(5)

    res = await task
    if res.get("success"):
        f_path, f_name, d_name = res["file_path"], res["filename"], res["display_name"]
        d_link = f"{HEROKU_BASE_URL}/{DOWNLOADS_DIR}/{f_name}"
        
        # تفعيل نظام تتبع انتهاء الوقت
        expiration_times[f_path] = time.time() + 900 # 15 دقيقة افتراضية
        
        final_embed = discord.Embed(title="✅ الملف جاهز", description=f"ملف: `{d_name}`\nسيتم حذفه تلقائياً بعد 15 دقيقة.", color=discord.Color.green())
        
        # إنشاء الأزرار وتمرير البيانات لها
        view = FileManagementView(f_path, d_link, d_name)
        await curr_msg.edit(embed=final_embed, view=view)
        
        # تشغيل مهمة الحذف في الخلفية
        asyncio.create_task(background_cleanup_task(f_path))
    else:
        await curr_msg.edit(content=f"❌ خطأ: {res.get('error')}", embed=None)

@bot.event
async def on_ready():
    print(f'Bot {bot.user} is online!'); bot.loop.create_task(start_web_server())
    await bot.tree.sync()

if DISCORD_BOT_TOKEN: bot.run(DISCORD_BOT_TOKEN)
