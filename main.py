import discord
from discord.ext import commands
from PIL import Image
from io import BytesIO
import dropbox
import re
import os
import asyncio
import uuid
import zipfile
import shutil
# استيرادات Selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service 
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
import requests
import time 

# --- الإعدادات والثوابت ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")

MIN_WIDTH = 800
CLEANUP_DELAY_SECONDS = 1800
LOCAL_TEMP_DIR = "manga_temp" 
IMAGE_DOWNLOAD_TIMEOUT = 30 
VALID_FORMATS = ['jpg', 'jpeg', 'webp', 'png']

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

# --- دالة تهيئة متصفح Selenium (مُثبتة لـ Heroku) ---
def init_driver():
    """
    تهيئة متصفح Chrome في وضع Headless.
    """
    chrome_bin = os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_BIN")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    
    if not chrome_bin or not chromedriver_path:
        print("[CRITICAL ERROR] Heroku environment variables (CHROME_BIN/CHROMEDRIVER_PATH) not found.")
        return None

    chrome_options = Options()
    
    # خيارات أساسية لـ Headless
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-dev-shm-usage")
    # زيادة المهلة لانتظار تحميل الصفحة
    chrome_options.page_load_strategy = 'normal' 
    
    chrome_options.binary_location = chrome_bin 

    try:
        service = Service(executable_path=chromedriver_path)
        # زيادة مهلة تشغيل المتصفح إلى 60 ثانية
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(60) # مهلة تحميل الصفحة
        print("[INFO] Chrome Driver initialized successfully using Heroku static paths and Service object.")
        return driver
    except WebDriverException as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver: {e}")
        return None


# --- الدوال المساعدة (تم تعديل دالة تحميل الصور هنا) ---

def download_and_check_image(image_url, target_format="jpg"):
    """
    تحميل الصورة، التحقق من حجمها، وتحويلها لـ format المستهدف.
    (تم إضافة User-Agent ومعالجة الأخطاء المحسّنة)
    """
    target_format = target_format.lower()
    
    if target_format in ['jpg', 'jpeg']:
        save_format = 'jpeg'
        ext = 'jpg'
    elif target_format == 'webp':
        save_format = 'webp'
        ext = 'webp'
    elif target_format == 'png':
        save_format = 'png'
        ext = 'png'
    else:
        save_format = 'jpeg'
        ext = 'jpg'
        
    # إضافة User-Agent لزيادة موثوقية التحميل
    headers = {
        "User-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(image_url, stream=True, timeout=IMAGE_DOWNLOAD_TIMEOUT, headers=headers)
        response.raise_for_status() 
        
        image_bytes = BytesIO(response.content)
        img = Image.open(image_bytes)
        
        # التأكد من تحويل الصورة لـ RGB إذا لم تكن PNG
        if save_format != 'png' and img.mode != 'RGB':
            img = img.convert("RGB")
        
        if img.width >= MIN_WIDTH:
            return img, ext, save_format
        else:
            print(f"[ERROR LOG] Skipping image {image_url}: Width {img.width}px is less than {MIN_WIDTH}px.")
            return None, None, None
            
    except Exception as e:
        if isinstance(e, requests.exceptions.HTTPError):
            print(f"[ERROR LOG] HTTP Error processing image {image_url}: {e.response.status_code}")
        else:
            print(f"[ERROR LOG] General Error processing image {image_url}: {e}")
            
        return None, None, None


# دالة التنظيف تبقى كما هي (Async)
async def cleanup_dropbox_file(dropbox_path: str, delay_seconds: int):
    """ينتظر 15 دقيقة ثم يحذف الملف المضغوط من Dropbox."""
    await asyncio.sleep(delay_seconds)
    try:
        dbx.files_delete_v2(dropbox_path)
        print(f"🗑️ تم حذف ملف ZIP ({dropbox_path}) بنجاح بعد {delay_seconds} ثواني.")
    except Exception as e:
        print(f"❌ فشل حذف ملف ZIP ({dropbox_path}): {e}")


def merge_chapter_images(chapter_folder: str, image_format: str):
    """
    تنفذ دمج الصور لملفات JPG/JPEG فقط.
    """
    if image_format.lower() not in ['jpg', 'jpeg']:
        print(f"[INFO] Skipping merge: Merge is only supported for JPG/JPEG format.")
        return

    jpeg_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(('.jpg', '.jpeg'))])
    
    num_jpeg = len(jpeg_files)
    merge_list = [] 
    
    i = 0
    while i + 1 < num_jpeg:
        file1_path = os.path.join(chapter_folder, jpeg_files[i])
        file2_path = os.path.join(chapter_folder, jpeg_files[i+1])
        merge_list.append((file1_path, file2_path))
        i += 2
        
    for file1_path, file2_path in merge_list:
        try:
            img1 = Image.open(file1_path).convert("RGB") 
            img2 = Image.open(file2_path).convert("RGB")
            
            max_width = max(img1.width, img2.width)
            total_height = img1.height + img2.height
            
            merged_img = Image.new('RGB', (max_width, total_height))
            merged_img.paste(img1, (0, 0)) 
            merged_img.paste(img2, (0, img1.height)) 
            
            merged_img.save(file1_path, 'jpeg', quality=90) 
            os.remove(file2_path)
            print(f"Merged {os.path.basename(file1_path)} and {os.path.basename(file2_path)}")

        except Exception as e:
            print(f"[ERROR LOG] Failed to merge images: {e}")
            continue

    # إعادة ترقيم الملفات النهائية
    final_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(tuple(VALID_FORMATS))])
    
    for index, filename in enumerate(final_files):
        ext = filename.split('.')[-1]
        new_filename = f"{index + 1:03d}.{ext}"
        
        if filename != new_filename:
            try:
                os.rename(os.path.join(chapter_folder, filename), os.path.join(chapter_folder, new_filename))
            except Exception as e:
                print(f"[ERROR LOG] Failed to rename file: {e}")


# --- مهمة المعالجة الطويلة (متزامنة - تم تعديلها للتعامل مع Lazy Loading) ---
def _process_manga_download(url, chapter_number, chapters, merge_images, image_format):
    """
    تحتوي على كل منطق الـ Selenium والملفات. تُشغل في خيط منفصل.
    تعيد قاموسًا بالنتائج النهائية.
    """
    driver = None
    chapters_processed = 0
    
    # تنظيف المجلد المؤقت قبل البدء
    if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    
    try:
        # 1. تهيئة المتصفح
        driver = init_driver()
        if not driver:
            return {"success": False, "error": "فشل في تهيئة متصفح Chrome/Selenium."}

        # 2. تحليل الرابط وتحديد نطاق الفصول
        base_url_pattern = url
        url_contains_chapter_num = False
        
        match = re.search(r'(chapter|no|epi)[\-_=]\d+', url, re.IGNORECASE)
        
        if match:
            base_url_pattern = re.sub(r'(chapter|no|epi)[\-_=]\d+', r'\1-{}', url, re.IGNORECASE)
            url_contains_chapter_num = True
        
        if not url_contains_chapter_num and chapters > 1:
            chapters = 1

        chapter_range = range(chapter_number, chapter_number + chapters) 
        
        # 3. حلقة معالجة الفصول
        for current_chapter_num in chapter_range:
            if url_contains_chapter_num:
                current_url = base_url_pattern.format(current_chapter_num)
            else:
                current_url = url
                
            local_chapter_folder = os.path.join(LOCAL_TEMP_DIR, str(current_chapter_num))
            images_downloaded = 0
            
            try:
                os.makedirs(local_chapter_folder, exist_ok=True)
                
                driver.get(current_url)
                
                # 3.1 الانتظار حتى تحميل أول صورة (يشمل انتظار data-src)
                WebDriverWait(driver, 45).until( 
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'img.page-image, img[src*="cdn"], img[src*="data"], img[data-src]'))
                )
                
                # 3.2 التمرير لأسفل الصفحة للتعامل مع Lazy Loading
                last_height = driver.execute_script("return document.body.scrollHeight")
                scroll_attempts = 0
                max_scrolls = 10 
                
                while scroll_attempts < max_scrolls:
                    # التمرير لأسفل
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(3) # الانتظار لتحميل الصور الجديدة
                    
                    # حساب الارتفاع الجديد بعد التمرير
                    new_height = driver.execute_script("return document.body.scrollHeight")
                    
                    if new_height == last_height:
                        # لم يتم تحميل المزيد من المحتوى، نتوقف
                        break
                        
                    last_height = new_height
                    scroll_attempts += 1
                
                # 3.3 استخلاص روابط الصور (البحث في src و data-src)
                image_elements = driver.find_elements(By.TAG_NAME, 'img')
                
                image_srcs = []
                for img in image_elements:
                    src = img.get_attribute('src')
                    data_src = img.get_attribute('data-src') # لاقتناص Lazy Load
                    
                    # نستخدم data-src إذا كان موجوداً وغير فارغ
                    if data_src and not data_src.startswith('data:'):
                        image_srcs.append(data_src)
                    # وإلا، نستخدم src إذا كان موجوداً وغير فارغ
                    elif src and not src.startswith('data:'):
                        image_srcs.append(src)
                        
                # إزالة الروابط المكررة للحفاظ على الكفاءة
                image_srcs = list(dict.fromkeys(image_srcs))


                if not image_srcs: 
                    print(f"[ERROR LOG] No unique image URLs found in chapter {current_chapter_num}")
                    if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
                    continue
                
                # تنزيل وحفظ الصور
                image_counter = 1
                for img_src in image_srcs:
                    if not img_src or img_src.startswith('data:'): continue

                    img_obj, ext, save_format = download_and_check_image(img_src, image_format)
                    
                    if img_obj:
                        filename = f"{image_counter:03d}.{ext}"
                        local_file_path = os.path.join(local_chapter_folder, filename)
                        
                        if save_format in ['jpeg', 'webp']:
                            img_obj.save(local_file_path, save_format, quality=90)
                        elif save_format == 'png':
                            img_obj.save(local_file_path, 'png') 

                        images_downloaded += 1
                        image_counter += 1
                
                if images_downloaded > 0:
                    if merge_images:
                        merge_chapter_images(local_chapter_folder, image_format) 
                    chapters_processed += 1
                else:
                    print(f"[ERROR LOG] No images were successfully downloaded in chapter {current_chapter_num}.")
                    if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
                
            except Exception as e:
                print(f"[ERROR LOG] Chapter {current_chapter_num} failed: {e}")
                if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
                continue
        
        # 4. إنهاء العملية (الضغط والرفع)
        if chapters_processed == 0:
            return {"success": False, "error": "**لم يتم معالجة أو تنزيل أي فصول بنجاح.**"}

        unique_id = uuid.uuid4().hex[:8]
        zip_filename = f"manga_{unique_id}.zip"
        local_zip_path = os.path.join(os.getcwd(), zip_filename)

        # الضغط
        with zipfile.ZipFile(local_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(LOCAL_TEMP_DIR):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, LOCAL_TEMP_DIR)
                    zipf.write(file_path, arcname)
        
        # الرفع إلى Dropbox
        dropbox_path = f"/{zip_filename}"
        with open(local_zip_path, 'rb') as f:
            dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode('overwrite'))

        # إنشاء رابط المشاركة
        shared_link = ""
        try:
            shared_link_metadata = dbx.sharing_create_shared_link_with_settings(dropbox_path)
            shared_link = shared_link_metadata.url
        except dropbox.exceptions.ApiError as e:
            if e.error.is_shared_link_already_exists():
                shared_links = dbx.sharing_list_shared_links(path=dropbox_path, direct_only=True).links
                if shared_links:
                    shared_link = shared_links[0].url
            else:
                shared_link = "(فشل إنشاء رابط مشاركة)"

        # إرجاع النتائج للواجهة غير المتزامنة
        return {
            "success": True, 
            "shared_link": shared_link, 
            "chapters_processed": chapters_processed,
            "zip_path": local_zip_path,
            "dropbox_path": dropbox_path,
            "url_was_fixed": not url_contains_chapter_num and chapters == 1
        }

    except Exception as e:
        print(f"[CRITICAL ERROR] Download task failed: {e}")
        return {"success": False, "error": f"فشل العملية: {e}"}
        
    finally:
        # التنظيف النهائي
        if driver: driver.quit()
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)


# --- أحداث البوت ---

@bot.event
async def on_ready():
    # ... (كما هو) ...
    print(f'Bot is ready. Logged in as {bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
        dbx.users_get_current_account()
        print("Dropbox connection successful.")
    except Exception as e:
        print(f"Dropbox connection failed or slash commands sync failed: {e}")


# --- أمر التطبيق (Slash Command) ---

@bot.tree.command(name="download", description="تحميل الصور من مواقع المانجا وضغطها ورفعها.")
@discord.app_commands.describe(
    url="رابط صفحة المانجا/الويبتون",
    chapter_number="رقم الفصل الأول الذي سيبدأ به الترقيم (افتراضي 1)",
    chapters="عدد الفصول المراد تحميلها (افتراضي 1)",
    merge_images="دمج الصور المزدوجة في كل فصل (JPG فقط - افتراضي: False)", # تم تغيير الافتراضي لتجنب الدمج الغير مرغوب فيه
    image_format="صيغة الإخراج المطلوبة (مثل: jpg, webp, png - افتراضي: jpg)"
)
async def download_command(
    interaction: discord.Interaction, 
    url: str,
    chapter_number: int = 1,
    chapters: int = 1,       
    merge_images: bool = False,
    image_format: str = "jpg"
):
    user_mention = interaction.user.mention
    
    # 1. التحقق الأولي
    if image_format.lower() not in VALID_FORMATS:
        error_msg = f"❌ **صيغة الإخراج غير مدعومة!** الصيغ المدعومة هي: {', '.join(VALID_FORMATS)}."
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    initial_embed = discord.Embed(
        title="📥 تحميل فصل المانهوا",
        description=f"{user_mention} **جارِ المعالجة، الرجاء الانتظار...** ⌛",
        color=discord.Color.dark_grey()
    )
    
    # يجب إرسال الرد المبدئي بسرعة قبل البدء بالمهمة الطويلة
    await interaction.response.send_message(embed=initial_embed, ephemeral=False)
    original_response = await interaction.original_response()

    # 2. تنفيذ المهمة الطويلة في خيط منفصل (يمنع حظر الـ Heartbeat)
    try:
        # استخدام asyncio.to_thread لتشغيل الدالة المتزامنة في خيط العامل
        result = await asyncio.to_thread(
            _process_manga_download,
            url,
            chapter_number,
            chapters,
            merge_images,
            image_format.lower()
        )
    except Exception as e:
        print(f"[CRITICAL ERROR] asyncio.to_thread failed: {e}")
        result = {"success": False, "error": f"فشل غير متوقع في الخادم: {e}"}

    # 3. معالجة النتائج وإرسال الرد النهائي
    if result["success"]:
        # تنظيف الملف المضغوط محليًا بعد نجاح الرفع
        if os.path.exists(result["zip_path"]): os.remove(result["zip_path"])
        
        # جدولة مهمة حذف ملف Dropbox
        bot.loop.create_task(cleanup_dropbox_file(result["dropbox_path"], CLEANUP_DELAY_SECONDS))
        
        final_embed = discord.Embed(
            title="✅ تم الرفع إلى Dropbox",
            description=f"{user_mention} **تم رفع الملف بنجاح!**\n\n**رابط التحميل:**\n{result['shared_link']}\n\n"
                        f"**ملاحظة:** سيتم حذف الملف تلقائيًا بعد **{CLEANUP_DELAY_SECONDS // 60} دقيقة**.",
            color=discord.Color.green()
        )
        footer_text = f"تم معالجة {result['chapters_processed']} فصل/فصول بنجاح. الصيغة: {image_format.upper()}. الدمج: {'مفعل' if merge_images else 'غير مفعل'}."
        # إضافة تحذير إذا تم تعديل عدد الفصول إلى 1
        if result.get('url_was_fixed'):
            footer_text += " (تحذير: تم تحميل فصل واحد فقط لعدم وجود نمط ترقيم واضح)."
            
        final_embed.set_footer(text=footer_text)
        
        await original_response.edit(embed=final_embed)
    else:
        error_embed = discord.Embed(
            title="❌ فشل العملية",
            description=f"حدث خطأ أثناء المعالجة:\n**{result.get('error', 'خطأ غير معروف')}**",
            color=discord.Color.red()
        )
        await original_response.edit(embed=error_embed)

# تشغيل البوت
bot.run(DISCORD_BOT_TOKEN)
