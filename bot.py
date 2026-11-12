# ==============================================================================
# HAKUREI REIMU CHATBOT EDITION v2.2 - MASTER ROLEPLAY EDITION
# - Tự dò model Gemini còn tồn tại (hết lỗi 404 NOT_FOUND)
# - Dùng client async (aio) + timeout + retry theo từng model/key
# - Giảm tối đa số request gửi tới Discord để tránh 429
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


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ==============================================================================
# 1. CẤU HÌNH ID BỐ NUÔI HAN SEIKI / ADMIN & BỘ NHỚ RAM
# ==============================================================================
AUTHORIZED_ADMIN_ID = 1502579398560317441

chat_memory = {}
MAX_HISTORY = 10
MAX_MEMORY_KEYS = 500


def get_history_key(channel_id, user_id):
    return f"{channel_id}_{user_id}"


def get_conversation_history(channel_id, user_id):
    return list(chat_memory.get(get_history_key(channel_id, user_id), []))


def save_conversation_history(channel_id, user_id, history_list):
    key = get_history_key(channel_id, user_id)
    trimmed = history_list[-MAX_HISTORY:]
    # Gemini cần lịch sử bắt đầu bằng lượt của "user"
    while trimmed and trimmed[0].get("role") != "user":
        trimmed.pop(0)
    chat_memory[key] = trimmed
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
# 3. CẤU HÌNH GEMINI (XOAY VÒNG KEY + FALLBACK MODEL + TỰ DÒ MODEL)
# ==============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
GEMINI_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
GEMINI_CLIENTS = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]

# Danh sách ưu tiên (có thể ghi đè bằng biến môi trường GEMINI_MODELS="a,b,c").
# Model nào KHÔNG tồn tại trên API sẽ tự bị loại nhờ bước dò model bên dưới.
_DEFAULT_MODELS = (
    "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash,"
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
    "gemini-flash-latest,gemini-flash-lite-latest"
)
PREFERRED_MODELS = [
    m.strip() for m in os.getenv("GEMINI_MODELS", _DEFAULT_MODELS).split(",") if m.strip()
] or [m.strip() for m in _DEFAULT_MODELS.split(",")]

RETRIES_PER_MODEL = max(1, _env_int("GEMINI_RETRIES_PER_MODEL", 2))
GEMINI_TIMEOUT = _env_float("GEMINI_TIMEOUT", 35.0)
MODEL_REFRESH_SECONDS = 6 * 3600

MSG_QUOTA = "⛩️ *Reimu ngáp dài buông chén trà.* Hòm công đức đông khách quá làm bùa chú nghẽn mạch rồi! Mau cúng tiền rồi lát nữa quay lại!"
MSG_SAFETY = "⛩️ *Reimu nhíu mày, vung gohei gạt phăng câu đó đi.* \"Câu này ta không thèm đáp! Đổi chủ đề đi, đồ rảnh rỗi!\""
MSG_EMPTY = "⛩️ *Reimu chớp mắt ngơ ngác.* \"Ta lỡ thần người mất rồi... Nói lại xem nào!\""

cooldown_tracker = {}          # (key_idx, model) -> thời điểm hết cooldown
_available_models = set()      # model thực sự có trên API (dò tự động)
_active_models = list(PREFERRED_MODELS)
_models_checked_at = 0.0
_models_lock = None
gemini_semaphore = None

SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold="BLOCK_ONLY_HIGH")
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]


def _get_sem():
    global gemini_semaphore
    if gemini_semaphore is None:
        gemini_semaphore = asyncio.Semaphore(3)
    return gemini_semaphore


async def _refresh_models():
    """Hỏi API xem model nào có thật & hỗ trợ generateContent, rồi lọc danh sách ưu tiên."""
    global _active_models, _models_checked_at, _available_models
    found = set()
    try:
        pager = await GEMINI_CLIENTS[0].aio.models.list()
        async for m in pager:
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                found.add((m.name or "").replace("models/", ""))
    except Exception as e:
        print(f"⚠️ [MODEL] Không dò được danh sách model: {str(e)[:150]}", flush=True)

    _models_checked_at = time.time()
    if not found:
        # Dò thất bại -> dùng nguyên danh sách ưu tiên, lỗi 404 sẽ tự cooldown
        _active_models = list(PREFERRED_MODELS)
        return

    _available_models = found
    chosen = [m for m in PREFERRED_MODELS if m in found]
    if not chosen:
        # Không model ưu tiên nào tồn tại -> lấy các model "flash" ổn định có sẵn
        bad = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "thinking-exp")
        chosen = sorted(
            m for m in found if "flash" in m and not any(b in m for b in bad)
        )[:6]
    _active_models = chosen or list(PREFERRED_MODELS)
    print(f"🧠 [MODEL] Danh sách đang dùng: {', '.join(_active_models)}", flush=True)


async def get_active_models():
    global _models_lock
    if _models_lock is None:
        _models_lock = asyncio.Lock()
    if time.time() - _models_checked_at > MODEL_REFRESH_SECONDS:
        async with _models_lock:
            if time.time() - _models_checked_at > MODEL_REFRESH_SECONDS:
                await _refresh_models()
    return list(_active_models)


def _classify_gemini_error(e: Exception) -> str:
    code = getattr(e, "code", None)
    text = str(e).upper()
    if code == 429 or "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "quota"
    if code == 404 or "404" in text or "NOT_FOUND" in text:
        return "missing"
    if code in (500, 502, 503, 504) or "503" in text or "UNAVAILABLE" in text or "DEADLINE" in text:
        return "retry"
    return "other"


def _extract_text(resp):
    """Trả về (text, blocked_by_safety)."""
    try:
        text = resp.text
    except Exception:
        text = None
    if text and text.strip():
        return text.strip(), False

    blocked = False
    pf = getattr(resp, "prompt_feedback", None)
    if pf is not None and getattr(pf, "block_reason", None):
        blocked = True
    for c in getattr(resp, "candidates", None) or []:
        fr = str(getattr(c, "finish_reason", "") or "").upper()
        if "SAFETY" in fr or "PROHIBITED" in fr or "BLOCKLIST" in fr or "SPII" in fr:
            blocked = True
    return None, blocked


async def ask_gemini(contents, system_instruction, temperature=0.85):
    """Trả về (text, ok). ok=False nghĩa là text là thông báo lỗi có sẵn (không lưu vào lịch sử)."""
    if not GEMINI_CLIENTS:
        print("❌ ask_gemini: chưa có GEMINI_API_KEY", flush=True)
        return MSG_QUOTA, False

    models = await get_active_models()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        safety_settings=SAFETY_SETTINGS,
    )

    async with _get_sem():
        for key_idx, client in enumerate(GEMINI_CLIENTS):
            for model_name in models:
                tracker_key = (key_idx, model_name)
                if cooldown_tracker.get(tracker_key, 0) > time.time():
                    continue

                for attempt in range(RETRIES_PER_MODEL):
                    try:
                        resp = await asyncio.wait_for(
                            client.aio.models.generate_content(
                                model=model_name, contents=contents, config=config
                            ),
                            timeout=GEMINI_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        cooldown_tracker[tracker_key] = time.time() + 30
                        print(f"⚠️ [TIMEOUT] Key #{key_idx+1} | {model_name}", flush=True)
                        break
                    except Exception as e:
                        kind = _classify_gemini_error(e)
                        err_str = str(e)

                        if kind == "quota":
                            if "PerDay" in err_str or "limit: 0" in err_str:
                                cooldown_tracker[tracker_key] = time.time() + 3600
                                print(f"⚠️ [HẾT QUOTA NGÀY] Key #{key_idx+1} | {model_name}", flush=True)
                            else:
                                cooldown_tracker[tracker_key] = time.time() + 20
                                print(f"⚠️ [CHẠM QUOTA PHÚT] Key #{key_idx+1} | {model_name}", flush=True)
                            break

                        if kind == "retry":
                            if attempt + 1 < RETRIES_PER_MODEL:
                                await asyncio.sleep(1.0 + random.random())
                                continue
                            cooldown_tracker[tracker_key] = time.time() + 20
                            print(f"⚠️ [503 QUÁ TẢI] {model_name}", flush=True)
                            break

                        if kind == "missing":
                            # Model không tồn tại -> bỏ qua 6 tiếng và ép dò lại danh sách
                            cooldown_tracker[tracker_key] = time.time() + 6 * 3600
                            global _models_checked_at
                            _models_checked_at = 0.0
                            print(f"⚠️ [MODEL 404] {model_name} không tồn tại, bỏ qua.", flush=True)
                            break

                        cooldown_tracker[tracker_key] = time.time() + 600
                        print(f"⚠️ [LỖI MODEL] {model_name}: {err_str[:150]}", flush=True)
                        break

                    text, blocked = _extract_text(resp)
                    if text:
                        return text, True
                    if blocked:
                        return MSG_SAFETY, False
                    print(f"⚠️ [RỖNG] {model_name} trả về rỗng, thử model khác.", flush=True)
                    break

    return MSG_QUOTA, False


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
discord_blocked_until = 0.0
USER_COOLDOWN_SECONDS = 3.0
user_last_request = {}

# Mỗi lần "đang nhập..." là 1 request tới Discord -> dễ gây 429 trên hosting dùng chung IP.
# Mặc định TẮT. Bật bằng biến môi trường TYPING_INDICATOR=1
TYPING_INDICATOR = _env_bool("TYPING_INDICATOR", False)
# Đồng bộ slash command mỗi lần khởi động. Sau khi đã sync xong 1 lần, có thể đặt SYNC_COMMANDS=0
SYNC_COMMANDS = _env_bool("SYNC_COMMANDS", True)


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
        if not TYPING_INDICATOR or is_discord_blocked():
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
INITIAL_LOGIN_BACKOFF = 300   # 5 phút
MAX_LOGIN_BACKOFF = 1800      # 30 phút
login_backoff = INITIAL_LOGIN_BACKOFF


class ReimuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="?reimu ", intents=intents, help_command=None)
        self._synced = False

    async def setup_hook(self):
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
                wiki_text, ok = await ask_gemini(prompt, REIMU_SYSTEM_PROMPT, 0.7)
                if not ok:
                    await interaction.followup.send(wiki_text)
                    return
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
                    await interaction.followup.send(MSG_QUOTA)
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

        if SYNC_COMMANDS and not self._synced and not is_discord_blocked():
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
        login_backoff = INITIAL_LOGIN_BACKOFF
        print("==================================================", flush=True)
        print(f"✅ [CHATBOT] Đã đăng nhập: {self.user.name} ({self.user.id})", flush=True)
        print(f"🔑 Số lượng API Key đang kích hoạt: {len(GEMINI_CLIENTS)}", flush=True)
        print("⛩️ Hakurei Reimu Master Roleplay Chatbot sẵn sàng phục vụ!", flush=True)
        print("==================================================", flush=True)
        # Dò model sớm để log ra danh sách đang dùng
        if GEMINI_CLIENTS:
            await get_active_models()

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

        if message.content.startswith("!") or message.content.startswith("/"):
            return

        # Đang bị Discord chặn -> bỏ qua (cũng tiết kiệm luôn quota Gemini)
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
                reply_text, ok = await ask_gemini(contents, REIMU_SYSTEM_PROMPT, temperature=0.85)
            except Exception as e:
                print(f"❌ [LỖI GEMINI CHAT]: {type(e).__name__}: {str(e)[:300]}", flush=True)
                reply_text, ok = MSG_QUOTA, False

        if ok:
            role_tag = "Bố Han Seiki" if is_father else f"Người dùng {user_display_name}"
            history.append({"role": "user", "text": f"[{role_tag}]: {clean_content}"})
            history.append({"role": "model", "text": reply_text})
            save_conversation_history(message.channel.id, message.author.id, history)

        await safe_reply(message, reply_text)


# ==============================================================================
# 7. KHỞI CHẠY BOT AN TOÀN (KHÔNG RESTART DỒN DẬP)
# ==============================================================================
async def main():
    global login_backoff

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
