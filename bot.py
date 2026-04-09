import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import time
import base64
import re
import shutil
import gc  # 🔥 [جديد] مكتبة لتنظيف الرام إجبارياً
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

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- إعدادات خادم الويب ---
async def download_file_handler(request):
    filename = request.match_info.get('filename')
    file_path = os.path.join(DOWNLOADS_DIR, filename)
    
    if not os.path.exists(file_path):
        return web.Response(status=404, text="❌ الملف غير موجود أو تم حذفه لانتهاء صلاحيته.")
    
    return web.FileResponse(file_path, headers={
        'Content-Disposition': f'attachment; filename="{filename}"'
    })

async def health_check_handler(request):
    return web.Response(text="✅ خادم الملفات يعمل بنجاح!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check_handler)
    app.router.add_get(f'/{DOWNLOADS_DIR}/{{filename}}', download_file_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"[INFO] Web server started on port {port}")

# --- إعدادات Selenium ---
def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    # 🔥 [جديد] أوامر تقييد استهلاك الذاكرة في Chrome
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

def extract_pdf_via_canvas(url: str, output_id: str, progress_state: dict, img_quality: float, img_sleep: float, scroll_sleep: float):
    driver = init_driver()
    if not driver:
        progress_state["error"] = "فشل في تشغيل المتصفح."
        return {"success": False, "error": progress_state["error"]}

    processed_urls = set()
    saved_images_paths = []
    
    temp_dir = os.path.join(DOWNLOADS_DIR, f"temp_{output_id}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        progress_state["status"] = "جاري فتح الصفحة وجلب المعلومات..."
        driver.get(url)
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, 'img'))
        )
        
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
                            var quality = arguments[1];
                            if (img.naturalWidth === 0) return null;
                            
                            var canvasElement = document.createElement("canvas");
                            var con = canvasElement.getContext("2d");
                            canvasElement.width = img.naturalWidth;
                            canvasElement.height = img.naturalHeight;
                            
                            con.drawImage(img, 0, 0, img.naturalWidth, img.naturalHeight);
                            var data = canvasElement.toDataURL('image/jpeg', quality);
                            
                            // 🔥 [جديد] تدمير الكانفاس فوراً لتفريغ الرام في المتصفح
                            con.clearRect(0, 0, canvasElement.width, canvasElement.height);
                            canvasElement.width = 0;
                            canvasElement.height = 0;
                            canvasElement = null;
                            
                            return data;
                        """, img, img_quality)
                        
                        if b64_data:
                            b64_string = b64_data.split(",")[1] if "," in b64_data else b64_data
                            img_bytes = base64.b64decode(b64_string)
                            
                            page_path = os.path.join(temp_dir, f"page_{len(saved_images_paths):04d}.jpg")
                            with open(page_path, "wb") as f:
                                f.write(img_bytes)
                                
                            saved_images_paths.append(page_path)
                            processed_urls.add(src)
                            
                            progress_state["pages"] = len(saved_images_paths)
                            extracted_in_this_pass = True
                            empty_scrolls = 0
                            
                            # 🔥 [جديد] الإجبار على مسح المتغيرات الضخمة من الرام
                            del b64_data, b64_string, img_bytes
                            gc.collect() # استدعاء منظف الذاكرة
                            break 
                            
                except StaleElementReferenceException:
                    break
            
            if not extracted_in_this_pass:
                driver.execute_script("window.scrollBy(0, window.innerHeight);")
                time.sleep(scroll_sleep)
                empty_scrolls += 1
                # مسح ذاكرة المتصفح العشوائية أثناء التمرير
                driver.execute_script("window.gc && window.gc();") 
            
            if empty_scrolls >= 6:
                break
                
            scroll_attempts += 1

        if not saved_images_paths:
            progress_state["error"] = "لم يتم العثور على أي محتوى مطابق."
            return {"success": False, "error": progress_state["error"]}
        
        progress_state["status"] = "جاري تجميع الملف وتحويله لـ PDF..."
        
        safe_filename = f"{output_id}_{clean_title}"
        pdf_path = os.path.join(os.getcwd(), DOWNLOADS_DIR, safe_filename)
        
        first_image = Image.open(saved_images_paths[0]).convert('RGB')
        
        def image_generator():
            for img_path in saved_images_paths[1:]:
                with Image.open(img_path) as img:
                    yield img.convert('RGB')

        first_image.save(
            pdf_path, 
            save_all=True, 
            append_images=image_generator(), 
            resolution=100.0
        )
        
        return {"success": True, "file_path": pdf_path, "filename": safe_filename, "display_name": clean_title}

    except Exception as e:
        progress_state["error"] = str(e)
        return {"success": False, "error": str(e)}
    finally:
        progress_state["done"] = True
        if driver:
            driver.quit()
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

async def delete_file_after_delay(file_path: str, delay_seconds: int = 900):
    await asyncio.sleep(delay_seconds)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"[ERROR] فشل الحذف: {e}")

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
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

@bot.tree.command(name="fetchpdf", description="استخراج ملف من جوجل درايف وإعطاء رابط تحميل مباشر")
@app_commands.describe(
    url="رابط جوجل درايف للملف",
    expected_pages="عدد الصفحات (اختياري) للحصول على شريط تقدم ووقت مقدر دقيق",
    quality="اختر جودة الصور المستخرجة",
    speed="اختر سرعة عملية السحب"
)
async def fetch_pdf(
    interaction: discord.Interaction, 
    url: str, 
    expected_pages: int = None,
    quality: Literal["عالية (دقة ممتازة - حجم كبير)", "متوسطة (موصى به - متوازن)", "منخفضة (سريعة - حجم صغير)"] = "متوسطة (موصى به - متوازن)",
    speed: Literal["ممتازة/بطيئة (تضمن عدم ضياع الصفحات)", "متوسطة (توازن بين الأمان والوقت)", "سريعة جداً (قد تفقد بعض الصفحات وتكون مشوشة)"] = "ممتازة/بطيئة (تضمن عدم ضياع الصفحات)"
):
    await interaction.response.defer(ephemeral=False)
    
    if "عالية" in quality:
        img_quality = 0.9
    elif "منخفضة" in quality:
        img_quality = 0.5
    else:
        img_quality = 0.7
        
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
        "status": "تهيئة المتصفح...",
        "pages": 0,
        "title": "جاري التعرف على الملف...",
        "start_time": None,
        "done": False,
        "error": None
    }
    
    task = asyncio.create_task(
        asyncio.to_thread(extract_pdf_via_canvas, url, str(interaction.id), progress_state, img_quality, img_sleep, scroll_sleep)
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
        
        eta_text = "جاري الحساب..."
        if start_time and current_pages > 0:
            elapsed_time = time.time() - start_time
            if expected_pages:
                time_per_page = elapsed_time / current_pages
                pages_left = max(0, expected_pages - current_pages)
                eta_seconds = int(time_per_page * pages_left)
                
                mins, secs = divmod(eta_seconds, 60)
                if mins > 0:
                    eta_text = f"حوالي {mins} دقيقة و {secs} ثانية"
                else:
                    eta_text = f"حوالي {secs} ثانية"
            else:
                eta_text = "غير معروف (لم يتم إدخال الإجمالي)"
        elif not expected_pages:
            eta_text = "غير معروف (لم يتم إدخال الإجمالي)"
        
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
        display_name = result["display_name"]
        
        direct_link = f"{HEROKU_BASE_URL}/{DOWNLOADS_DIR}/{filename}"
        
        final_embed = discord.Embed(
            title="✅ تم تجهيز الملف بنجاح!", 
            description=f"تم استخراج جميع الصفحات لملف **{display_name}**.",
            color=discord.Color.green()
        )
        final_embed.add_field(name="📥 رابط التحميل المباشر:", value=f"[اضغط هنا لتحميل الملف]({direct_link})\nأو انسخ الرابط:\n`{direct_link}`", inline=False)
        final_embed.set_footer(text="⚠️ سيتم حذف هذا الرابط والملف تلقائياً بعد 15 دقيقة لتوفير مساحة السيرفر.")
        
        await current_message.edit(embed=final_embed)
        
        asyncio.create_task(delete_file_after_delay(file_path, 900))
        
    else:
        err_embed = discord.Embed(title="❌ فشل العملية", description=result.get('error'), color=discord.Color.red())
        await current_message.edit(embed=err_embed)

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("[CRITICAL ERROR] Token not found.")
