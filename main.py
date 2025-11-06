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
from selenium.common.exceptions import TimeoutException, WebDriverException
import requests

# --- الإعدادات والثوابت ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")

MIN_WIDTH = 800
CLEANUP_DELAY_SECONDS = 900
LOCAL_TEMP_DIR = "manga_temp" 
IMAGE_DOWNLOAD_TIMEOUT = 15 
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
    يستخدم متغيرات البيئة CHROME_BIN و CHROMEDRIVER_PATH التي يوفرها Buildpack.
    """
    
    # قراءة متغيرات البيئة التي يوفرها Buildpack
    chrome_bin = os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_BIN")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    
    if not chrome_bin or not chromedriver_path:
        print("[CRITICAL ERROR] Heroku environment variables (CHROME_BIN/CHROMEDRIVER_PATH) not found.")
        print("[CRITICAL ERROR] Please ensure the Buildpack is correctly installed and deployed.")
        return None

    chrome_options = Options()
    
    # خيارات أساسية لـ Headless
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    # تعيين مسار Chrome
    chrome_options.binary_location = chrome_bin 

    try:
        # الإصدار الصحيح لـ Selenium 4.x: استخدام Service object وتمرير مسار Driver
        service = Service(executable_path=chromedriver_path)
        
        # تمرير كائن Service بدلاً من executable_path مباشرةً
        driver = webdriver.Chrome(service=service, options=chrome_options)
        print("[INFO] Chrome Driver initialized successfully using Heroku static paths and Service object.")
        return driver
    except WebDriverException as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver: {e}")
        return None


# --- الدوال المساعدة ---

def download_and_check_image(image_url, target_format="jpg"):
    """
    تحميل الصورة مع مهلة، التحقق من حجمها، وتحويلها لـ RGB والـ format المستهدف.
    """
    target_format = target_format.lower()
    
    # تعيين صيغة الحفظ الفعلية
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
        # الصيغة الافتراضية إذا كانت غير صالحة
        save_format = 'jpeg'
        ext = 'jpg'

    try:
        response = requests.get(image_url, stream=True, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        response.raise_for_status() 
        
        image_bytes = BytesIO(response.content)
        img = Image.open(image_bytes)
        
        # تحويل لـ RGB ما لم تكن الصيغة المستهدفة PNG
        if save_format != 'png':
             # التحويل إلى RGB يزيل قناة ألفا (الشفافية) الضرورية للـ PNG
            img = img.convert("RGB")
        
        if img.width >= MIN_WIDTH:
            return img, ext, save_format
        else:
            print(f"[ERROR LOG] Skipping image {image_url}: Width {img.width}px is less than {MIN_WIDTH}px.")
            return None, None, None
            
    except requests.exceptions.Timeout:
        print(f"[ERROR LOG] Request Timeout for image: {image_url} after {IMAGE_DOWNLOAD_TIMEOUT}s.")
        return None, None, None
    except requests.exceptions.HTTPError as http_err:
        print(f"[ERROR LOG] HTTP Error for image: {http_err} for URL {image_url}.")
        return None, None, None
    except requests.exceptions.RequestException as req_err:
        print(f"[ERROR LOG] Request Error for image: {req_err} for URL {image_url}.")
        return None, None, None
    except Exception as e:
        print(f"[ERROR LOG] Generic Error processing image {image_url}: {e}")
        return None, None, None


async def cleanup_dropbox_file(dropbox_path: str, delay_seconds: int):
    """ينتظر 15 دقيقة ثم يحذف الملف المضغوط من Dropbox."""
    await asyncio.sleep(delay_seconds)
    try:
        dbx.files_delete_v2(dropbox_path)
        print(f"🗑️ تم حذف ملف ZIP ({dropbox_path}) بنجاح بعد {delay_seconds} ثواني.")
    except Exception as e:
        print(f"❌ فشل حذف ملف ZIP ({dropbox_path}): {e}")
        print(f"[ERROR LOG] Cleanup failed for {dropbox_path}: {e}")


def merge_chapter_images(chapter_folder: str, image_format: str):
    """
    تنفذ دمج الصور لملفات JPG/JPEG فقط لضمان عدد زوجي من المخرجات، وتتجاهل الصيغ الأخرى.
    """
    
    if image_format.lower() not in ['jpg', 'jpeg']:
        print(f"[INFO] Skipping merge: Merge is only supported for JPG/JPEG format, current format is {image_format}.")
        return

    # جمع ملفات JPG/JPEG فقط
    jpeg_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(('.jpg', '.jpeg'))])
    
    num_jpeg = len(jpeg_files)
    merge_list = [] 
    
    i = 0
    # إنشاء قائمة بالملفات التي سيتم دمجها (اثنين باثنين)
    while i + 1 < num_jpeg:
        file1_name = jpeg_files[i]
        file2_name = jpeg_files[i+1]
        merge_list.append((os.path.join(chapter_folder, file1_name), os.path.join(chapter_folder, file2_name)))
        i += 2
        
    for file1_path, file2_path in merge_list:
        try:
            # يجب استخدام convert("RGB") لضمان التوافق بين الصورتين
            img1 = Image.open(file1_path).convert("RGB") 
            img2 = Image.open(file2_path).convert("RGB")
            
            max_width = max(img1.width, img2.width)
            total_height = img1.height + img2.height
            
            merged_img = Image.new('RGB', (max_width, total_height))
            # لصق الصورة الأولى في الأعلى
            merged_img.paste(img1, (0, 0)) 
            # لصق الصورة الثانية أسفل الأولى
            merged_img.paste(img2, (0, img1.height)) 
            
            # حفظ الصورة المدمجة فوق الصورة الأولى
            merged_img.save(file1_path, 'jpeg', quality=90) 
            # حذف الصورة الثانية
            os.remove(file2_path)
            print(f"Merged {os.path.basename(file1_path)} and {os.path.basename(file2_path)}")

        except Exception as e:
            print(f"[ERROR LOG] Failed to merge images {os.path.basename(file1_path)} and {os.path.basename(file2_path)}: {e}")
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
                print(f"[ERROR LOG] Failed to rename file {filename} to {new_filename}: {e}")


# --- أحداث البوت ---

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
        dbx.users_get_current_account()
        print("Dropbox connection successful.")
    except Exception as e:
        print(f"Dropbox connection failed or slash commands sync failed: {e}")
        print(f"[ERROR LOG] Initial setup failed: {e}")


# --- أمر التطبيق (Slash Command) ---

@bot.tree.command(name="download", description="تحميل الصور من مواقع المانجا وضغطها ورفعها.")
@discord.app_commands.describe(
    url="رابط صفحة المانجا/الويبتون",
    chapter_number="رقم الفصل الأول الذي سيبدأ به الترقيم (افتراضي 1)",
    chapters="عدد الفصول المراد تحميلها (افتراضي 1)",
    merge_images="دمج الصور المزدوجة في كل فصل (JPG فقط - افتراضي: True)",
    image_format="صيغة الإخراج المطلوبة (مثل: jpg, webp, png - افتراضي: jpg)"
)
async def download_command(
    interaction: discord.Interaction, 
    url: str,
    chapter_number: int = 1,
    chapters: int = 1,       
    merge_images: bool = True,
    image_format: str = "jpg"
):
    user_mention = interaction.user.mention
    
    # التحقق من الصيغة المدخلة
    if image_format.lower() not in VALID_FORMATS:
        error_msg = f"❌ **صيغة الإخراج غير مدعومة!** الصيغ المدعومة هي: {', '.join(VALID_FORMATS)}."
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    initial_embed = discord.Embed(
        title="📥 تحميل فصل المانهوا",
        description=f"{user_mention} **جارِ المعالجة، الرجاء الانتظار...** ⌛",
        color=discord.Color.dark_grey()
    )
    
    await interaction.response.send_message(embed=initial_embed, ephemeral=False)
    original_response = await interaction.original_response()

    # --- تهيئة المتصفح ---
    driver = init_driver()
    if not driver:
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        await original_response.edit(embed=discord.Embed(title="❌ فشل التهيئة", description="**فشل في تهيئة متصفح Chrome/Selenium. الرجاء التحقق من إعدادات Buildpacks والتوزيع.**", color=discord.Color.red()))
        return

    # إنشاء مجلد العمل المؤقت
    if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    
    # --- تحديد نطاق الفصول والروابط ---
    base_url_pattern = url
    url_contains_chapter_num = False
    
    # البحث عن نمط رقم الفصل (مثل chapter-XX, no=XX, epi=XX)
    match = re.search(r'(chapter|no|epi)[\-_=]\d+', url, re.IGNORECASE)
    
    if match:
        # استبدال رقم الفصل بنمط جاهز للتنسيق ({}). مثال: no=100 -> no-{}
        base_url_pattern = re.sub(r'(chapter|no|epi)[\-_=]\d+', r'\1-{}', url, re.IGNORECASE)
        url_contains_chapter_num = True
    
    # إذا لم يكن هناك نمط، يتم تحميل فصل واحد فقط من الرابط المباشر
    if not url_contains_chapter_num and chapters > 1:
        # إذا لم يكن الرابط قابلًا للترقيم، نلغي التحميل المتعدد ونبقي على الفصل الأول فقط
        chapters = 1
        initial_embed.description = f"⚠️ **تحذير:** لم يتم العثور على نمط ترقيم في الرابط. سيتم تحميل **فصل واحد** فقط.\n\n{user_mention} **جارِ جلب وتحميل الفصل رقم {chapter_number}...** ⏳"
        await original_response.edit(embed=initial_embed)
    
    # نطاق الأرقام التي ستستخدم لتسمية المجلدات وتحديد URL (إذا كان قابلاً للترقيم)
    chapter_range = range(chapter_number, chapter_number + chapters) 
    
    chapters_processed = 0
    
    # --- حلقة معالجة الفصول ---
    for current_chapter_num in chapter_range:
        
        # تحديد الرابط الحالي
        if url_contains_chapter_num:
            # يتم استخدام current_chapter_num في الرابط إذا كان قابلاً للترقيم
            current_url = base_url_pattern.format(current_chapter_num)
        else:
            # إذا لم يكن قابلاً للترقيم، يتم استخدام الرابط الأصلي طوال الوقت
            current_url = url
            
        # يتم استخدام current_chapter_num لتسمية المجلد المحلي دائمًا
        local_chapter_folder = os.path.join(LOCAL_TEMP_DIR, str(current_chapter_num))
        images_downloaded = 0
        
        try:
            initial_embed.description = f"{user_mention} **جارِ جلب وتحميل الفصل رقم {current_chapter_num}، الرجاء الانتظار...** ⏳"
            await original_response.edit(embed=initial_embed)
            
            os.makedirs(local_chapter_folder, exist_ok=True)
            
            # 1. جلب الصفحة باستخدام Selenium والانتظار حتى تحميل الصور
            driver.get(current_url)
            
            # الانتظار حتى تحميل أول صورة
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'img.page-image, img[src*="cdn"], img[src*="data"]'))
            )
            
            # تمرير الصفحة للأسفل لضمان تحميل جميع الصور (Lazy Loading)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            
            # إعطاء مهلة إضافية بسيطة للانتهاء من التحميل بعد التمرير
            await asyncio.sleep(3) 
            
            # 2. استخراج روابط الصور
            image_elements = driver.find_elements(By.CSS_SELECTOR, 'img.page-image, img[src*="cdn"], img[src*="data"]')
            image_srcs = [img.get_attribute('src') for img in image_elements if img.get_attribute('src')]
            
            if not image_srcs: 
                print(f"[ERROR LOG] No images found via Selenium in chapter {current_chapter_num} at URL: {current_url}")
                shutil.rmtree(local_chapter_folder)
                continue
            
            # 3. تنزيل وحفظ الصور (باستخدام requests لفعالية التنزيل)
            image_counter = 1
            for img_src in image_srcs:
                if not img_src or img_src.startswith('data:'): continue

                # تمرير صيغة الصورة المطلوبة
                img_obj, ext, save_format = download_and_check_image(img_src, image_format)
                
                if img_obj:
                    filename = f"{image_counter:03d}.{ext}"
                    local_file_path = os.path.join(local_chapter_folder, filename)
                    
                    # حفظ الصورة بالصيغة المطلوبة مع تحديد معايير الجودة
                    if save_format in ['jpeg', 'webp']:
                        img_obj.save(local_file_path, save_format, quality=90)
                    elif save_format == 'png':
                        # حفظ PNG بدون ضغط الجودة (يمكن استخدام compress_level=9)
                        img_obj.save(local_file_path, 'png') 
                    else:
                         # صيغة غير متوقعة
                        img_obj.save(local_file_path, 'jpeg', quality=90)


                    images_downloaded += 1
                    image_counter += 1
            
            # 4. دمج الصور بعد التنزيل (اختياري)
            if images_downloaded > 0:
                if merge_images:
                    initial_embed.description = f"{user_mention} **جارِ دمج وضغط الفصل رقم {current_chapter_num}...** ⚙️"
                    await original_response.edit(embed=initial_embed)
                    merge_chapter_images(local_chapter_folder, image_format) 
                    
                chapters_processed += 1
            else:
                print(f"[ERROR LOG] No images were successfully downloaded in chapter {current_chapter_num}.")
                if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
            
        except TimeoutException:
            print(f"[ERROR LOG] Selenium Timeout: Page took too long to load images for chapter {current_chapter_num} (URL: {current_url}).")
            if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
            continue
        except WebDriverException as wde:
            print(f"[ERROR LOG] WebDriver Error (Chapter {current_chapter_num}): {wde}")
            if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
            continue
        except Exception as e:
            print(f"[ERROR LOG] Unexpected Error in chapter {current_chapter_num}: {e}")
            if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
            continue
    
    # إغلاق المتصفح بعد الانتهاء من جميع الفصول
    driver.quit() 

    # --- إنهاء العملية (الضغط والرفع) ---
    if chapters_processed == 0:
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        await original_response.edit(embed=discord.Embed(title="❌ فشل", description="**لم يتم معالجة أو تنزيل أي فصول بنجاح.**", color=discord.Color.red()))
        return

    # 1. الضغط
    unique_id = uuid.uuid4().hex[:8]
    zip_filename = f"manga_{unique_id}.zip"
    local_zip_path = os.path.join(os.getcwd(), zip_filename)

    initial_embed.description = f"{user_mention} **جارِ رفع الملف المضغوط...** 🚀"
    await original_response.edit(embed=initial_embed)

    try:
        with zipfile.ZipFile(local_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(LOCAL_TEMP_DIR):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, LOCAL_TEMP_DIR)
                    zipf.write(file_path, arcname)
    except Exception as e:
        print(f"[ERROR LOG] ZIP compression failed: {e}")
        await original_response.edit(content=f"```ini\n[ ❌ خطأ ]\n```\n**فشل في عملية ضغط الملفات: {e}**")
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        if os.path.exists(local_zip_path): os.remove(local_zip_path)
        return
    
    # 2. الرفع
    dropbox_path = f"/{zip_filename}"
    try:
        with open(local_zip_path, 'rb') as f:
            dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode('overwrite'))
    except Exception as e:
        print(f"[ERROR LOG] Dropbox upload failed: {e}")
        await original_response.edit(content=f"```ini\n[ ❌ خطأ ]\n```\n**فشل في عملية الرفع إلى Dropbox: {e}**")
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        if os.path.exists(local_zip_path): os.remove(local_zip_path)
        return

    # 3. الرابط والتنظيف
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
            print(f"[ERROR LOG] Failed to create shared link: {e}")
            shared_link = "(فشل إنشاء رابط مشاركة)"

    if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
    if os.path.exists(local_zip_path): os.remove(local_zip_path)
        
    bot.loop.create_task(cleanup_dropbox_file(dropbox_path, CLEANUP_DELAY_SECONDS))

    # 4. رسالة النجاح النهائية
    final_embed = discord.Embed(
        title="✅ تم الرفع إلى Dropbox",
        description=f"{user_mention} **تم رفع الملف بنجاح!**\n\n**رابط التحميل:**\n{shared_link}\n\n"
                    f"**ملاحظة:** سيتم حذف الملف تلقائيًا بعد **{CLEANUP_DELAY_SECONDS // 60} دقيقة**.",
        color=discord.Color.green()
    )
    final_embed.set_footer(text=f"تم معالجة {chapters_processed} فصل/فصول بنجاح. الصيغة: {image_format.upper()}. الدمج: {'مفعل' if merge_images else 'غير مفعل'}.")
    
    await original_response.edit(embed=final_embed)

# تشغيل البوت
bot.run(DISCORD_BOT_TOKEN)
