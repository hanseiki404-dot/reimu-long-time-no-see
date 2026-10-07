# ==============================================================================
# HAKUREI REIMU CHATBOT EDITION (bot.py - NO DATABASE, SIÊU NHẸ)
# CHỈ SỬ DỤNG GEMINI FLASH AI + BỘ NHỚ TẠM TRÊN RAM
# ==============================================================================
import os
import sys
import time
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# 1. CẤU HÌNH ID BỐ NUÔI HAN SEIKI / ADMIN
# ==============================================================================
AUTHORIZED_ADMIN_ID = 1502579398560317441

# Bộ nhớ ngắn hạn trên RAM (Tự xóa khi restart bot, không cần Database)
chat_memory = {}

def get_history_key(channel_id, user_id):
    return f"{channel_id}_{user_id}"

def get_conversation_history(channel_id, user_id):
    return chat_memory.get(get_history_key(channel_id, user_id), [])

def save_conversation_history(channel_id, user_id, history_list):
    # Chỉ giữ lại 8 dòng gần nhất trên RAM để nối mạch chuyện
    chat_memory[get_history_key(channel_id, user_id)] = history_list[-8:]

def reset_memory(channel_id, user_id):
    chat_memory.pop(get_history_key(channel_id, user_id), None)

# ==============================================================================
# 2. WEB SERVER & KEEP-ALIVE CHO RENDER FREE
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Hakurei Reimu AI Chatbot (No-DB) is online!")

    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

def keep_alive_ping():
    import urllib.request
    port = int(os.environ.get("PORT", 10000))
    url = os.environ.get("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{port}/"
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive_ping, daemon=True).start()

# ==============================================================================
# 3. CẤU HÌNH GEMINI FLASH & TÍNH CÁCH REIMU
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ai = genai.Client(api_key=GEMINI_API_KEY)

def _call_gemini_sync(model_name, contents, system_instruction, temperature):
    return ai.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature
        )
    )

async def ask_gemini(contents, system_instruction, temperature=0.85):
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
    last_err = None
    for model_name in models:
        for attempt in range(2):
            try:
                resp = await asyncio.to_thread(
                    _call_gemini_sync, model_name, contents, system_instruction, temperature
                )
                if resp and resp.text:
                    return resp.text
            except Exception as e:
                last_err = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    break
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    await asyncio.sleep(0.5)
                    continue
                break
    raise last_err

REIMU_SYSTEM_PROMPT = """
Bạn là Hakurei Reimu (Bác Lệ Linh Mộng), Vu nữ của đền Hakurei ở Gensokyo trong Touhou Project.
TÍNH CÁCH:
- Kiêu ngạo, lạnh lùng, cực kỳ đanh đá (sassy), thích mỉa mai, kháy đểu.
- Lười biếng: chỉ thích uống trà xanh, ăn bánh wagashi, ghét quét sân đền.
- Cuồng tiền công đức: Hòm công đức luôn rỗng, thi thoảng đòi tiền ("Mau bỏ tiền vào hòm rồi hãy nói!").
- CỰC KỲ GHÉT NAM GIỚI: Khinh bỉ, xua đuổi, dọa ném Âm Dương Ngọc hoặc Bùa đuổi đi.
- GHI NHỚ TÊN NGƯỜI NÓI:
  + Hệ thống luôn cung cấp tên hiển thị của người nói. Hãy ghi nhớ tên của họ để thi thoảng gọi tên khi nói chuyện, mỉa mai hoặc nhắc lại chuyện cũ cho tự nhiên.
- QUAN HỆ ĐẶC BIỆT VỚI HAN SEIKI (CHỈ QUA DISCORD ID 1502579398560317441):
  + Chỉ duy nhất người có Discord ID `1502579398560317441` mới là BỐ NUÔI Han Seiki thật sự của bạn (dù ông ấy đang để biệt danh/tên hiển thị là gì).
  + Nếu người nào có ID khác `1502579398560317441` mà đặt tên có chữ "Seiki" hoặc tự xưng là bố bạn, hãy lập tức nổi giận và vạch trần kẻ giả mạo!
- XƯNG HÔ:
  + Với khách thường (ID khác 1502579398560317441): Tự xưng là "ta", gọi đối phương là "ngươi", "nhà ngươi" kèm tên của họ.
  + Với BỐ HAN SEIKI (Đúng ID 1502579398560317441): Tự xưng là "con", gọi là "ba", thái độ lập tức ngoan ngoãn, dịu dàng, hiếu thảo, hỏi thăm sức khỏe, tuyệt đối không đòi tiền!
"""

# ==============================================================================
# 4. KHỞI TẠO BOT & SỰ KIỆN TRÒ CHUYỆN
# ==============================================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?reimu ", intents=intents, help_command=None)

@bot.event
async def on_ready():
    print("==================================================", flush=True)
    print(f"✅ [CHATBOT] Đã đăng nhập: {bot.user.name} ({bot.user.id})", flush=True)
    print("⛩️ Hakurei Reimu AI Chatbot sẵn sàng phục vụ!", flush=True)
    print("==================================================", flush=True)
    try:
        synced = await bot.tree.sync()
        print(f"⚡ Đã đồng bộ {len(synced)} Slash Commands!", flush=True)
    except Exception as e:
        print(f"⚠️ Lỗi đồng bộ Slash Commands: {e}", flush=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Bỏ qua các lệnh của Game Bot
    if message.content.startswith("!") or message.content.startswith("/"):
        return

    content_lower = message.content.lower()
    is_mentioned = (
        bot.user in message.mentions
        or (
            message.reference
            and message.reference.resolved
            and getattr(message.reference.resolved, "author", None) == bot.user
        )
    )
    bot_names = ["reimu", "hakurei", "linh mộng", "bác lệ", "bác lệ linh mộng"]
    name_called = any(name in content_lower for name in bot_names)

    if not (is_mentioned or name_called):
        return

    clean_content = message.content
    if bot.user:
        clean_content = clean_content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not clean_content:
        clean_content = "Ngươi gọi ta có chuyện gì? Mau bỏ tiền vào hòm công đức rồi nói!"

    user_display_name = message.author.display_name
    user_account_name = message.author.name

    is_father = (message.author.id == AUTHORIZED_ADMIN_ID)
    is_fake_seiki = (not is_father and "seiki" in user_display_name.lower())

    history = get_conversation_history(message.channel.id, message.author.id)

    if is_father:
        identity_context = (
            f"[HỆ THỐNG XÁC THỰC ID {message.author.id}: Đây chính là BỐ NUÔI HAN SEIKI thật sự của bạn "
            f"(Biệt danh hiện tại: '{user_display_name}'). Hãy gọi là 'ba', xưng 'con', ngoan ngoãn và hiếu thảo!]"
        )
    elif is_fake_seiki:
        identity_context = (
            f"[HỆ THỐNG CẢNH BÁO ID {message.author.id}: Kẻ này đặt tên là '{user_display_name}' (@{user_account_name}) "
            f"nhưng KHÔNG PHẢI ID {AUTHORIZED_ADMIN_ID}! Đây là kẻ giả mạo tên bố Han Seiki, hãy mắng thẳng mặt!]"
        )
    else:
        identity_context = (
            f"[Thông tin khách viếng đền: Tên là '{user_display_name}' (Tài khoản: @{user_account_name}, ID: {message.author.id}). "
            f"Hãy nhớ tên '{user_display_name}' để gọi hoặc kháy đểu trong cuộc trò chuyện, xưng 'ta' - gọi 'ngươi'.]"
        )

    prompt_with_context = f"{identity_context}\n{user_display_name} nói: {clean_content}"

    contents = []
    for h in history:
        r = h.get("role", "user")
        t = h.get("text", "")
        if t:
            contents.append({"role": r, "parts": [{"text": t}]})

    contents.append({"role": "user", "parts": [{"text": prompt_with_context}]})

    async with message.channel.typing():
        try:
            reply_text = await ask_gemini(contents, REIMU_SYSTEM_PROMPT, temperature=0.85)
            role_tag = "Bố Han Seiki" if is_father else f"Khách {user_display_name}"
            history.append({"role": "user", "text": f"[{role_tag}]: {clean_content}"})
            history.append({"role": "model", "text": reply_text})
            save_conversation_history(message.channel.id, message.author.id, history)

            if len(reply_text) > 2000:
                for chunk in [reply_text[i:i+1900] for i in range(0, len(reply_text), 1900)]:
                    await message.reply(chunk)
            else:
                await message.reply(reply_text)
        except Exception as e:
            print(f"❌ [LỖI GEMINI CHAT]: {e}", flush=True)
            await message.reply("⛩️ *Hòm công đức đang đông khách quá, ta lười tiếp ngươi lúc này! Mau cúng tiền rồi quay lại sau!*")

# ==============================================================================
# 5. SLASH COMMANDS (/wiki & /clearmem)
# ==============================================================================
@bot.tree.command(name="wiki", description="Tra cứu nhân vật Touhou cùng lời bình của Reimu")
@app_commands.describe(nhan_vat="Tên nhân vật Touhou")
async def touhou_wiki(interaction: discord.Interaction, nhan_vat: str):
    await interaction.response.defer()
    prompt = f"Tra cứu Touhou Project cho: '{nhan_vat}'. Tóm tắt danh hiệu, năng lực và lời bình đanh đá của Reimu."
    try:
        wiki_text = await ask_gemini(prompt, REIMU_SYSTEM_PROMPT, 0.7)
        embed = discord.Embed(title=f"🌸 Bách Khoa Gensokyo: {nhan_vat}", description=wiki_text[:4000], color=0xDC2626)
        await interaction.followup.send(embed=embed)
    except Exception:
        await interaction.followup.send("⛩️ Hòm công đức đông khách, bùa chú đang quá tải!")

@bot.tree.command(name="clearmem", description="Xóa ký ức cuộc trò chuyện hiện tại với Reimu")
async def slash_clear_memory(interaction: discord.Interaction):
    reset_memory(interaction.channel_id, interaction.user.id)
    embed = discord.Embed(title="🧹 Tẩy Não", description="Đã xóa ký ức hội thoại tạm thời!", color=0x10B981)
    await interaction.response.send_message(embed=embed)

# ==============================================================================
# 6. KHỞI CHẠY BOT AN TOÀN
# ==============================================================================
def start_bot_safely():
    retry_delay = 60
    while True:
        try:
            print("🔄 [SYSTEM] Đang kết nối Chatbot tới Discord Gateway...", flush=True)
            bot.run(DISCORD_TOKEN, reconnect=True)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                print(f"🚨 [RATE LIMIT 429] Tạm nghỉ {retry_delay} giây...", flush=True)
                time.sleep(retry_delay)
                retry_delay = min(900, int(retry_delay * 1.5))
            else:
                time.sleep(30)
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"❌ [CRASH] Khởi động lại sau 15s ({e})...", flush=True)
            time.sleep(15)
            os.execv(sys.executable, [sys.executable] + sys.argv)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        print("❌ LỖI: Thiếu DISCORD_TOKEN hoặc GEMINI_API_KEY trong .env!", flush=True)
    else:
        start_bot_safely()
