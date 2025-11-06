import discord
from discord.ext import commands
import requests
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO
import dropbox
import re
import os
import asyncio
import uuid
import zipfile
import shutil
import glob

# --- الإعدادات والثوابت (كما هي) ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
MIN_WIDTH = 800      # الحد الأدنى لعرض الصورة بالبكسل
CLEANUP_DELAY_SECONDS = 900 # 15 دقيقة = 900 ثانية
LOCAL_TEMP_DIR = "manga_temp" 
IMAGE_DOWNLOAD_TIMEOUT = 15 # مهلة 15 ثانية لتحميل كل صورة

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

# دالة مساعدة لتنزيل صورة والتحقق من حجمها (مضافة مهلة انتظار)
def download_and_check_image(image_url):
    try:
        response = requests.get(image_url, stream=True, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        
        image_bytes = BytesIO(response.content)
        img = Image.open(image_bytes)
        
        # إذا كانت الصورة ليست PNG، نحولها لـ RGB لضمان التوافق مع JPEG
        if img.format != 'PNG':
            img = img.convert("RGB")
        
        if img.width >= MIN_WIDTH:
            image_bytes.seek(0)
            return img, img.format.lower() if img.format else 'jpg'
        else:
            return None, None
    except requests.exceptions.Timeout:
        print(f"Error processing image {image_url}: Request timed out after {IMAGE_DOWNLOAD_TIMEOUT}s")
        return None, None
    except Exception as e:
        print(f"Error processing image {image_url}: {e}")
        return None, None

async def cleanup_dropbox_file(dropbox_path: str, delay_seconds: int):
    """ينتظر 15 دقيقة ثم يحذف الملف المضغوط من Dropbox."""
    await asyncio.sleep(delay_seconds)
    try:
        dbx.files_delete_v2(dropbox_path)
        print(f"🗑️ تم حذف ملف ZIP ({dropbox_path}) بنجاح بعد {delay_seconds} ثواني.")
    except Exception as e:
        print(f"❌ فشل حذف ملف ZIP ({dropbox_path}): {e}")

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

# دالة لدمج الصور داخل مجلد الفصل محلياً (المعدلة)
def merge_chapter_images(chapter_folder: str):
    """
    تنفذ دمج الصور لملفات JPG/JPEG فقط.
    تتجاهل ملفات PNG.
    """
    
    # 1. فلترة وتحديد ملفات JPG/JPEG فقط للدمج
    jpeg_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(('.jpg', '.jpeg'))])
    
    num_jpeg = len(jpeg_files)
    
    # قائمة بأسماء الملفات المراد دمجها (أزواج)
    merge_list = [] 
    
    # تحديد أزواج الصور (كل اثنين مع بعض)
    i = 0
    while i + 1 < num_jpeg:
        file1_name = jpeg_files[i]
        file2_name = jpeg_files[i+1]
        merge_list.append((os.path.join(chapter_folder, file1_name), os.path.join(chapter_folder, file2_name)))
        i += 2
        
    # 2. تنفيذ الدمج على أزواج JPG/JPEG
    for file1_path, file2_path in merge_list:
        try:
            # نستخدم RGB للصور المدمجة لضمان التوافق مع JPEG
            img1 = Image.open(file1_path).convert("RGB")
            img2 = Image.open(file2_path).convert("RGB")
            
            max_width = max(img1.width, img2.width)
            total_height = img1.height + img2.height
            
            merged_img = Image.new('RGB', (max_width, total_height))
            merged_img.paste(img1, (0, 0))
            merged_img.paste(img2, (0, img1.height))
            
            # حفظ الصورة المدمجة بالاسم الأقدم (كـ JPEG)
            merged_img.save(file1_path, 'jpeg', quality=90) 
            
            # حذف الصورة الثانية
            os.remove(file2_path)
            print(f"Merged {os.path.basename(file1_path)} and {os.path.basename(file2_path)}")

        except Exception as e:
            print(f"Failed to merge images {os.path.basename(file1_path)} and {os.path.basename(file2_path)}: {e}")
            continue

    # 3. إعادة ترقيم جميع الملفات المتبقية (PNG + JPEG المدمجة/المفردة)
    
    # الحصول على جميع الملفات المتبقية
    final_files = sorted([f for f in os.listdir(chapter_folder) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
    
    for index, filename in enumerate(final_files):
        # تحديد الصيغة الأصلية
        ext = filename.split('.')[-1]
        # الترقيم الجديد (001.jpg, 002.png, 003.jpg, ...)
        new_filename = f"{index + 1:03d}.{ext}"
        
        if filename != new_filename:
            os.rename(os.path.join(chapter_folder, filename), os.path.join(chapter_folder, new_filename))


# أمر التطبيق (Slash Command)
@bot.tree.command(name="download", description="تحميل الصور من مواقع المانجا وضغطها ورفعها.")
@discord.app_commands.describe(
    url="رابط صفحة المانجا",
    chapter_number="رقم الفصل الأول",
    chapters="عدد الفصول"
)
async def download_command(
    interaction: discord.Interaction, 
    url: str,
    chapter_number: int,
    chapters: int
):
    user_mention = interaction.user.mention
    
    # الرسالة الابتدائية المظللة
    initial_embed = discord.Embed(
        title="📥 تحميل فصل المانهوا",
        description=f"{user_mention} **جارِ المعالجة، الرجاء الانتظار...** ⌛",
        color=discord.Color.dark_grey()
    )
    
    await interaction.response.send_message(embed=initial_embed, ephemeral=False)
    original_response = await interaction.original_response()

    if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    
    base_url_pattern = re.sub(r'chapter-\d+', 'chapter-{}', url)
    if '{}' not in base_url_pattern:
        shutil.rmtree(LOCAL_TEMP_DIR)
        await original_response.edit(content="❌ **فشل في تحليل الرابط!** تأكد من أن رقم الفصل مكتوب كـ `chapter-XX` في الرابط.")
        return

    chapters_processed = 0
    
    for current_chapter_num in range(chapter_number, chapter_number + chapters):
        current_url = base_url_pattern.format(current_chapter_num)
        local_chapter_folder = os.path.join(LOCAL_TEMP_DIR, str(current_chapter_num))
        images_downloaded = 0
        
        try:
            initial_embed.description = f"{user_mention} **جارِ تنزيل الفصل {current_chapter_num}، الرجاء الانتظار...** ⏳"
            await original_response.edit(embed=initial_embed)
            
            os.makedirs(local_chapter_folder, exist_ok=True)
            response = requests.get(current_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            image_tags = soup.find_all('img', class_='page-image') 
            if not image_tags: image_tags = soup.find_all('img')
            if not image_tags: 
                shutil.rmtree(local_chapter_folder)
                continue
            
            image_counter = 1
            for img_tag in image_tags:
                img_src = img_tag.get('src')
                if not img_src or img_src.startswith('data:'): continue
                if img_src.startswith('//'): img_src = 'https:' + img_src
                elif img_src.startswith('/'): img_src = current_url.split('/reader')[0] + img_src 

                img_obj, file_format = download_and_check_image(img_src)
                
                if img_obj:
                    # نحدد الصيغة بناءً على ما تم اكتشافه (png يبقى png، والباقي يكون jpeg)
                    ext = file_format if file_format == 'png' else 'jpg'
                    filename = f"{image_counter:03d}.{ext}"
                    local_file_path = os.path.join(local_chapter_folder, filename)
                    
                    # حفظ كائن الصورة (PNG تحفظ كـ PNG، و JPEG تحفظ كـ JPEG)
                    if ext == 'png':
                        img_obj.save(local_file_path, 'png')
                    else:
                        img_obj.save(local_file_path, 'jpeg', quality=90)

                    images_downloaded += 1
                    image_counter += 1
            
            # --- دمج الصور بعد التنزيل ---
            if images_downloaded > 0:
                initial_embed.description = f"{user_mention} **جارِ دمج وضغط الفصل {current_chapter_num}...** ⚙️"
                await original_response.edit(embed=initial_embed)
                merge_chapter_images(local_chapter_folder) # تنفيذ دمج الصور
                chapters_processed += 1
            else:
                shutil.rmtree(local_chapter_folder)
            
        except Exception as e:
            print(f"Error in chapter {current_chapter_num}: {e}")
            if os.path.exists(local_chapter_folder): shutil.rmtree(local_chapter_folder)
            continue
    
    if chapters_processed == 0:
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        await original_response.edit(embed=discord.Embed(title="❌ فشل", description="**لم يتم معالجة أو تنزيل أي فصول بنجاح.**", color=discord.Color.red()))
        return

    # --- مرحلة الضغط والرفع ---
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
        await original_response.edit(content=f"```ini\n[ ❌ خطأ ]\n```\n**فشل في عملية ضغط الملفات: {e}**")
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        if os.path.exists(local_zip_path): os.remove(local_zip_path)
        return
    
    dropbox_path = f"/{zip_filename}"
    try:
        with open(local_zip_path, 'rb') as f:
            dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode('overwrite'))
    except Exception as e:
        await original_response.edit(content=f"```ini\n[ ❌ خطأ ]\n```\n**فشل في عملية الرفع إلى Dropbox: {e}**")
        if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
        if os.path.exists(local_zip_path): os.remove(local_zip_path)
        return

    # --- إنشاء رابط المشاركة والتنظيف النهائي ---
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

    if os.path.exists(LOCAL_TEMP_DIR): shutil.rmtree(LOCAL_TEMP_DIR)
    if os.path.exists(local_zip_path): os.remove(local_zip_path)
        
    bot.loop.create_task(cleanup_dropbox_file(dropbox_path, CLEANUP_DELAY_SECONDS))

    # --- رسالة الملخص النهائية (تنسيق التضمين) ---
    final_embed = discord.Embed(
        title="✅ تم الرفع إلى Dropbox",
        description=f"{user_mention} **تم رفع الملف بنجاح! يمكنك تحميله من الرابط التالي:**\n\n"
                    f"{shared_link}\n\n",
        color=discord.Color.green()
    )
    final_embed.set_footer(text="انسخ الرابط أعلاه لفتح الملف 📥")
    
    await original_response.edit(embed=final_embed)

# تشغيل البوت
bot.run(DISCORD_BOT_TOKEN)
