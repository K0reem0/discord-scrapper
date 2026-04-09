import discord
from discord.ext import commands
import asyncio
import os
import time
import base64
import aiohttp
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
# رابط تطبيق هيروكو الخاص بك (بدون شرطة / في النهاية)
HEROKU_BASE_URL = "https://discord-scrap-f0a38eba4b1c.herokuapp.com" 

# إنشاء مجلد التنزيلات إذا لم يكن موجوداً
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- إعدادات خادم الويب (لتقديم الملفات) ---
async def download_file_handler(request):
    """معالج طلبات تحميل الملفات"""
    filename = request.match_info.get('filename')
    file_path = os.path.join(DOWNLOADS_DIR, filename)
    
    if not os.path.exists(file_path):
        return web.Response(status=404, text="❌ الملف غير موجود أو تم حذفه لانتهاء صلاحيته.")
    
    # فرض التحميل المباشر كملف
    return web.FileResponse(file_path, headers={
        'Content-Disposition': f'attachment; filename="{filename}"'
    })

async def health_check_handler(request):
    """صفحة رئيسية بسيطة للتأكد من عمل السيرفر"""
    return web.Response(text="✅ بوت الديسكورد وخادم الملفات يعملان بنجاح!")

async def start_web_server():
    """تشغيل خادم الويب في الخلفية مع البوت"""
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
    """تهيئة متصفح Chrome في وضع Headless (Heroku - chrome-for-testing)"""
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
        print("[INFO] Chrome Driver initialized successfully (chrome-for-testing).")
        return driver

    except WebDriverException as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver: {type(e).__name__} - {e}")
        return None

def extract_pdf_via_canvas(url: str, output_filename: str):
    """سكربت Selenium لاستخراج الصور وتجميعها في PDF"""
    driver = init_driver()
    if not driver:
        return {"success": False, "error": "فشل في تشغيل المتصفح. تأكد من إعدادات Heroku."}

    images_base64 = []
    processed_urls = set()
    
    try:
        driver.get(url)
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, 'img'))
        )
        
        scroll_attempts = 0
        max_attempts = 1500  # زيادة الحد لدعم الملفات التي تصل لـ 1000+ صفحة
        empty_scrolls = 0    # عداد لمعرفة متى نصل لنهاية الملف
        
        print(f"[INFO] Starting to extract document: {output_filename}")
        
        while scroll_attempts < max_attempts:
            # 1. إحضار العناصر الموجودة في الصفحة *حالياً*
            img_elements = driver.find_elements(By.TAG_NAME, 'img')
            extracted_in_this_pass = False
            
            for img in img_elements:
                try:
                    src = img.get_attribute('src')
                    check_url_string = "blob:https://drive.google.com/"
                    
                    if src and src.startswith(check_url_string) and src not in processed_urls:
                        # النزول للصورة لضمان تحميلها بأعلى جودة
                        driver.execute_script("arguments[0].scrollIntoView(true);", img)
                        time.sleep(1) # انتظار ثانية لتكتمل الدقة
                        
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
                            extracted_in_this_pass = True
                            empty_scrolls = 0
                            
                            # ⚠️ كسر الحلقة هنا مهم جداً: لتحديث عناصر الصفحة وتفادي خطأ Stale Element
                            break 
                            
                except StaleElementReferenceException:
                    # إذا اختفت الصورة أثناء المحاولة، نكسر الحلقة لنجلب العناصر الجديدة من الـ DOM
                    break
            
            # إذا لم نجد صوراً جديدة في هذه المحاولة، نقوم بعمل سكرول إجباري للأسفل
            if not extracted_in_this_pass:
                driver.execute_script("window.scrollBy(0, window.innerHeight);")
                time.sleep(1.5)
                empty_scrolls += 1
            
            # إذا قمنا بالسكرول 6 مرات متتالية ولم نجد صوراً جديدة، فهذا يعني أننا وصلنا للنهاية
            if empty_scrolls >= 6:
                print(f"[INFO] Reached the end of the document. Total pages: {len(images_base64)}")
                break
                
            scroll_attempts += 1

        if not images_base64:
            return {"success": False, "error": "لم يتم العثور على أي محتوى مطابق."}
        
        pil_images = []
        for index, b64 in enumerate(images_base64):
            if "," in b64:
                b64_string = b64.split(",")[1]
            else:
                continue
                
            img_bytes = base64.b64decode(b64_string)
            img_obj = Image.open(BytesIO(img_bytes)).convert('RGB')
            pil_images.append(img_obj)

        # حفظ الملف في مجلد التنزيلات
        pdf_path = os.path.join(os.getcwd(), DOWNLOADS_DIR, output_filename)
        pil_images[0].save(
            pdf_path, 
            save_all=True, 
            append_images=pil_images[1:], 
            resolution=100.0
        )
        
        return {"success": True, "file_path": pdf_path, "filename": output_filename}

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if driver:
            driver.quit()

# --- دالة الحذف التلقائي بعد 15 دقيقة ---
async def delete_file_after_delay(file_path: str, delay_seconds: int = 900):
    """تنتظر مدة معينة ثم تحذف الملف"""
    await asyncio.sleep(delay_seconds)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            print(f"[INFO] 🗑️ تم حذف الملف تلقائياً: {file_path}")
        except Exception as e:
            print(f"[ERROR] فشل حذف الملف {file_path}: {e}")

# --- أحداث البوت ---
@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
    
    bot.loop.create_task(start_web_server())

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

@bot.tree.command(name="fetchpdf", description="استخراج ملف من جوجل درايف وإعطاء رابط تحميل مباشر")
@discord.app_commands.describe(url="رابط جوجل درايف للملف")
async def fetch_pdf(interaction: discord.Interaction, url: str):
    
    await interaction.response.defer(ephemeral=False)
    await interaction.edit_original_response(content="⏳ **جاري معالجة الملف...** *(قد يستغرق الملف المكون من 400 صفحة عدة دقائق، يرجى الانتظار)*")
    
    output_filename = f"drive_doc_{interaction.id}.pdf"
    
    result = await asyncio.to_thread(extract_pdf_via_canvas, url, output_filename)
    
    if result["success"]:
        file_path = result["file_path"]
        filename = result["filename"]
        
        direct_link = f"{HEROKU_BASE_URL}/{DOWNLOADS_DIR}/{filename}"
        
        await interaction.edit_original_response(content=(
            f"✅ **تم تجهيز الملف بنجاح!**\n\n"
            f"📥 **رابط التحميل المباشر:**\n{direct_link}\n\n"
            f"⚠️ *ملاحظة: هذا الرابط سيعمل لمدة **15 دقيقة** فقط وسيتم حذف الملف تلقائياً لتوفير المساحة.*"
        ))
        
        asyncio.create_task(delete_file_after_delay(file_path, 900))
        
    else:
        await interaction.edit_original_response(content=f"❌ **فشل استخراج الملف:** {result['error']}")

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("[CRITICAL ERROR] DISCORD_BOT_TOKEN not found in environment variables.")

