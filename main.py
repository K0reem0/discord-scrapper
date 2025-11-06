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
from webdriver_manager.chrome import ChromeDriverManager
import requests

# --- الإعدادات والثوابت ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")

MIN_WIDTH = 800
CLEANUP_DELAY_SECONDS = 3000
LOCAL_TEMP_DIR = "manga_temp" 
IMAGE_DOWNLOAD_TIMEOUT = 50 

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

# --- دالة تهيئة متصفح Selenium ---
def init_driver():
    """تهيئة متصفح Chrome في وضع Headless."""
    
    chrome_bin = os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_BIN")
    
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    if chrome_bin:
        chrome_options.binary_location = chrome_bin 

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except Exception as e:
        print(f"[CRITICAL ERROR] Failed to initialize Chrome Driver using webdriver-manager: {e}")
        try:
             chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
             if chromedriver_path and chrome_bin:
                driver = webdriver.Chrome(executable_path=chromedriver_path, options=chrome_options)
                print("[INFO] Successfully initialized using Heroku static paths after webdriver-manager failure.")
                return driver
        except Exception as e_fallback:
             print(f"[CRITICAL ERROR] Fallback initialization also failed: {e_fallback}")
             return None


# --- الدوال المساعدة ---

def download_and_check_image(image_url):
    """
    تحميل الصورة مع مهلة، التحقق من حجمها، وتحويلها لـ RGB إذا لم تكن PNG.
    """
    try:
        response = requests.get(image_url, stream=True, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        response.raise_for_status() 
        
        image_bytes = BytesIO(response.content)
        img = Image.open(image_bytes)
        
        # دائماً نحولها لـ RGB (ما لم تكن PNG) لضمان التوافق مع JPG/WEBP
        if img.format != 'PNG':
            img = img.convert("RGB")
        
        if img.width >= MIN_WIDTH:
            # نرجع كائن الصورة فقط، ودالة الحفظ هي التي تقرر الصيغة النهائية
            return img 
        else:
            print(f"[ERROR LOG] Skipping image {image_url}: Width {img.width}px is less than {MIN_WIDTH}px.")
            return None
            
    except requests.exceptions.Timeout:
        print(f"[ERROR LOG] Request Timeout for image: {image_url} after {IMAGE_DOWNLOAD_TIMEOUT}s.")
        return None
    except requests.exceptions.HTTPError as http_err:
        print(f"[ERROR LOG] HTTP Error for image: {http_err} for URL {image_url}.")
        return None
    except requests.exceptions.RequestException as req_err:
        print(f"[ERROR LOG] Request Error for image: {req_err} for URL {image_url}.")
        return None
    except Exception as e:
        print(f"[ERROR LOG] Generic Error processing image {image_url}: {e}")
        return None

async def cleanup_dropbox_file(dropbox_path: str, delay_seconds: int):
    """ينتظر 15 دقيقة ثم يحذف الملف المضغوط من Dropbox."""
    await asyncio.sleep(delay_seconds)
    try:
        dbx.files_delete_v2(dropbox_path)
        print(f"🗑️ تم حذف ملف ZIP ({dropbox_path}) بنجاح بعد {delay_seconds} ثواني.")
    except Exception as e:
        print(f"❌ فشل حذف ملف ZIP ({dropbox_path}): {e}")
        print(f"[ERROR LOG] Cleanup failed for {dropbox_path}: {e}")


def merge_chapter_images(chapter_folder: str, output_ext: str):
    """
    تنفذ دمج الصور لملفات JPG/JPEG فقط، وتتجاهل باقي الصيغ.
    ثم تعيد ترقيم جميع الملفات الناتجة بالصيغة المطلوبة.
    """
    # نبحث عن الملفات التي قد تحتاج للدمج (المحفوظة كـ jpg أو jpeg مؤقتاً)
    mergeable_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(('.jpg', '.jpeg'))])
    
    num_mergeable = len(mergeable_files)
    merge_list = [] 
    
    i = 0
    while i + 1 < num_mergeable:
        file1_name = mergeable_files[i]
        file2_name = mergeable_files[i+1]
        merge_list.append((os.path.join(chapter_folder, file1_name), os.path.join(chapter_folder, file2_name)))
        i += 2
        
    # 1. تنفيذ الدمج
    for file1_path, file2_path in merge_list:
        try:
            img1 = Image.open(file1_path).convert("RGB")
            img2 = Image.open(file2_path).convert("RGB")
            
            max_width = max(img1.width, img2.width)
            total_height = img1.height + img2.height
            
            merged_img = Image.new('RGB', (max_width, total_height))
            merged_img.paste(img1, (0, 0))
            merged_img.paste(img2, (0, img1.height))
            
            merged_img.save(file1_path, 'jpeg', quality=90) # الحفظ كـ JPG مؤقتاً
            os.remove(file2_path)
            print(f"Merged {os.path.basename(file1_path)} and {os.path.basename(file2_path)}")

        except Exception as e:
            print(f"[ERROR LOG] Failed to merge images {os.path.basename(file1_path)} and {os.path.basename(file2_path)}: {e}")
            continue

    # 2. إعادة تسمية/تحويل جميع الملفات المتبقية إلى الصيغة النهائية المطلوبة
    final_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))])
    
    for index, filename in enumerate(final_files):
        current_path = os.path.join(chapter_folder, filename)
        new_filename = f"{index + 1:03d}.{output_ext}"
        new_path = os.path.join(chapter_folder, new_filename)
        
        try:
            # إذا لم تكن الصيغة الحالية هي الصيغة المطلوبة، نقوم بتحويلها
            if not filename.lower().endswith(output_ext):
                img = Image.open(current_path).convert("RGB")
                
                # حفظ بالصيغة المطلوبة
                if output_ext == 'webp':
                    img.save(new_path, 'webp', quality=90)
                elif output_ext == 'jpg':
                    img.save(new_path, 'jpeg', quality=90)
                else: # افتراضياً، png أو أي شيء آخر
                    img.save(new_path, output_ext)
                
                # حذف الملف الأصلي بعد التحويل
                os.remove(current_path)
            
            # إذا كانت الصيغة الحالية هي الصيغة المطلوبة، نقوم فقط بإعادة الترقيم
            elif filename != new_filename:
                 os.rename(current_path, new_path)
            
        except Exception as e:
             print(f"[ERROR LOG] Failed to convert/rename file {filename} to {new_filename}: {e}")


# --- أحداث البوت ---

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user}')
    try:
        # تسجيل الأوامر (Command Tree)
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
        # اختبار Dropbox
        dbx.users_get_current_account()
        print("Dropbox connection successful.")
    except Exception as e:
        print(f"Dropbox connection failed or slash commands sync failed: {e}")
        print(f"[ERROR LOG] Initial setup failed: {e}")


# --- أمر التطبيق (Slash Command) ---

@bot.tree.command(name="download", description="تحميل الصور من مواقع المانجا وضغطها ورفعها.")
@discord.app_commands.describe(
    url="رابط صفحة الفصل الأول (قد يحتوي على رقم الفصل أو لا)",
    chapters="عدد الفصول التي تريد تنزيلها بدءاً من هذا الفصل",
    merge_images="دمج الصور (صورتان في صورة واحدة)، يعمل فقط لـ JPG/JPEG",
    output_format="صيغة الصورة النهائية (jpg أو webp)"
)
@discord.app_commands.choices(
    output_format=[
        discord.app_commands.Choice(name="JPG (أكثر توافقاً)", value="jpg"),
        discord.app_commands.Choice(name="WEBP (أصغر حجماً)", value="webp"),
        # يمكنك إضافة خيارات أخرى مثل png هنا
    ]
)
async def download_command(
    interaction: discord.Interaction, 
    url: str,
    chapters: int,
    merge_images: bool = True,
    output_format: str = "jpg"
):
    user_mention = interaction.user.mention
    
    # التحقق من صيغة الإخراج المدعومة
    if output_format not in ['jpg', 'webp']:
         await interaction.response.send_message("❌ **صيغة الإخراج غير مدعومة.** الرجاء اختيار `jpg` أو `webp`.", ephemeral=True)
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
        await original_response.edit(embed=discord.Embed(title="❌ فشل التهيئة", description="**فشل في تهيئة متصفح Chrome/Selenium.**", color=discord.Color.red()))
        return

    # إنشاء مجلد العمل المؤقت
    if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    
    # --- تحليل الرابط واستخراج رقم الفصل الأول ---
    
    # 1. محاولة استخراج رقم الفصل من نمط 'chapter-XX'
    match_chapter = re.search(r'chapter-(\d+)', url, re.IGNORECASE)
    # 2. محاولة استخراج رقم الفصل من نمط 'no=XX' (لروابط Naver)
    match_no = re.search(r'no=(\d+)', url, re.IGNORECASE)
    
    if match_chapter:
        initial_chapter_num = int(match_chapter.group(1))
        # إنشاء نمط الرابط (مثل: /chapter-XX/ -> /chapter-{}/)
        base_url_pattern = re.sub(r'chapter-\d+', 'chapter-{}', url, 1, re.IGNORECASE)
        url_type = 'chapter'
    elif match_no:
        initial_chapter_num = int(match_no.group(1))
        # إنشاء نمط الرابط (مثل: &no=XX& -> &no={}&)
        base_url_pattern = re.sub(r'no=\d+', 'no={}', url, 1, re.IGNORECASE)
        url_type = 'no'
    else:
        # فشل التحليل
        shutil.rmtree(LOCAL_TEMP_DIR)
        driver.quit()
        print(f"[ERROR LOG] URL parsing failed: Could not find chapter number in URL: {url}")
        await original_response.edit(content="❌ **فشل في تحليل الرابط!** لم يتم العثور على رقم فصل في الرابط (سواء `chapter-XX` أو `no=XX`).")
        return

    chapters_processed = 0
    
    # --- حلقة معالجة الفصول ---
    for i in range(chapters):
        # في روابط Naver، يتم العد تنازليًا (مثل no=10, no=9, no=8) إذا كانت listSortOrder=DESC
        # في هذه الحالة، نستخدم i-1. سنفترض أن الزيادة هي المتوقعة ما لم يتم تحديد عكس ذلك.
        current_chapter_num = initial_chapter_num + i if url_type == 'chapter' else initial_chapter_num - i
        
        # إذا كان رقم الفصل سالباً (للروابط التنازلية)، نتوقف
        if current_chapter_num <= 0 and url_type != 'chapter':
             print(f"[INFO] Stopped processing as chapter number reached {current_chapter_num}.")
             break

        current_url = base_url_pattern.format(current_chapter_num)
        local_chapter_folder = os.path.join(LOCAL_TEMP_DIR, str(current_chapter_num))
        images_downloaded = 0
        
        try:
            initial_embed.description = (
                f"{user_mention} **جارِ جلب الفصل {current_chapter_num} ({i + 1} من {chapters})...** ⏳\n"
                f"الدمج: {'مفعّل' if merge_images else 'غير مفعّل'} | الإخراج: {output_format.upper()}"
            )
            await original_response.edit(embed=initial_embed)
            
            os.makedirs(local_chapter_folder, exist_ok=True)
            
            # 1. جلب الصفحة باستخدام Selenium والانتظار حتى تحميل الصور
            driver.get(current_url)
            
            # الانتظار حتى تحميل أول صورة
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'img.page-image, img[src*="cdn"], #toon_view_detail img'))
            )
            
            # تمرير الصفحة للأسفل لضمان تحميل جميع الصور (Lazy Loading)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            
            # إعطاء مهلة إضافية بسيطة للانتهاء من التحميل بعد التمرير
            await asyncio.sleep(3) 
            
            # 2. استخراج روابط الصور
            # توسيع نطاق البحث ليشمل موقع Naver (#toon_view_detail img)
            image_elements = driver.find_elements(By.CSS_SELECTOR, 'img.page-image, img[src*="cdn"], #toon_view_detail img')
            image_srcs = [img.get_attribute('src') for img in image_elements if img.get_attribute('src')]
            
            if not image_srcs: 
                # محاولة ثانية إذا فشلت طريقة CSS Selector
                body_html = driver.page_source
                if "페이지를 찾을 수 없습니다" in body_html or "Page Not Found" in body_html:
                    raise requests.exceptions.HTTPError("Chapter not found (404-like error).")

                print(f"[ERROR LOG] No images found via Selenium in chapter {current_chapter_num} at URL: {current_url}")
                shutil.rmtree(local_chapter_folder)
                continue
            
            # 3. تنزيل وحفظ الصور (باستخدام requests لفعالية التنزيل)
            image_counter = 1
            for img_src in image_srcs:
                if not img_src or img_src.startswith('data:'): continue

                img_obj = download_and_check_image(img_src)
                
                if img_obj:
                    # نستخدم 'jpg' كصيغة مؤقتة للحفظ قبل الدمج، باستثناء ملفات PNG الأصلية
                    ext = 'png' if img_obj.format == 'PNG' else 'jpg'
                    filename = f"{image_counter:03d}.{ext}"
                    local_file_path = os.path.join(local_chapter_folder, filename)
                    
                    if ext == 'png':
                        img_obj.save(local_file_path, 'png')
                    else:
                        img_obj.save(local_file_path, 'jpeg', quality=90)

                    images_downloaded += 1
                    image_counter += 1
            
            # 4. دمج وتحويل الصور
            if images_downloaded > 0:
                initial_embed.description = f"{user_mention} **جارِ دمج وتحويل الفصل {current_chapter_num}...** ⚙️"
                await original_response.edit(embed=initial_embed)
                
                if merge_images:
                    merge_chapter_images(local_chapter_folder, output_format) 
                else:
                    # إذا لم يكن هناك دمج، نقوم بالتحويل وإعادة الترقيم فقط
                    merge_chapter_images(local_chapter_folder, output_format) 

                chapters_processed += 1
            else:
                print(f"[ERROR LOG] No images were successfully downloaded in chapter {current_chapter_num}.")
                shutil.rmtree(local_chapter_folder)
            
        except requests.exceptions.HTTPError:
            print(f"[ERROR LOG] Chapter URL Error: Chapter {current_chapter_num} likely does not exist (404). Stopping chapter loop.")
            if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
            break # الخروج من حلقة الفصول إذا كان الرابط غير موجود
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
        description=f"{user_mention} **تم رفع الملف بنجاح!**\n\n"
                    f"**الملف المضغوط:** `{zip_filename}`\n"
                    f"**الدمج:** {'نعم' if merge_images else 'لا'} | **الصيغة:** {output_format.upper()}\n"
                    f"**رابط التحميل:** [اضغط هنا للتحميل]({shared_link})\n\n",
        color=discord.Color.green()
    )
    final_embed.set_footer(text="سيتم حذف الملف من Dropbox بعد 15 دقيقة.")
    
    await original_response.edit(embed=final_embed)

# تشغيل البوت
bot.run(DISCORD_BOT_TOKEN)
