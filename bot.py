import discord
from discord.ext import commands
import asyncio
import os
import time
import base64
import aiohttp
from io import BytesIO
from PIL import Image

# استيرادات Selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service 
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

# --- الإعدادات ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def init_driver():
    """
    تهيئة متصفح Chrome في وضع Headless (Heroku - chrome-for-testing)
    """
    chrome_options = Options()

    # مهم جدًا لـ Heroku
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    # يخلي Selenium يلقاه تلقائي
    chrome_options.binary_location = os.environ.get("GOOGLE_CHROME_BIN")

    try:
        # ❗ بدون Service ولا executable_path
        driver = webdriver.Chrome(options=chrome_options)

        driver.set_page_load_timeout(60)
        print("[INFO] Chrome Driver initialized successfully (chrome-for-testing).")
        return driver

    except WebDriverException as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver: {type(e).__name__} - {e}")
        return None
        
async def upload_for_direct_link(file_path):
    """
    دالة لرفع الملف إلى خدمة Catbox المجانية والحصول على رابط تحميل مباشر.
    لا تحتاج إلى API Key وتقبل حتى 200MB.
    """
    url = "https://catbox.moe/user/api.php"
    
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('reqtype', 'fileupload')
                data.add_field('fileToUpload', f, filename=os.path.basename(file_path))
                
                async with session.post(url, data=data) as response:
                    if response.status == 200:
                        download_link = await response.text()
                        return download_link.strip()
        return None
    except Exception as e:
        print(f"Upload Error: {e}")
        return None

def extract_pdf_via_canvas(url: str, output_filename: str):
    """سكربت Selenium لاستخراج الصور المعروضة كـ Blob وتجميعها في PDF"""
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
        
        last_height = 0
        scroll_attempts = 0
        
        while scroll_attempts < 15:
            img_elements = driver.find_elements(By.TAG_NAME, 'img')
            
            for img in img_elements:
                src = img.get_attribute('src')
                check_url_string = "blob:https://drive.google.com/"
                
                if src and src.startswith(check_url_string) and src not in processed_urls:
                    driver.execute_script("arguments[0].scrollIntoView(true);", img)
                    time.sleep(0.5) 
                    
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
            
            driver.execute_script("window.scrollBy(0, 1000);")
            time.sleep(2)
            
            new_height = driver.execute_script("return document.documentElement.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
            scroll_attempts += 1

        if not images_base64:
            return {"success": False, "error": "لم يتم العثور على أي محتوى مطابق."}
        
        pil_images = []
        for b64 in images_base64:
            if "," in b64:
                b64_string = b64.split(",")[1]
            else:
                continue
                
            img_bytes = base64.b64decode(b64_string)
            img_obj = Image.open(BytesIO(img_bytes)).convert('RGB')
            pil_images.append(img_obj)

        pdf_path = os.path.join(os.getcwd(), output_filename)
        pil_images[0].save(
            pdf_path, 
            save_all=True, 
            append_images=pil_images[1:], 
            resolution=100.0
        )
        
        return {"success": True, "file_path": pdf_path}

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if driver:
            driver.quit()

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
    # مزامنة السلاش كوماندز مع سيرفرات الديسكورد
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

@bot.tree.command(name="fetchpdf", description="استخراج ملف من جوجل درايف وإعطاء رابط تحميل مباشر")
@discord.app_commands.describe(url="رابط جوجل درايف للملف")
async def fetch_pdf(interaction: discord.Interaction, url: str):
    """سلاش كوماند لاستخراج ملف درايف وإعطاء رابط تحميل مباشر"""
    
    # يجب استخدام defer لأن العملية تطول أكثر من 3 ثوانٍ
    await interaction.response.defer(ephemeral=False)
    
    await interaction.edit_original_response(content="⏳ **جاري فحص المحتوى وسحب الصفحات... الرجاء الانتظار.**")
    
    # استخدام الـ id الخاص بالـ interaction لتسمية الملف
    output_filename = f"drive_doc_{interaction.id}.pdf"
    
    # 1. استخراج الملف وتحويله لـ PDF
    result = await asyncio.to_thread(extract_pdf_via_canvas, url, output_filename)
    
    if result["success"]:
        file_path = result["file_path"]
        await interaction.edit_original_response(content="⏳ **تم تجميع الملف بنجاح! جاري رفعه لإنشاء رابط تحميل مباشر...**")
        
        # 2. رفع الملف للحصول على الرابط المباشر
        direct_link = await upload_for_direct_link(file_path)
        
        if direct_link:
            await interaction.edit_original_response(content=f"✅ **اكتملت العملية بنجاح!**\n\n📥 **رابط التحميل المباشر:**\n{direct_link}")
        else:
            # كخيار بديل، إذا فشل الرفع وكان حجمه أقل من 25 ميجا سيرسله بالديسكورد
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb <= 25:
                await interaction.edit_original_response(
                    content="⚠️ **فشل إنشاء رابط التحميل، لكن سأرسل الملف هنا مباشرة:**",
                    attachments=[discord.File(file_path)]
                )
            else:
                await interaction.edit_original_response(content="❌ **فشل إنشاء الرابط وحجم الملف كبير جداً لإرساله في ديسكورد مباشرة.**")
                
        # 3. حذف الملف من السيرفر لتوفير المساحة
        if os.path.exists(file_path):
            os.remove(file_path)
    else:
        await interaction.edit_original_response(content=f"❌ **فشل استخراج الملف:** {result['error']}")

# تشغيل البوت باستخدام التوكن من متغيرات البيئة
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("[CRITICAL ERROR] DISCORD_BOT_TOKEN not found in environment variables.")

