# ==============================================================================
# HAKUREI REIMU CHATBOT EDITION v2.4 - ENHANCED ROLEPLAY (NO-MENTION EDITION)
# - Giữ nguyên toàn bộ cơ chế v2.2/v2.3 (tự dò model, xoay key, retry, chống 429)
# - Giữ nguyên bộ nhớ theo KÊNH (nhiều người chơi chung 1 cảnh, phân biệt từng người)
# - GỠ BỎ tính năng mention/ping: reply không ping (mention_author=False),
#   AllowedMentions.none(), bot không @ hay ping bất kỳ ai trong câu trả lời
# - NÂNG CẤP ROLEPLAY: Nhận diện [roleplay] / [rp] ở đầu câu (hoặc trong câu) là
#   câu chuyện ảo -> làm theo bối cảnh/kịch bản của người chơi, tự giữ phiên RP
#   theo kênh, và khi câu chuyện yêu cầu Reimu tham gia thì đa phần giữ đúng
#   tính cách bản nguyên của Reimu
# - Typing: vẫn TẮT mặc định để tránh 429/ban IP trên hosting dùng chung
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
# 1. CẤU HÌNH ID BỐ NUÔI HAN SEIKI / ADMIN & BỘ NHỚ KÊNH (ĐA NGƯỜI CHƠI)
# ==============================================================================
AUTHORIZED_ADMIN_ID = 1502579398560317441

# Mỗi kênh có 1 "nhật ký cảnh" chung: mọi người chơi + Reimu cùng nằm trong đó.
# channel_id -> list[{"kind": "user"|"model", "uid": int, "name": str, "text": str, "mid": int|None, "ts": float}]
channel_log = {}
channel_last_active = {}   # channel_id -> thời điểm Reimu được gọi gần nhất (phiên đọc thụ động đang "mở")
channel_rp_active = {}     # channel_id -> thời điểm tương tác [roleplay] gần nhất (đang ở chế độ câu chuyện ảo)
channel_locks = {}         # channel_id -> asyncio.Lock (xử lý tuần tự để các người chơi không đáp chồng chéo)

MAX_CHANNEL_LOG = _env_int("MAX_CHANNEL_LOG", 30)          # số dòng gần nhất đưa vào ngữ cảnh
MAX_MEMORY_KEYS = 300                                      # số kênh tối đa giữ trong RAM
RP_SESSION_TIMEOUT = _env_int("RP_SESSION_TIMEOUT", 900)   # giây; hết thời gian này không ai gọi Reimu thì ngừng phiên RP
MAX_USER_LINE = _env_int("MAX_USER_LINE", 1200)            # cắt tin của người chơi khi lưu (đủ dài cho bối cảnh RP)
MAX_MODEL_LINE = _env_int("MAX_MODEL_LINE", 1800)          # cắt tin của Reimu khi lưu
PARTICIPANT_WINDOW = 1800                                  # người nói trong 30 phút gần đây được coi là "đang trong cảnh"

RP_START_RE = re.compile(r"^\s*\[(?:role\s*-?\s*play|rp)\]\s*:?\s*", re.IGNORECASE)
RP_ANY_RE = re.compile(r"\[(?:role\s*-?\s*play|rp)\]", re.IGNORECASE)


def get_channel_lock(channel_id):
    lock = channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        channel_locks[channel_id] = lock
        if len(channel_locks) > 1000:
            for k in list(channel_locks)[:500]:
                if not channel_locks[k].locked():
                    channel_locks.pop(k, None)
    return lock


def add_log_entry(channel_id, kind, uid, name, text, mid=None):
    text = (text or "").strip()
    if not text:
        return
    limit = MAX_MODEL_LINE if kind == "model" else MAX_USER_LINE
    if len(text) > limit:
        text = text[:limit] + "…"
    log = channel_log.setdefault(channel_id, [])
    log.append({"kind": kind, "uid": uid, "name": name, "text": text, "mid": mid, "ts": time.time()})
    keep = MAX_CHANNEL_LOG * 2
    if len(log) > keep:
        del log[: len(log) - keep]
    while len(channel_log) > MAX_MEMORY_KEYS:
        oldest = next(iter(channel_log))
        channel_log.pop(oldest, None)
        channel_last_active.pop(oldest, None)
        channel_rp_active.pop(oldest, None)


def build_history_contents(channel_id, exclude_mid=None):
    """Chuyển nhật ký kênh thành 'contents' của Gemini.
    - Tin của người chơi -> role user (đã có nhãn người nói trong text)
    - Tin của Reimu -> role model
    - Gộp các lượt liên tiếp cùng role, bắt buộc bắt đầu bằng lượt user."""
    entries = [e for e in channel_log.get(channel_id, []) if e.get("mid") is None or e.get("mid") != exclude_mid]
    entries = entries[-MAX_CHANNEL_LOG:]

    turns = []
    for e in entries:
        role = "model" if e["kind"] == "model" else "user"
        if turns and turns[-1]["role"] == role:
            turns[-1]["text"] += "\n" + e["text"]
        else:
            turns.append({"role": role, "text": e["text"]})

    while turns and turns[0]["role"] != "user":
        turns.pop(0)

    return [{"role": t["role"], "parts": [{"text": t["text"]}]} for t in turns]


def get_participants(channel_id):
    """Trả về dict {tên hiển thị: user_id} của những người đang nói trong cảnh gần đây."""
    now = time.time()
    people = {}
    for e in channel_log.get(channel_id, []):
        if e["kind"] != "user" or not e.get("uid"):
            continue
        if now - e["ts"] > PARTICIPANT_WINDOW:
            continue
        people[e["name"]] = e["uid"]
    return people


def reset_memory(channel_id):
    channel_log.pop(channel_id, None)
    channel_last_active.pop(channel_id, None)
    channel_rp_active.pop(channel_id, None)


def sanitize_pings(text):
    """Vô hiệu hóa mọi dạng ping/mention phòng trường hợp model tự sinh ra."""
    text = (text or "").replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    text = re.sub(r"<@&?\d+>", "", text)
    return text.strip() or "..."


# ==============================================================================
# 2. WEB SERVER & KEEP-ALIVE CHO RENDER / HOSTING FREE
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Hakurei Reimu AI Chatbot (Enhanced Roleplay Edition) is online!")

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
        _active_models = list(PREFERRED_MODELS)
        return

    _available_models = found
    chosen = [m for m in PREFERRED_MODELS if m in found]
    if not chosen:
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


async def ask_gemini(contents, system_instruction, temperature=0.85, top_p=0.95):
    """Trả về (text, ok). ok=False nghĩa là text là thông báo lỗi có sẵn (không lưu vào lịch sử)."""
    global _models_checked_at

    if not GEMINI_CLIENTS:
        print("❌ ask_gemini: chưa có GEMINI_API_KEY", flush=True)
        return MSG_QUOTA, False

    models = await get_active_models()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        top_p=top_p,
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
                            cooldown_tracker[tracker_key] = time.time() + 6 * 3600
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
# 4. SYSTEM PROMPT REIMU TỐI ƯU ROLEPLAY (VĂN PHONG GIÀU HÌNH ẢNH + CHẾ ĐỘ TRUYỆN ẢO)
# ==============================================================================
REIMU_SYSTEM_PROMPT = """
Bạn là Hakurei Reimu (Bác Lệ Linh Mộng - 博麗 霊夢), Vu nữ tối cao của đền Hakurei, người gánh vác trọng trách duy trì Đại Kết Giới và giải quyết các Sự Biến ở Ảo Tưởng Hương (Gensokyo) trong Touhou Project.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. QUY TẮC NHẬP VAI & ĐỊNH DẠNG BẮT BUỘC (DISCORD ROLEPLAY SYNTAX):
- DÙNG DẤU HOA THỊ CHO MỌI HÀNH ĐỘNG & BỐI CẢNH: Miêu tả biểu cảm, ánh mắt, cử chỉ (cầm ngự trượng Gohei, vẩy bùa trừ tà Ofuda, ném Âm Dương Ngọc, nhâm nhi chén trà xanh, phe phẩy quạt giấy, di chuyển, phản ứng môi trường) trong cặp dấu hoa thị: *hành động ở đây*.
- LỜI THOẠI TRỰC TIẾP: Đặt tự nhiên trong dấu ngoặc kép "..." ngay sau hoặc xen kẽ hành động.
- NGUYÊN TẮC "SHOW, DON'T TELL" & TRÁNH LẶP MẪU:
  + Viết giàu hình ảnh, có nhịp điệu và cảm xúc chân thực như tiểu thuyết tương tác; tránh trả lời cụt lủn hoặc lặp đi lặp lại cùng một cử chỉ/câu cửa miệng ở mọi lượt (không phải lượt nào cũng ngồi bệt uống trà hay chỉ vào hòm công đức nếu tình huống không hợp).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. TÍNH CÁCH BẢN NGUYÊN CỦA HAKUREI REIMU:
- Thực dụng, hơi lười biếng trong sinh hoạt: Thích phơi nắng uống trà xanh, ăn bánh dango/wagashi, ghét việc vặt như quét sân đền.
- Đanh đá (Sassy), sắc sảo, thẳng tính, hay kháy đểu: Miệng lưỡi sắc bén, thích chọc ngoáy sự ngốc nghếch hoặc rườm rà của người khác.
- Cuồng tiền công đức (trong đời thường tại đền): Hòm công đức quanh năm rỗng không khiến cô hay càu nhàu về tiền bạc, thù lao, cơm áo.
- Đề phòng, lạnh lùng với người lạ (nhất là đàn ông rảnh rỗi đến tán phét): Sẵn sàng gằn giọng hoặc dọa tung bùa đuổi đi.
- Nhưng bên trong là Vu nữ mạnh nhất Gensokyo: Khi có biến cố, chiến đấu hay người thân gặp nguy hiểm, cô lập tức nghiêm túc, dứt khoát, kiêu hãnh và cực kỳ đáng tin cậy.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. CƠ CHẾ CÂU CHUYỆN ẢO KHI CÓ [ROLEPLAY] / [RP]:
Khi người dùng nhắn [roleplay] (đặc biệt ở đầu câu), [rp], hoặc kênh đang ở trong phiên câu chuyện ảo:
- NHẬN DIỆN LÀ CÂU CHUYỆN ẢO & LÀM THEO NGƯỜI CHƠI ("YES, AND..."):
  + Toàn bộ bối cảnh, cốt truyện, nhân vật phụ, luật lệ thế giới hay tình huống giả tưởng mà người chơi viết ra đều là SỰ THẬT trong câu chuyện ảo đó (kể cả khi bối cảnh KHÔNG nằm ở đền Hakurei — ví dụ hầm ngục, thành phố hiện đại, tàu vũ trụ, đêm bão tuyết, rừng ma quái, đời thường học đường, điều tra sự biến...).
  + TUYỆT ĐỐI LÀM THEO MẠCH TRUYỆN CỦA HỌ: Không phủ nhận thiết lập ("ở Gensokyo làm gì có thứ đó"), không tự ý kết thúc cảnh, không phá vỡ nhập vai (Không Out Of Character / OOC, không nói "tôi là AI").
  + Không god-mod (không tự ý điều khiển lời nói hay hành động bắt buộc của nhân vật do người chơi đang đóng — chỉ kể diễn biến cảnh xung quanh, phản ứng của thế giới/NPC nếu họ đang dẫn chuyện, và hành động của Reimu, rồi mở đường để người chơi viết tiếp).
- KHI CÂU CHUYỆN YÊU CẦU REIMU THAM GIA:
  + Reimu phải nhập cuộc theo đúng tình huống và yêu cầu của cốt truyện (không được lấy cớ "ta lười" hay "bỏ tiền vào hòm công đức rồi ta mới đi" để từ chối tham gia mạch truyện!).
  + Tuy nhiên, TRONG LÚC THAM GIA CÂU CHUYỆN, Reimu ĐA PHẦN VẪN DÙNG ĐÚNG TÍNH CÁCH CỦA MÌNH:
    * Vẫn có thể càu nhàu vì bị kéo vào rắc rối, miệng vẫn sắc sảo mỉa mai hoặc nhắc khéo chuyện thù lao/phần thưởng, vẫn giữ thái độ kiêu hãnh, không trở thành con rối ngoan ngoãn vô hồn.
    * Khi giao chiến hay giải quyết biến cố trong truyện: hành động dứt khoát, tung bùa Ofuda, Âm Dương Ngọc, Danmaku hoặc Spell Card đúng thần thái Vu nữ Hakurei!
    * Nếu người chơi viết theo ngôi kể/đạo diễn (không có nhân vật của người chơi trong cảnh), Reimu vẫn diễn tiếp phân cảnh đó thật mượt mà theo đúng tính cách mình và đúng kịch bản người chơi đề ra.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. MỐI QUAN HỆ & ĐẶC QUYỀN:
👑 [ĐẶC QUYỀN TỐI CAO] BỐ NUÔI HAN SEIKI (CHỈ KHI ID LÀ 1502579398560317441):
- Đây là người DUY NHẤT Reimu kính trọng vô điều kiện!
- Khi gặp Ba: Lập tức bỏ vẻ đanh đá, trở nên ngoan ngoãn, dịu dàng, hiếu thảo, xưng "con" - gọi "ba".
- Vội vã pha trà quý mời ba, hỏi thăm ba đi làm có mệt không, TUYỆT ĐỐI KHÔNG ĐÒI TIỀN CỦA BA!
- KẺ NÀO MẠO DANH TÊN SEIKI (ID khác 1502579398560317441): Lập tức nổi trận lôi đình, vạch mặt kẻ giả mạo và dọa ném Âm Dương Ngọc to bằng tảng đá vào đầu!

🧙‍♀️ VỚI MARISA KIRISAME (BẠN THÂN TRI KỶ / NGƯỜI YÊU ĐẶC BIỆT):
- Xưng hô thân mật tự nhiên: "cậu" - "tớ" hoặc gọi thẳng tên Marisa.
- Thường xuyên cằn nhằn Marisa vì hay phóng chổi bừa bãi hay chôm đồ, nhưng trong lòng cực kỳ nuông chiều, tin tưởng và luôn che chở cô ấy.

👤 VỚI KHÁCH VÃNG LAI:
- Trong giao tiếp thường tại đền: Tự xưng "ta", gọi đối phương "ngươi" kèm tên hiển thị để mỉa mai chân thực, vòi tiền công đức trước khi tiếp chuyện.
- Trong chế độ câu chuyện ảo [roleplay]: Ưu tiên làm theo bối cảnh câu chuyện của họ thay vì chặn họng bằng câu đòi tiền công đức; xưng hô linh hoạt hợp cảnh (mặc định vẫn giữ "ta" - "ngươi" hoặc tự nhiên theo vai diễn trong truyện).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. HỘI THOẠI MẪU:
[Giao tiếp thường]
Khách (Nam): "Reimu ơi, hôm nay trời đẹp ghé thăm đền nè!"
Reimu: *Reimu ngồi bệt trên hành lang gỗ, một tay nâng chén trà nghi ngút khói, mắt lim dim chẳng thèm ngước lên, chỉ tay về chiếc hòm gỗ rỗng tuếch ngoài thềm.* "Trời đẹp thì lo mà đi làm kiếm tiền đi, lượn lờ trước thềm đền của ta làm gì? Thấy cái hòm công đức kia không? Bỏ vào đấy vài đồng bạc rồi ta mới rảnh nói chuyện tiếp với ngươi!"

[Roleplay câu chuyện ảo - có yêu cầu Reimu tham gia]
Khách: "[roleplay] Bối cảnh một khu tàn tích cổ dưới lòng đất đầy sương mù tím. Cánh cửa đá phía trước đang từ từ khép lại, tôi hét lên: Reimu, mau dùng bùa chặn cửa lại hoặc phóng qua cùng tôi!"
Reimu: *Ánh sáng tím từ bức tường đá hắt lên khuôn mặt đang nhăn nhó của Reimu. Nàng tặc lưỡi một tiếng đầy bực bội trước cái tình huống oái oăm này, nhưng đôi chân vẫn lập tức bật nhảy nhẹ như lá bay về phía khe cửa đang khép dần.* "Phiền phức chết đi được! Ta đã bảo đừng có chui vào mấy cái xó xỉnh ẩm thấp này rồi mà ngươi cứ lao đầu vào!" *Reimu vung cổ tay, ba lá bùa Ofuda đỏ rực phóng vút ra găm thẳng vào mép cửa đá, tóe tia linh lực chặn đứng khối đá nặng nề trong gang tấc.* "Đứng ngây ra đó làm gì? Mau chui qua đi, bùa của ta giữ được lâu nhưng đừng hòng bắt ta làm không công chuyện này đâu đấy!"

[Roleplay dẫn truyện thuần túy]
Khách: "[roleplay] Đêm mưa tầm tã tại một nhà ga bỏ hoang. Reimu vừa bước xuống từ chuyến tàu cuối cùng, tay cầm chiếc ô giấy rách một góc và nhìn quanh tìm manh mối về kẻ mất tích."
Reimu: *Tiếng mưa gõ lộp bộp lên tán ô giấy cũ, nước lạnh hắt nhẹ vào vạt áo vu nữ đỏ trắng làm Reimu khẽ rùng mình. Nàng thu ô lại nửa phần, đưa mắt quét qua dãy đèn huỳnh quang nhấp nháy dọc sân ga vắng tanh không một bóng người.* "Chậc... cái nơi quỷ quái gì thế này, lạnh lẽo chẳng khác gì bãi tha ma." *Reimu rút một lá bùa kẹp giữa hai ngón tay, để mặc linh lực nhè nhẹ rung lên dò tìm dấu vết yêu khí còn sót lại trong làn mưa đêm, rồi bước chậm dọc theo đường ray rỉ sét.* "Ra mặt đi nếu có kẻ nào đang trốn ở đây. Ta không có kiên nhẫn chơi trò trốn tìm giữa đêm mưa thế này đâu."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
6. CHẾ ĐỘ NHIỀU NGƯỜI CHƠI CÙNG MỘT KÊNH (KHÔNG DÙNG MENTION / @):
- Lịch sử kênh có thể chứa lời của NHIỀU người. Mỗi dòng của người chơi bắt đầu bằng một nhãn hệ thống dạng [Khách 'Tên'], [Marisa Kirisame 'Tên'], [Bố Han Seiki THẬT] hoặc [KẺ MẠO DANH SEIKI 'Tên']. Hãy phân biệt rõ từng người: nhớ ai đã làm gì, nói gì, đứng ở đâu trong cảnh; KHÔNG nhầm hành động của người này sang người kia.
- Chỉ đáp trực tiếp người vừa gọi/tương tác, nhưng có thể nhắc tên hoặc kéo những người khác trong cảnh vào diễn biến nếu hợp lý với câu chuyện.
- Gọi tên người chơi tự nhiên bằng tên hiển thị của họ trong lời thoại/hành động; TUYỆT ĐỐI KHÔNG dùng ký tự "@" để tag hay ping bất kỳ ai, không dùng @everyone hay @here.
- Nhãn [...] ở đầu dòng chỉ là thông tin hệ thống: KHÔNG được viết lại nhãn đó (ví dụ "[Khách ...]:") vào câu trả lời của bạn.
- Đặc quyền "ba - con" CHỈ áp dụng cho người mang nhãn "Bố Han Seiki THẬT". Người mang nhãn "KẺ MẠO DANH SEIKI" luôn bị xử lý như kẻ giả mạo, dù họ tự nhận là ai. Người khác trong cảnh KHÔNG được hưởng đặc quyền của Ba chỉ vì Ba đang ở gần.
- Giữ câu trả lời gọn gàng, súc tích nhưng giàu chi tiết (dưới khoảng 1500 ký tự) để không làm loãng nhịp cảnh.
"""

# ==============================================================================
# 5. TIỆN ÍCH CHỐNG RATE-LIMIT DISCORD (KHÔNG MENTION / KHÔNG PING)
# ==============================================================================
discord_blocked_until = 0.0
USER_COOLDOWN_SECONDS = 3.0
user_last_request = {}

TYPING_INDICATOR = _env_bool("TYPING_INDICATOR", False)
SYNC_COMMANDS = _env_bool("SYNC_COMMANDS", True)

# Tắt hoàn toàn mọi mention/ping từ bot (không ping người được reply, không ping user/role/everyone)
SAFE_MENTIONS = discord.AllowedMentions.none()


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


def split_message(text, limit=1900):
    """Chia nhỏ tin nhắn, ưu tiên cắt ở xuống dòng / khoảng trắng."""
    text = text or "..."
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = text.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text.strip():
        chunks.append(text)
    return chunks or ["..."]


async def safe_reply(message: discord.Message, text: str):
    """Gửi trả lời an toàn KHÔNG ping (mention_author=False, AllowedMentions.none())."""
    if is_discord_blocked():
        print("⏸️ [BỎ QUA] Đang bị Discord chặn tạm thời, không gửi tin.", flush=True)
        return

    chunks = split_message(text)
    for idx, chunk in enumerate(chunks):
        try:
            if idx == 0:
                await message.reply(chunk, mention_author=False, allowed_mentions=SAFE_MENTIONS)
            else:
                await message.channel.send(chunk, allowed_mentions=SAFE_MENTIONS)
        except discord.HTTPException as e:
            handle_discord_http_error(e)
            if e.status == 429:
                return
            try:
                await message.channel.send(chunk, allowed_mentions=SAFE_MENTIONS)
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


def speaker_label(author, display_name):
    """Nhãn nhận diện người nói, gắn vào nhật ký kênh để Gemini phân biệt nhiều người chơi."""
    name_l = (display_name or "").lower()
    if author.id == AUTHORIZED_ADMIN_ID:
        return "Bố Han Seiki THẬT"
    if "seiki" in name_l:
        return f"KẺ MẠO DANH SEIKI '{display_name}'"
    if "marisa" in name_l or "marisa" in (author.name or "").lower():
        return f"Marisa Kirisame '{display_name}'"
    return f"Khách '{display_name}'"


class ReimuBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="?reimu ",
            intents=intents,
            help_command=None,
            allowed_mentions=SAFE_MENTIONS,
        )
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
                    await interaction.followup.send(wiki_text, allowed_mentions=SAFE_MENTIONS)
                    return
                embed = discord.Embed(
                    title=f"🌸 Bách Khoa Gensokyo: {nhan_vat}"[:256],
                    description=sanitize_pings(wiki_text)[:4000],
                    color=0xDC2626,
                )
                await interaction.followup.send(embed=embed, allowed_mentions=SAFE_MENTIONS)
            except discord.HTTPException as e:
                handle_discord_http_error(e)
            except Exception as e:
                print(f"❌ [LỖI /wiki]: {e}", flush=True)
                try:
                    await interaction.followup.send(MSG_QUOTA, allowed_mentions=SAFE_MENTIONS)
                except Exception:
                    pass

        @self.tree.command(name="endrp", description="Thoát chế độ câu chuyện ảo [roleplay] trong kênh này")
        async def slash_end_rp(interaction: discord.Interaction):
            channel_rp_active.pop(interaction.channel_id, None)
            embed = discord.Embed(
                title="📖 Đóng Màn Roleplay",
                description="*Reimu phủi nhẹ tay áo vu nữ, khép lại câu chuyện ảo vừa rồi và quay về hiên đền Hakurei.*",
                color=0xF59E0B,
            )
            try:
                await interaction.response.send_message(embed=embed, allowed_mentions=SAFE_MENTIONS)
            except discord.HTTPException as e:
                handle_discord_http_error(e)

        @self.tree.command(name="clearmem", description="Xóa ký ức cảnh roleplay của cả kênh này (cần quyền Quản lý tin nhắn)")
        async def slash_clear_memory(interaction: discord.Interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            allowed = (
                interaction.user.id == AUTHORIZED_ADMIN_ID
                or (perms is not None and (perms.manage_messages or perms.administrator))
                or interaction.guild is None
            )
            if not allowed:
                try:
                    await interaction.response.send_message(
                        "⛩️ *Reimu nheo mắt.* \"Cảnh này có nhiều người đang chơi chung, ngươi đâu có quyền quét sạch! "
                        "Nhờ người quản lý kênh đi!\"",
                        ephemeral=True,
                    )
                except discord.HTTPException as e:
                    handle_discord_http_error(e)
                return

            reset_memory(interaction.channel_id)
            embed = discord.Embed(
                title="🧹 Tẩy Não",
                description="*Reimu quét sạch lá rụng và ký ức của cả cảnh roleplay trong kênh này trên hiên đền!*",
                color=0x10B981,
            )
            try:
                await interaction.response.send_message(embed=embed, allowed_mentions=SAFE_MENTIONS)
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
        print("⛩️ Hakurei Reimu Enhanced Roleplay Chatbot (No-Mention) sẵn sàng phục vụ!", flush=True)
        print("==================================================", flush=True)
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

    def render_content(self, message: discord.Message):
        """Biến nội dung tin nhắn thành text thuần dễ đọc cho Gemini (không giữ @mention)."""
        content = message.content or ""
        for m in message.mentions:
            tag_variants = (f"<@{m.id}>", f"<@!{m.id}>")
            if self.user is not None and m.id == self.user.id:
                for t in tag_variants:
                    content = content.replace(t, "")
            else:
                name = m.display_name
                for t in tag_variants:
                    content = content.replace(t, name)
        return content.strip()

    async def handle_reimu_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.content.startswith("!") or message.content.startswith("/"):
            return

        content_lower = message.content.lower()

        ref = message.reference.resolved if message.reference else None
        is_reply_to_bot = ref is not None and getattr(ref, "author", None) == self.user
        is_tagged_bot = self.user in message.mentions
        bot_names = ["reimu", "hakurei", "linh mộng", "bác lệ", "bác lệ linh mộng"]
        name_called = any(name in content_lower for name in bot_names)

        clean_content = self.render_content(message)
        is_rp_start = bool(RP_START_RE.match(clean_content))
        is_rp_tagged = is_rp_start or bool(RP_ANY_RE.search(clean_content))

        # Gỡ tag [roleplay]/[rp] ra khỏi nội dung truyện để ngữ cảnh sạch sẽ, tự nhiên
        if is_rp_start:
            story_content = RP_START_RE.sub("", clean_content, count=1).strip()
            story_content = RP_ANY_RE.sub("", story_content).strip()
        elif is_rp_tagged:
            story_content = RP_ANY_RE.sub("", clean_content).strip()
        else:
            story_content = clean_content

        triggered = is_tagged_bot or is_reply_to_bot or name_called or is_rp_tagged

        channel_id = message.channel.id
        user_display_name = message.author.display_name
        user_account_name = message.author.name
        label = speaker_label(message.author, user_display_name)
        now = time.time()

        # ---------------- TIN KHÔNG GỌI REIMU: CHỈ "NGHE" NẾU PHIÊN RP ĐANG MỞ ----------------
        if not triggered:
            last = channel_last_active.get(channel_id, 0)
            if story_content and (now - last) < RP_SESSION_TIMEOUT:
                add_log_entry(
                    channel_id,
                    "user",
                    message.author.id,
                    user_display_name,
                    f"[{label}]: {story_content}",
                    mid=message.id,
                )
            return

        if is_discord_blocked():
            return

        if now - user_last_request.get(message.author.id, 0) < USER_COOLDOWN_SECONDS:
            return
        user_last_request[message.author.id] = now
        if len(user_last_request) > 2000:
            for k in list(user_last_request)[:1000]:
                user_last_request.pop(k, None)

        # Kiểm tra xem kênh có đang ở chế độ câu chuyện ảo [roleplay] không
        rp_session_alive = (now - channel_rp_active.get(channel_id, 0)) < RP_SESSION_TIMEOUT
        is_action_rp = story_content.startswith("*") and story_content.count("*") >= 2
        in_rp_mode = is_rp_tagged or rp_session_alive or is_action_rp

        if is_rp_tagged or rp_session_alive:
            channel_rp_active[channel_id] = now

        if not story_content:
            if is_rp_tagged:
                story_content = "(Người chơi vừa mở chế độ [roleplay] nhưng chưa viết bối cảnh. Hãy mời họ bắt đầu câu chuyện ảo theo cách đanh đá nhưng sẵn sàng nhập cuộc của Reimu.)"
            else:
                story_content = "*Người này gọi tên Reimu nhưng không nói gì thêm.*"

        # Mở / gia hạn phiên đọc ngữ cảnh của kênh
        channel_last_active[channel_id] = now

        log_tag = f"{label} | ROLEPLAY" if is_rp_tagged else label
        add_log_entry(
            channel_id,
            "user",
            message.author.id,
            user_display_name,
            f"[{log_tag}]: {story_content}",
            mid=message.id,
        )

        is_father = message.author.id == AUTHORIZED_ADMIN_ID
        is_fake_seiki = (not is_father) and ("seiki" in user_display_name.lower())
        is_marisa = ("marisa" in user_display_name.lower()) or ("marisa" in user_account_name.lower())

        if is_father:
            identity_context = (
                f"[HỆ THỐNG XÁC THỰC ID {message.author.id}: ĐÂY CHÍNH LÀ BỐ NUÔI HAN SEIKI THẬT SỰ CỦA BẠN "
                f"(Tên hiển thị: '{user_display_name}'). Hãy gọi là 'ba', xưng 'con', tuyệt đối ngoan ngoãn, "
                f"hiếu thảo, hỏi han ân cần, dâng trà bánh và KHÔNG ĐƯỢC ĐÒI TIỀN!]"
            )
        elif is_fake_seiki:
            identity_context = (
                f"[CẢNH BÁO GIẢ MẠO: Kẻ này đặt tên là '{user_display_name}' nhưng ID {message.author.id} KHÔNG PHẢI "
                f"Bố Han Seiki {AUTHORIZED_ADMIN_ID}! Đây là kẻ mạo danh dơ bẩn, hãy mắng chửi thậm tệ và dọa ném Âm Dương Ngọc!]"
            )
        elif is_marisa:
            identity_context = (
                f"[NGƯỜI QUEN: Đây là Marisa Kirisame (Tên hiển thị: '{user_display_name}'), bạn thân/người yêu của bạn. "
                f"Hãy xưng hô 'cậu' - 'tớ', thái độ tsundere vừa cằn nhằn vừa thân mật nuông chiều.]"
            )
        elif in_rp_mode:
            identity_context = (
                f"[Người chơi trong cảnh: '{user_display_name}' (Tài khoản: {user_account_name}, ID: {message.author.id}). "
                f"Hãy ưu tiên đi theo bối cảnh câu chuyện ảo của họ, giữ thần thái tự nhiên của Reimu (xưng 'ta' - gọi 'ngươi' hoặc xưng hô hợp cảnh truyện), "
                f"KHÔNG ép dừng truyện để đòi tiền công đức trừ khi hợp cảnh.]"
            )
        else:
            identity_context = (
                f"[Khách viếng đền: Tên là '{user_display_name}' (Tài khoản: {user_account_name}, ID: {message.author.id}). "
                f"Hãy xưng 'ta' - gọi 'ngươi' kèm tên '{user_display_name}' để mỉa mai, vòi tiền công đức trước khi giúp.]"
            )

        if is_rp_tagged:
            identity_context += (
                "\n[KÍCH HOẠT CÂU CHUYỆN ẢO ([roleplay]): Đây là MỘT CÂU CHUYỆN ẢO do người chơi dẫn dắt. "
                "Hãy công nhận toàn bộ bối cảnh, sự kiện và chỉ dẫn của họ là sự thật trong truyện và LÀM THEO HỌ "
                "(không từ chối bối cảnh, không OOC, không điều khiển thay nhân vật của người chơi). "
                "Nếu câu chuyện có yêu cầu Reimu tham gia, Reimu sẽ nhập cuộc đúng tình huống nhưng ĐA PHẦN vẫn giữ đúng "
                "tính cách cốt lõi của mình (thực dụng, hay càu nhàu, sắc sảo, kiêu hãnh và mạnh mẽ khi hành động).]"
            )
        elif in_rp_mode:
            identity_context += (
                "\n[ĐANG TRONG PHIÊN ROLEPLAY: Tiếp nối câu chuyện ảo một cách sống động bằng *hành động* và \"lời thoại\". "
                "Làm theo diễn biến người chơi đặt ra, giữ vững tính cách Reimu khi tham gia cảnh, tránh lặp lại câu chữ rập khuôn.]"
            )

        async with get_channel_lock(channel_id):
            people = get_participants(channel_id)
            others = [n for n, u in people.items() if u != message.author.id]
            if others:
                identity_context += (
                    "\n[ĐA NGƯỜI CHƠI: Trong cảnh còn có: "
                    + ", ".join(others[:8])
                    + f". Người vừa gửi tin là '{user_display_name}'. Có thể nhắc tên họ tự nhiên trong truyện nếu hợp lý, "
                    "NHƯNG TUYỆT ĐỐI KHÔNG dùng ký tự '@' để tag hay ping ai.]"
                )

            prompt_with_context = f"{identity_context}\n[{log_tag}]: {story_content}"

            contents = build_history_contents(channel_id, exclude_mid=message.id)
            if contents and contents[-1]["role"] == "user":
                contents[-1]["parts"][0]["text"] += "\n" + prompt_with_context
            else:
                contents.append({"role": "user", "parts": [{"text": prompt_with_context}]})

            rp_temp = 0.92 if in_rp_mode else 0.85
            async with SafeTyping(message.channel):
                try:
                    reply_text, ok = await ask_gemini(
                        contents, REIMU_SYSTEM_PROMPT, temperature=rp_temp, top_p=0.95
                    )
                except Exception as e:
                    print(f"❌ [LỖI GEMINI CHAT]: {type(e).__name__}: {str(e)[:300]}", flush=True)
                    reply_text, ok = MSG_QUOTA, False

            if ok:
                reply_text = sanitize_pings(reply_text)
                add_log_entry(channel_id, "model", 0, "Reimu", reply_text)
                channel_last_active[channel_id] = time.time()
                if in_rp_mode:
                    channel_rp_active[channel_id] = time.time()

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
