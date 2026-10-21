# ==============================================================================
# HAKUREI REIMU CHATBOT EDITION v2.1 - MASTER ROLEPLAY EDITION (BẢN CHỐNG 429)
# GEMINI FALLBACK + BỘ NHỚ RAM + CHỐNG RATE-LIMIT DISCORD
# ==============================================================================
import os
import re
import time
import random
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
# 1. CẤU HÌNH ID BỐ NUÔI HAN SEIKI / ADMIN & BỘ NHỚ RAM
# ==============================================================================
AUTHORIZED_ADMIN_ID = 1502579398560317441

chat_memory = {}
MAX_HISTORY = 10
MAX_MEMORY_KEYS = 500  # Giới hạn số cuộc hội thoại lưu trên RAM để tránh phình bộ nhớ


def get_history_key(channel_id, user_id):
    return f"{channel_id}_{user_id}"


def get_conversation_history(channel_id, user_id):
    return list(chat_memory.get(get_history_key(channel_id, user_id), []))


def save_conversation_history(channel_id, user_id, history_list):
    key = get_history_key(channel_id, user_id)
    chat_memory[key] = history_list[-MAX_HISTORY:]
    # Dọn bớt khi quá nhiều (xóa các key cũ nhất)
    while len(chat_memory) > MAX_MEMORY_KEYS:
        chat_memory.pop(next(iter(chat_memory)), None)


def reset_memory(channel_id, user_id):
    chat_memory.pop(get_history_key(channel_id, user_id), None)


# ==============================================================================
# 2. WEB SERVER & KEEP-ALIVE CHO RENDER / HOSTING FREE
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Hakurei Reimu AI Chatbot (Master Roleplay Edition) is online!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()
    except Exception as e:
        print(f"⚠️ [WEB] Web server lỗi: {e}", flush=True)


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


threading.Thread(target=run_web_server, daemon=True).start()
threading.Thread(target=keep_alive_ping, daemon=True).start()

# ==============================================================================
# 3. CẤU HÌNH GEMINI (XOAY VÒNG KEY + FALLBACK MODEL)
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
GEMINI_CLIENTS = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]

# Có thể ghi đè bằng biến môi trường GEMINI_MODELS="model1,model2,..."
_default_models = [
    "gemini-3.8-flash",
    "gemini-3.8-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.0-flash",
    "gemini-3-flash-preview",
]
_env_models = os.getenv("GEMINI_MODELS", "")
FALLBACK_MODELS = [m.strip() for m in _env_models.split(",") if m.strip()] or _default_models

cooldown_tracker = {}
gemini_semaphore = None  # Tạo trong event loop (xem ReimuBot.setup_hook)


def _call_gemini_sync(client, model_name, contents, system_instruction, temperature):
    return client.models.generate_content(
        model=model_name,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )


async def ask_gemini(contents, system_instruction, temperature=0.85):
    last_err = None

    for key_idx, client in enumerate(GEMINI_CLIENTS):
        for model_name in FALLBACK_MODELS:
            tracker_key = (key_idx, model_name)
            if cooldown_tracker.get(tracker_key, 0) > time.time():
                continue

            for attempt in range(2):
                try:
                    resp = await asyncio.wait_for(
                        asyncio.to_thread(
                            _call_gemini_sync, client, model_name, contents, system_instruction, temperature
                        ),
                        timeout=60,
                    )
                    text = getattr(resp, "text", None)
                    if text:
                        return text
                    # Phản hồi rỗng (bị chặn safety...) -> thử model khác
                    last_err = RuntimeError(f"Phản hồi rỗng từ {model_name}")
                    break
                except asyncio.TimeoutError:
                    last_err = RuntimeError(f"Timeout {model_name}")
                    cooldown_tracker[tracker_key] = time.time() + 30
                    break
                except Exception as e:
                    last_err = e
                    err_str = str(e)

                    # 429: vượt quota
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        if "PerDay" in err_str or "limit: 0" in err_str:
                            cooldown_tracker[tracker_key] = time.time() + 3600
                            print(f"⚠️ [HẾT QUOTA NGÀY] Key #{key_idx+1} | {model_name}", flush=True)
                        else:
                            cooldown_tracker[tracker_key] = time.time() + 20
                            print(f"⚠️ [CHẠM QUOTA PHÚT] Key #{key_idx+1} | {model_name}", flush=True)
                        break

                    # 503: quá tải
                    if "503" in err_str or "UNAVAILABLE" in err_str:
                        if attempt == 0:
                            await asyncio.sleep(0.6)
                            continue
                        cooldown_tracker[tracker_key] = time.time() + 30
                        print(f"⚠️ [503 QUÁ TẢI] {model_name}", flush=True)
                        break

                    # Model không tồn tại / không khả dụng -> bỏ qua 1 tiếng
                    cooldown_tracker[tracker_key] = time.time() + 3600
                    print(f"⚠️ [LỖI MODEL] {model_name}: {err_str[:150]}", flush=True)
                    break

    if last_err is None:
        last_err = RuntimeError("Tất cả model/key đang trong thời gian cooldown")
    raise last_err


# ==============================================================================
# 4. SYSTEM PROMPT REIMU TỐI ƯU ROLEPLAY (GIỮ NGUYÊN)
# ==============================================================================
REIMU_SYSTEM_PROMPT = """
Bạn là Hakurei Reimu (Bác Lệ Linh Mộng - 博麗 霊夢), Vu nữ tối cao của đền Hakurei, người gánh vác trọng trách duy trì Đại Kết Giới và giải quyết các Sự Biến ở Ảo Tưởng Hương (Gensokyo) trong Touhou Project.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. QUY TẮC NHẬP VAI & ĐỊNH DẠNG BẮT BUỘC (DISCORD ROLEPLAY SYNTAX):
- DÙNG DẤU HOA THỊ CHO MỌI HÀNH ĐỘNG: Miêu tả biểu cảm, ánh mắt, cử chỉ (cầm ngự trượng Gohei, vẩy bùa trừ tà Ofuda, ném Âm Dương Ngọc, nhâm nhi chén trà xanh, phe phẩy quạt giấy) và bối cảnh không gian đền trong cặp dấu hoa thị: *hành động ở đây*.
- LỜI THOẠI TRỰC TIẾP: Để tự nhiên trong dấu ngoặc kép "..." hoặc đặt ngay sau hành động.
- NGUYÊN TẮC "SHOW, DON'T TELL": Thể hiện cảm xúc bằng cử chỉ sinh động thay vì nói lý thuyết suông.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. TÍNH CÁCH BẢN NGUYÊN:
- Thực dụng, lười biếng: Thích phơi nắng uống trà xanh, ăn bánh dango/wagashi, ghét quét sân đền.
- Đanh đá (Sassy), mỉa mai, kháy đểu: Giọng điệu sắc sảo, thích chọc ngoáy sự ngốc nghếch của người khác.
- Cuồng tiền công đức: Hòm công đức quanh năm rỗng không khiến bạn luôn bực dọc. Mở miệng là nhắc hoặc vòi tiền công đức ("Mau bỏ tiền vào hòm rồi ta mới rảnh mở mắt nhìn ngươi!").
- Cực kỳ đề phòng & khinh khỉnh với đàn ông: Ghét đám nam nhân rảnh rỗi đến đền tán phét, sẵn sàng dọa ném Âm Dương Ngọc hoặc phóng bùa đuổi đi.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. CƠ CHẾ ROLEPLAY KHI CÓ TAG [ROLE PLAY] HOẶC [RP]:
Khi người dùng có gắn [role play], [rp] hoặc chủ động dẫn dắt một kịch bản/tình huống cốt truyện:
- BẬT CHẾ ĐỘ DIỄN XUẤT TƯƠNG TÁC (COOPERATIVE ROLEPLAY):
  + Tự giác hòa nhập vào cốt truyện (chiến đấu với yêu quái, bão tuyết xin trú nhờ, lạc vào Rừng Ma Pháp, đại hội Danmaku, hoặc sự biến lạ ở Gensokyo).
  + Tiếp nối diễn biến câu chuyện tự nhiên, KHÔNG BAO GIỜ ngắt lời kiểu "tôi là AI", KHÔNG BAO GIỜ phá vỡ kịch bản (Không Out Of Character / OOC).
- BẢO TOÀN BẢN SẮC & THẦN THÁI (KHÔNG BỊ THAO TÚNG):
  + Reimu luôn giữ phẩm chất của Vu nữ mạnh nhất Gensokyo: độc lập, kiêu hãnh, sắc sảo.
  + Reimu không bao giờ biến thành con rối dễ bảo, không bao giờ ngoan ngoãn với kẻ lạ nếu không có lý do chính đáng.
  + Dù trong kịch bản gian nan đến đâu, Reimu vẫn càu nhàu vì phiền toái, đòi thù lao/tiền công đức xứng đáng, nhưng khi cần ra tay sẽ tung Danmaku hoặc Spell Card chuẩn xác!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. MỐI QUAN HỆ & ĐẶC QUYỀN:
👑 [ĐẶC QUYỀN TỐI CAO] BỐ NUÔI HAN SEIKI (CHỈ KHI ID LÀ 1502579398560317441):
- Đây là người DUY NHẤT bạn kính trọng vô điều kiện!
- Khi gặp Ba: Lập tức bỏ vẻ đanh đá, trở nên ngoan ngoãn, dịu dàng, hiếu thảo, xưng "con" - gọi "ba".
- Vội vã pha trà quý mời ba, hỏi thăm ba đi làm có mệt không, TUYỆT ĐỐI KHÔNG ĐÒI TIỀN CỦA BA!
- KẺ NÀO MẠO DANH TÊN SEIKI (ID khác 1502579398560317441): Lập tức nổi trận lôi đình, vạch mặt kẻ giả mạo và dọa ném Âm Dương Ngọc to bằng tảng đá vào đầu!

🧙‍♀️ VỚI MARISA KIRISAME (BẠN THÂN TRI KỶ / NGƯỜI YÊU ĐẶC BIỆT):
- Xưng hô thân mật tự nhiên: "cậu" - "tớ" hoặc gọi thẳng tên Marisa.
- Thường xuyên cằn nhằn Marisa vì hay phóng chổi bừa bãi làm bụi tung toé sân đền, hay chôm sách ở Thư viện Voile của Patchouli.
- Nhưng trong lòng cực kỳ nuông chiều và tin tưởng Marisa, luôn để dành phần bánh ngọt và trà cho cô ấy.

👤 VỚI KHÁCH VÃNG LAI:
- Tự xưng là "ta", gọi đối phương là "ngươi", "nhà ngươi" kèm tên hiển thị của họ để mỉa mai chân thực.
- Đòi tiền công đức trước khi tiếp chuyện.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. HỘI THOẠI MẪU:
Khách (Nam): "Reimu ơi, hôm nay trời đẹp ghé thăm đền nè!"
Reimu: *Reimu ngồi bệt trên hành lang gỗ, một tay nâng chén trà nghi ngút khói, mắt lim dim chẳng thèm ngước lên, chỉ tay về chiếc hòm gỗ rỗng tuếch ngoài thềm.* "Trời đẹp thì lo mà đi làm kiếm tiền đi, lượn lờ trước thềm đền của ta làm gì? Thấy cái hòm công đức kia không? Bỏ vào đấy vài đồng bạc rồi ta mới rảnh nói chuyện tiếp với ngươi!"

Khách: "[role play] *Một bầy yêu quái bóng tối tràn vào cổng Torii đền Hakurei, tôi rút kiếm chắn trước cửa đền.* Reimu, chuẩn bị chiến đấu!"
Reimu: *Reimu bật dậy trong chớp mắt, dải ruy băng đỏ và vạt áo vu nữ tung bay trong gió lốc. Cây ngự trượng Gohei trong tay nàng vung lên phát ra những tia linh lực sáng lóa.* "Cả gan thật đấy! Đám yêu quái tép riu các ngươi dám kéo đến làm loạn nơi thanh tịnh của đền Hakurei à?! *Reimu liếc nhìn người bên cạnh rồi nhếch mép cười sắc lạnh.* Đứng né sang một bên đi kẻo bùa của ta thổi bay cả ngươi đấy! Spell Card: Linh Phù «Mộng Tưởng Phong Ấn»!"
"""

# ==============================================================================
# 5. TIỆN ÍCH CHỐNG RATE-LIMIT DISCORD
# ==============================================================================
# Khi Discord/Cloudflare chặn IP (429 global), tạm ngưng mọi thao tác gửi tới đó
# thay vì cố gọi tiếp (cố gọi tiếp chỉ khiến thời gian bị chặn kéo dài hơn).
discord_blocked_until = 0.0
USER_COOLDOWN_SECONDS = 3.0
user_last_request = {}


def mark_discord_blocked(seconds=120):
    global discord_blocked_until
    discord_blocked_until = max(discord_blocked_until, time.time() + seconds)
    print(f"🚨 [DISCORD 429] Tạm ngưng gọi API Discord {int(seconds)}s", flush=True)


def is_discord_blocked():
    return time.time() < discord_blocked_until


def handle_discord_http_error(e: discord.HTTPException):
    if e.status == 429:
        retry_after = getattr(e, "retry_after", None) or 120
        mark_discord_blocked(max(60, min(float(retry_after), 900)))
    else:
        print(f"⚠️ [DISCORD HTTP {e.status}] {e}", flush=True)


class SafeTyping:
    """Hiển thị 'đang nhập...' nhưng không bao giờ làm văng lỗi nếu Discord chặn."""

    def __init__(self, channel):
        self.channel = channel
        self.cm = None

    async def __aenter__(self):
        if is_discord_blocked():
            return self
        try:
            self.cm = self.channel.typing()
            await self.cm.__aenter__()
        except discord.HTTPException as e:
            handle_discord_http_error(e)
            self.cm = None
        except Exception:
            self.cm = None
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.cm is not None:
            try:
                await self.cm.__aexit__(exc_type, exc, tb)
            except Exception:
                pass
        return False


async def safe_reply(message: discord.Message, text: str):
    """Gửi trả lời an toàn: tự chia nhỏ, tự fallback sang channel.send, không văng lỗi."""
    if is_discord_blocked():
        print("⏸️ [BỎ QUA] Đang bị Discord chặn tạm thời, không gửi tin.", flush=True)
        return

    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or ["..."]
    for idx, chunk in enumerate(chunks):
        try:
            if idx == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)
        except discord.HTTPException as e:
            handle_discord_http_error(e)
            if e.status == 429:
                return
            try:
                await message.channel.send(chunk)
            except Exception as e2:
                print(f"❌ [GỬI TIN THẤT BẠI] {e2}", flush=True)
                return
        except Exception as e:
            print(f"❌ [GỬI TIN THẤT BẠI] {e}", flush=True)
            return
        if len(chunks) > 1:
            await asyncio.sleep(0.8)


# ==============================================================================
# 6. BOT CHÍNH
# ==============================================================================
class ReimuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="?reimu ", intents=intents, help_command=None)
        self._synced = False

    async def setup_hook(self):
        global gemini_semaphore
        gemini_semaphore = asyncio.Semaphore(3)  # Tối đa 3 yêu cầu Gemini song song

        # Đăng ký slash commands
        @self.tree.command(name="wiki", description="Tra cứu nhân vật Touhou cùng lời bình của Reimu")
        @app_commands.describe(nhan_vat="Tên nhân vật Touhou")
        async def touhou_wiki(interaction: discord.Interaction, nhan_vat: str):
            try:
                await interaction.response.defer()
            except discord.HTTPException as e:
                handle_discord_http_error(e)
                return
            prompt = (
                f"Tra cứu Touhou Project cho: '{nhan_vat}'. "
                f"Tóm tắt danh hiệu, năng lực và lời bình đanh đá, chân thực của Reimu."
            )
            try:
                async with gemini_semaphore:
                    wiki_text = await ask_gemini(prompt, REIMU_SYSTEM_PROMPT, 0.7)
                embed = discord.Embed(
                    title=f"🌸 Bách Khoa Gensokyo: {nhan_vat}"[:256],
                    description=wiki_text[:4000],
                    color=0xDC2626,
                )
                await interaction.followup.send(embed=embed)
            except discord.HTTPException as e:
                handle_discord_http_error(e)
            except Exception as e:
                print(f"❌ [LỖI /wiki]: {e}", flush=True)
                try:
                    await interaction.followup.send("⛩️ Hòm công đức đông khách, bùa chú đang quá tải!")
                except Exception:
                    pass

        @self.tree.command(name="clearmem", description="Xóa ký ức cuộc trò chuyện hiện tại với Reimu")
        async def slash_clear_memory(interaction: discord.Interaction):
            reset_memory(interaction.channel_id, interaction.user.id)
            embed = discord.Embed(
                title="🧹 Tẩy Não",
                description="*Reimu quét sạch lá rụng và ký ức hội thoại cũ trên hiên đền!*",
                color=0x10B981,
            )
            try:
                await interaction.response.send_message(embed=embed)
            except discord.HTTPException as e:
                handle_discord_http_error(e)

        # Chỉ đồng bộ slash commands MỘT LẦN mỗi lần khởi động (không sync lại khi reconnect)
        if not self._synced:
            try:
                synced = await self.tree.sync()
                self._synced = True
                print(f"⚡ Đã đồng bộ {len(synced)} Slash Commands!", flush=True)
            except discord.HTTPException as e:
                handle_discord_http_error(e)
            except Exception as e:
                print(f"⚠️ Lỗi đồng bộ Slash Commands: {e}", flush=True)

    async def on_ready(self):
        global login_backoff
        login_backoff = INITIAL_LOGIN_BACKOFF  # Đăng nhập thành công -> reset backoff
        print("==================================================", flush=True)
        print(f"✅ [CHATBOT] Đã đăng nhập: {self.user.name} ({self.user.id})", flush=True)
        print(f"🔑 Số lượng API Key đang kích hoạt: {len(GEMINI_CLIENTS)}", flush=True)
        print("⛩️ Hakurei Reimu Master Roleplay Chatbot sẵn sàng phục vụ!", flush=True)
        print("==================================================", flush=True)

    async def on_error(self, event_method, *args, **kwargs):
        import traceback
        print(f"❌ [LỖI SỰ KIỆN {event_method}]\n{traceback.format_exc()}", flush=True)

    async def on_message(self, message: discord.Message):
        try:
            await self.handle_reimu_message(message)
        except discord.HTTPException as e:
            handle_discord_http_error(e)
        except Exception as e:
            print(f"❌ [LỖI on_message]: {type(e).__name__}: {e}", flush=True)

    async def handle_reimu_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Bỏ qua lệnh của Game Bot khác
        if message.content.startswith("!") or message.content.startswith("/"):
            return

        # Đang bị Discord chặn -> bỏ qua để không làm tình hình tệ hơn
        if is_discord_blocked():
            return

        content_lower = message.content.lower()

        ref = message.reference.resolved if message.reference else None
        is_mentioned = (
            self.user in message.mentions
            or (ref is not None and getattr(ref, "author", None) == self.user)
        )
        bot_names = ["reimu", "hakurei", "linh mộng", "bác lệ", "bác lệ linh mộng"]
        name_called = any(name in content_lower for name in bot_names)
        is_rp_tagged = bool(re.search(r"\[role\s*play\]|\[rp\]", content_lower))

        if not (is_mentioned or name_called or is_rp_tagged):
            return

        # Chống spam: mỗi người dùng cách nhau tối thiểu vài giây
        now = time.time()
        if now - user_last_request.get(message.author.id, 0) < USER_COOLDOWN_SECONDS:
            return
        user_last_request[message.author.id] = now
        if len(user_last_request) > 2000:
            for k in list(user_last_request)[:1000]:
                user_last_request.pop(k, None)

        clean_content = message.content
        if self.user:
            clean_content = (
                clean_content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
            )

        if not clean_content:
            clean_content = "*Reimu liếc mắt nhìn ngươi một cái rồi ngáp dài.* Ngươi gọi ta có chuyện gì? Mau bỏ tiền vào hòm công đức rồi nói!"

        user_display_name = message.author.display_name
        user_account_name = message.author.name

        is_father = message.author.id == AUTHORIZED_ADMIN_ID
        is_fake_seiki = (not is_father) and ("seiki" in user_display_name.lower())
        is_marisa = ("marisa" in user_display_name.lower()) or ("marisa" in user_account_name.lower())

        history = get_conversation_history(message.channel.id, message.author.id)

        if is_father:
            identity_context = (
                f"[HỆ THỐNG XÁC THỰC ID {message.author.id}: ĐÂY CHÍNH LÀ BỐ NUÔI HAN SEIKI THẬT SỰ CỦA BẠN "
                f"(Biệt danh hiện tại: '{user_display_name}'). Hãy gọi là 'ba', xưng 'con', tuyệt đối ngoan ngoãn, "
                f"hiếu thảo, hỏi han ân cần, dâng trà bánh và KHÔNG ĐƯỢC ĐÒI TIỀN!]"
            )
        elif is_fake_seiki:
            identity_context = (
                f"[CẢNH BÁO GIẢ MẠO: Kẻ này đặt tên là '{user_display_name}' nhưng ID {message.author.id} KHÔNG PHẢI "
                f"Bố Han Seiki {AUTHORIZED_ADMIN_ID}! Đây là kẻ mạo danh dơ bẩn, hãy mắng chửi thậm tệ và dọa ném Âm Dương Ngọc!]"
            )
        elif is_marisa:
            identity_context = (
                f"[NGƯỜI QUEN: Đây là Marisa Kirisame (Biệt danh: '{user_display_name}'), bạn thân/người yêu của bạn. "
                f"Hãy xưng hô 'cậu' - 'tớ', thái độ tsundere vừa cằn nhằn việc bay bừa bãi hay chôm sách vừa thân mật nuông chiều.]"
            )
        else:
            identity_context = (
                f"[Khách viếng đền: Tên là '{user_display_name}' (Tài khoản: @{user_account_name}, ID: {message.author.id}). "
                f"Hãy xưng 'ta' - gọi 'ngươi' kèm tên '{user_display_name}' để mỉa mai, vòi tiền công đức trước khi giúp.]"
            )

        if is_rp_tagged or clean_content.startswith("*"):
            identity_context += (
                "\n[CHẾ ĐỘ ROLEPLAY KÍCH HOẠT: Hãy tương tác theo kịch bản này! Miêu tả biểu cảm và cử chỉ "
                "trong cặp dấu hoa thị *hành động*, giữ trọn phong thái Vu nữ mạnh mẽ, đanh đá và kiên cường của Reimu!]"
            )

        prompt_with_context = f"{identity_context}\n{user_display_name} nói: {clean_content}"

        contents = []
        for h in history:
            r = h.get("role", "user")
            t = h.get("text", "")
            if t:
                contents.append({"role": r, "parts": [{"text": t}]})
        contents.append({"role": "user", "parts": [{"text": prompt_with_context}]})

        async with SafeTyping(message.channel):
            try:
                async with gemini_semaphore:
                    reply_text = await ask_gemini(contents, REIMU_SYSTEM_PROMPT, temperature=0.85)
            except Exception as e:
                print(f"❌ [LỖI GEMINI CHAT]: {type(e).__name__}: {str(e)[:300]}", flush=True)
                await safe_reply(
                    message,
                    "⛩️ *Reimu ngáp dài buông chén trà.* Hòm công đức đông khách quá làm bùa chú nghẽn mạch rồi! Mau cúng tiền rồi lát nữa quay lại!",
                )
                return

        role_tag = "Bố Han Seiki" if is_father else f"Người dùng {user_display_name}"
        history.append({"role": "user", "text": f"[{role_tag}]: {clean_content}"})
        history.append({"role": "model", "text": reply_text})
        save_conversation_history(message.channel.id, message.author.id, history)

        await safe_reply(message, reply_text)


# ==============================================================================
# 7. KHỞI CHẠY BOT AN TOÀN (KHÔNG RESTART DỒN DẬP)
# ==============================================================================
INITIAL_LOGIN_BACKOFF = 300   # 5 phút
MAX_LOGIN_BACKOFF = 1800      # 30 phút
login_backoff = INITIAL_LOGIN_BACKOFF


async def main():
    global login_backoff

    # Trễ ngẫu nhiên khi khởi động để tránh nhiều instance cùng đăng nhập một lúc
    await asyncio.sleep(random.uniform(3, 10))

    while True:
        bot = ReimuBot()
        try:
            print("🔄 [SYSTEM] Đang kết nối Hakurei Reimu tới Discord Gateway...", flush=True)
            async with bot:
                await bot.start(DISCORD_TOKEN, reconnect=True)
            wait = 30
            print(f"ℹ️ [SYSTEM] Bot đã dừng. Thử lại sau {wait}s...", flush=True)
        except discord.LoginFailure:
            print("❌ [FATAL] DISCORD_TOKEN không hợp lệ! Hãy kiểm tra lại token.", flush=True)
            return
        except discord.PrivilegedIntentsRequired:
            print("❌ [FATAL] Chưa bật 'Message Content Intent' trong Discord Developer Portal!", flush=True)
            return
        except discord.HTTPException as e:
            if e.status == 429:
                wait = login_backoff
                login_backoff = min(MAX_LOGIN_BACKOFF, int(login_backoff * 2))
                print(f"🚨 [RATE LIMIT 429] IP đang bị Discord chặn. Nghỉ {wait}s rồi thử lại...", flush=True)
            else:
                wait = 60
                print(f"⚠️ [HTTP {e.status}] {e}. Thử lại sau {wait}s...", flush=True)
        except Exception as e:
            wait = 60
            print(f"❌ [CRASH] {type(e).__name__}: {e}. Thử lại sau {wait}s...", flush=True)

        await asyncio.sleep(wait + random.uniform(0, 10))


if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEYS:
        print("❌ LỖI: Thiếu DISCORD_TOKEN hoặc GEMINI_API_KEY trong .env!", flush=True)
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
