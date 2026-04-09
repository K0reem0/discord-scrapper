import discord
from discord.ext import commands
import asyncio
import os
import time
import base64
import re
from io import BytesIO
from PIL import Image

# استيرادات خادم الويب
from aiohttp import web

# استيرادات Selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service 
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
    chrome_options.binary_location = os.environ.get("GOOGLE_CHROME_BIN")

    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(60)
        return driver
    except WebDriverException as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver: {e}")
        return None

def extract_pdf_via_canvas(url: str, output_id: str, progress_state: dict):
    """سكربت استخراج الصور مع تحديث الحالة المشتركة لـ Progress Bar"""
    driver = init_driver()
    if not driver:
        progress_state["error"] = "فشل في تشغيل المتصفح."
        return {"success": False, "error": progress_state["error"]}

    images_base64 = []
    processed_urls = set()
    
    try:
        progress_state["status"] = "جاري فتح الصفحة وجلب المعلومات..."
        driver.get(url)
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, 'img'))
        )
        
        # استخراج اسم الملف من عنوان الصفحة
        time.sleep(2) # انتظار خفيف لتحميل العنوان
        raw_title = driver.title.replace(" - Google Drive", "").strip()
        
        # تنظيف الاسم من الرموز الممنوعة في أسماء الملفات
        clean_title = re.sub(r'[\\/*?:"<>|]', "", raw_title)
        if not clean_title:
            clean_title = f"drive_doc_{output_id}"
            
        if not clean_title.lower().endswith(".pdf"):
            clean_title += ".pdf"
            
        progress_state["title"] = clean_title
        progress_state["status"] = "جاري سحب الصفحات..."
        
        scroll_attempts = 0
        max_attempts = 1500
        empty_scrolls = 0
        
        while scroll_attempts < max_attempts:
            img_elements = driver.find_elements(By.TAG_NAME, 'img')
            extracted_in_this_pass = False
            
            for img in img_elements:
                try:
                    src = img.get_attribute('src')
                    check_url_string = "blob:https://drive.google.com/"
                    
                    if src and src.startswith(check_url_string) and src not in processed_urls:
                        driver.execute_script("arguments[0].scrollIntoView(true);", img)
                        time.sleep(1) 
                        
                        b64_data = driver.execute_script("""
                            var img = arguments[0];
                            if (img.naturalWidth === 0) return null;
                            
                            var canvasElement = document.createElement("canvas");
                            var con = canvasElement.getContext("2d");
                            canvasElement.width = img.naturalWidth;
                            canvasElement.height = img.naturalHeight;
                            
                            con.drawImage(img, 0, 0, img.naturalWidth, img.naturalHeight);
                            return canvasElement.toDataURL('image/png');
                        """, img)
                        
                        if b64_data:
                            images_base64.append(b64_data)
                            processed_urls.add(src)
                            
                            # تحديث عداد الصفحات للشريط
                            progress_state["pages"] = len(images_base64)
                            
                            extracted_in_this_pass = True
                            empty_scrolls = 0
                            break 
                            
                except StaleElementReferenceException:
                    break
            
            if not extracted_in_this_pass:
                driver.execute_script("window.scrollBy(0, window.innerHeight);")
                time.sleep(1.5)
                empty_scrolls += 1
            
            if empty_scrolls >= 6:
                break
                
            scroll_attempts += 1

        if not images_base64:
            progress_state["error"] = "لم يتم العثور على أي محتوى مطابق."
            return {"success": False, "error": progress_state["error"]}
        
        progress_state["status"] = "جاري تجميع الملف وتحويله لـ PDF (قد يستغرق لحظات)..."
        
        pil_images = []
        for index, b64 in enumerate(images_base64):
            if "," in b64:
                b64_string = b64.split(",")[1]
            else:
                continue
            img_bytes = base64.b64decode(b64_string)
            img_obj = Image.open(BytesIO(img_bytes)).convert('RGB')
            pil_images.append(img_obj)

        # إضافة المعرف للاسم لمنع التعارض إذا تم تحميل ملفين بنفس الاسم بوقت واحد
        safe_filename = f"{output_id}_{clean_title}"
        pdf_path = os.path.join(os.getcwd(), DOWNLOADS_DIR, safe_filename)
        
        pil_images[0].save(
            pdf_path, 
            save_all=True, 
            append_images=pil_images[1:], 
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
    """دالة لإنشاء شكل شريط التقدم"""
    if total is None or total <= 0:
        return "[▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓] (غير محدد)"
    
    percent = min(int((current / total) * 100), 100)
    filled_length = int(length * current // total)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percent}%"

@bot.tree.command(name="fetchpdf", description="استخراج ملف من جوجل درايف وإعطاء رابط تحميل مباشر")
@discord.app_commands.describe(
    url="رابط جوجل درايف للملف",
    expected_pages="عدد الصفحات (اختياري) للحصول على شريط تقدم دقيق"
)
async def fetch_pdf(interaction: discord.Interaction, url: str, expected_pages: int = None):
    
    await interaction.response.defer(ephemeral=False)
    
    # قاموس لتتبع حالة العملية ومشاركتها بين مسار الديسكورد ومسار السيلينيوم
    progress_state = {
        "status": "تهيئة المتصفح...",
        "pages": 0,
        "title": "جاري التعرف على الملف...",
        "done": False,
        "error": None
    }
    
    # تشغيل عملية السحب في مسار خلفي
    task = asyncio.create_task(
        asyncio.to_thread(extract_pdf_via_canvas, url, str(interaction.id), progress_state)
    )
    
    # حلقة التحديث الحي لرسالة الديسكورد (كل 5 ثواني)
    while not task.done():
        status_msg = progress_state["status"]
        current_pages = progress_state["pages"]
        title = progress_state["title"]
        
        p_bar = create_progress_bar(current_pages, expected_pages)
        pages_text = f"{current_pages} / {expected_pages}" if expected_pages else f"{current_pages} (لم يتم إدخال الإجمالي)"
        
        embed = discord.Embed(title="📥 جاري استخراج الملف", color=discord.Color.blue())
        embed.add_field(name="الاسم الأصلي:", value=f"`{title}`", inline=False)
        embed.add_field(name="الحالة:", value=f"**{status_msg}**", inline=False)
        embed.add_field(name="التقدم:", value=f"`{p_bar}`", inline=False)
        embed.add_field(name="الصفحات المسحوبة:", value=f"`{pages_text}`", inline=False)
        
        try:
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception:
            pass # تجاهل الأخطاء إذا حصل تأخير في الشبكة
        
        await asyncio.sleep(5) # التحديث كل 5 ثواني
    
    # عند انتهاء المهمة:
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
        final_embed.set_footer(text="⚠️ سيتم حذف هذا الرابط والملف تلقائياً بعد 15 دقيقة.")
        
        await interaction.edit_original_response(embed=final_embed)
        
        asyncio.create_task(delete_file_after_delay(file_path, 900))
        
    else:
        err_embed = discord.Embed(title="❌ فشل العملية", description=result.get('error'), color=discord.Color.red())
        await interaction.edit_original_response(embed=err_embed)

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("[CRITICAL ERROR] Token not found.")
