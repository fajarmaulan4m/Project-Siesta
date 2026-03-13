import os
import time
import sys
import shutil
import psutil
import platform
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ButtonStyle
from config import Config

# --- TAMBAHAN IMPOR ---
from bot.settings import bot_set

# Simpan waktu start bot
BOT_START_TIME = time.time()

# --- HELPER FUNCTIONS ---

def get_readable_time(seconds: int) -> str:
    count = 0
    ping_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", "days"]
    while count < 4:
        count += 1
        remainder, result = divmod(seconds, 60) if count < 3 else divmod(seconds, 24)
        if seconds == 0 and remainder == 0:
            break
        time_list.append(int(result))
        seconds = int(remainder)
    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]
    if len(time_list) == 4:
        ping_time += time_list.pop() + ", "
    time_list.reverse()
    ping_time += ":".join(time_list)
    return ping_time

def sizeof_fmt(num, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'Yi', suffix)

def make_progress_bar(percentage):
    filled = int(percentage / 100 * 12)
    bar = "◙" * filled + "◘" * (12 - filled)
    return f"[{bar}]"

# --- SYSTEM INFO HELPERS ---

def get_distro_name():
    try:
        return subprocess.check_output("cat /etc/*release | grep ^PRETTY_NAME | cut -d= -f2", shell=True).decode().strip().replace('"', '')
    except:
        return platform.system()

def get_cpu_model():
    try:
        command = "cat /proc/cpuinfo | grep 'model name' | uniq | cut -d: -f2"
        output = subprocess.check_output(command, shell=True).decode().strip()
        if not output:
             command_arm = "cat /proc/cpuinfo | grep 'Model' | uniq | cut -d: -f2"
             output = subprocess.check_output(command_arm, shell=True).decode().strip()
        return output if output else platform.processor()
    except:
        return "Unknown CPU"

def get_packages_count():
    try:
        return subprocess.check_output("dpkg -l | grep -c ^ii", shell=True).decode().strip()
    except:
        return "N/A"

def get_host_info():
    if os.environ.get("RENDER"):
        return "Render (Cloud Container)"
    try:
        if os.path.exists("/sys/devices/virtual/dmi/id/product_name"):
            with open("/sys/devices/virtual/dmi/id/product_name", "r") as f:
                return f.read().strip()
    except: pass
    return platform.node()

def get_real_timezone():
    try:
        if os.path.islink("/etc/localtime"):
            return os.readlink("/etc/localtime").replace("/usr/share/zoneinfo/", "")
        if os.path.exists("/etc/timezone"):
            with open("/etc/timezone", "r") as f:
                return f.read().strip()
        return subprocess.check_output("date +%Z", shell=True).decode().strip()
    except: pass
    return time.tzname[0]

# --- CORE LOGIC (GENERATOR TEXT) ---
def generate_stats_text():
    """
    Fungsi ini dipisahkan agar bisa dipanggil oleh Command dan Tombol Refresh
    tanpa menulis ulang kode yang sama.
    """
    # 1. System Info
    uname = platform.uname()
    os_name = get_distro_name()
    kernel = uname.release
    arch = uname.machine
    host_name = get_host_info()
    timezone = get_real_timezone()
    shell = os.environ.get("SHELL", "/bin/bash").split("/")[-1]
    packages = get_packages_count()
    
    # Uptime
    boot_time_timestamp = psutil.boot_time()
    os_uptime = get_readable_time(int(time.time() - boot_time_timestamp))
    bot_uptime = get_readable_time(int(time.time() - BOT_START_TIME))
    
    # 2. Resources
    cpu_model = get_cpu_model()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_usage = psutil.cpu_percent()
    cpu_bar = make_progress_bar(cpu_usage)
    
    mem = psutil.virtual_memory()
    mem_used = sizeof_fmt(mem.used)
    mem_total = sizeof_fmt(mem.total)
    mem_percent = mem.percent
    mem_free = sizeof_fmt(mem.available)
    mem_bar = make_progress_bar(mem_percent)
    
    swap = psutil.swap_memory()
    swap_total = sizeof_fmt(swap.total) if swap.total > 0 else "Not Set"
    swap_used = sizeof_fmt(swap.used) if swap.total > 0 else "Not Set"
    
    total, used, free = shutil.disk_usage(Config.DOWNLOAD_BASE_DIR)
    disk_used = sizeof_fmt(used)
    disk_total = sizeof_fmt(total)
    disk_free = sizeof_fmt(free)
    disk_percent = int((used / total) * 100)
    disk_bar = make_progress_bar(disk_percent)
    
    net_io = psutil.net_io_counters()
    upload = sizeof_fmt(net_io.bytes_sent)
    download = sizeof_fmt(net_io.bytes_recv)
    total_bw = sizeof_fmt(net_io.bytes_sent + net_io.bytes_recv)
    
    text = f"""
<code>OS: {os_name} {arch}
Host: {host_name}
Kernel: {kernel}
Uptime: {os_uptime}
Packages: {packages} (dpkg)
Shell: {shell}
CPU: {cpu_model} ({cpu_count})
Memory: {mem_used} / {mem_total} ({mem_percent}%)
Swap: {swap_total}
Disk (/): {disk_used} / {disk_total} ({disk_percent}%) - overlay

SERVER AREA: {timezone}
BOT UPTIME : {bot_uptime}

「 DISK 」
{disk_bar} | {disk_percent}% of {disk_total}
Available : {disk_free}
Used      : {disk_used}

「 CPU 」
{cpu_bar} | {cpu_usage}%
Cores     : {cpu_count}
Logical   : {cpu_count}
Frequency : Disabled

「 MEMORY 」
{mem_bar} | {mem_percent}% of {mem_total}
Available : {mem_free}
Used      : {mem_used}

「 SWAP 」
Total     : {swap_total}
Used      : {swap_used}

「 BANDWIDTH TOTAL 」
Download  : {download}
Upload    : {upload}
All BW    : {total_bw}
</code>
"""
    return text

# --- MAIN COMMAND ---

@Client.on_message(filters.command(["stats", "status"]))
async def stats_handler(client, message):
    # Pengecekan: Jika Bot Public False, dan user bukan admin/auth, bot akan diam.
    user_id = message.from_user.id
    if not bot_set.bot_public and user_id not in bot_set.auth_users and user_id not in bot_set.admins:
        return

    msg = await message.reply("🔄 **Mengumpulkan Data...**", quote=True)
    
    # Ambil text dari fungsi generator
    final_text = generate_stats_text()
    
    # Tombol Refresh (Default) dan Tutup (Merah)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="stats_refresh")],
        [InlineKeyboardButton("Close", callback_data="stats_close", style=ButtonStyle.DANGER)]
    ])
    
    await msg.edit(final_text, reply_markup=buttons)

# --- CALLBACK: REFRESH STATS ---
@Client.on_callback_query(filters.regex("^stats_refresh$"))
async def refresh_stats_callback(client, query: CallbackQuery):
    # Ambil data terbaru
    new_text = generate_stats_text()
    
    # Tombol Refresh (Default) dan Tutup (Merah)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="stats_refresh")],
        [InlineKeyboardButton("Close", callback_data="stats_close", style=ButtonStyle.DANGER)]
    ])
    
    try:
        # Edit pesan dengan data baru
        await query.edit_message_text(
            new_text, 
            reply_markup=buttons
        )
        await query.answer("✅ Data Diperbarui!")
    except Exception:
        # Menghindari error jika tombol ditekan terlalu cepat (konten belum berubah)
        await query.answer("✅ Data sudah paling update!")

# --- CALLBACK: CLOSE ---
@Client.on_callback_query(filters.regex("^stats_close$"))
async def close_callback(client, query: CallbackQuery):
    await query.message.delete()
