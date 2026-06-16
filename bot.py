"""Telegram-бот: редизайн интерьера по фото через Polza.ai API."""

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from PIL import Image

from subscriptions import SubscriptionStore, YooKassaBilling

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
POLZA_API_KEY = os.getenv("POLZA_API_KEY", "")
POLZA_MODEL = os.getenv("POLZA_MODEL", "black-forest-labs/flux.2-pro")
POLZA_API_BASE = os.getenv("POLZA_API_BASE", "https://polza.ai/api")
POLZA_STRENGTH = float(os.getenv("POLZA_STRENGTH", "0.72"))

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
SUBSCRIPTION_PRICE_RUB = float(os.getenv("SUBSCRIPTION_PRICE_RUB", "10"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
SUBSCRIPTION_GENERATIONS = int(os.getenv("SUBSCRIPTION_GENERATIONS", "20"))
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8765"))
SUBSCRIPTION_ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("SUBSCRIPTION_ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
}
SUBSCRIPTION_ADMIN_USERNAMES = {
    part.strip().lower().lstrip("@")
    for part in os.getenv("SUBSCRIPTION_ADMIN_USERNAMES", "leeooo19").split(",")
    if part.strip()
}
SUBSCRIPTION_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

if not POLZA_API_KEY:
    raise RuntimeError("Укажите POLZA_API_KEY в .env")

ROOMS = {
    "living": {
        "label": "гостиная",
        "en": "living room",
        "furniture": "sofa, armchairs, coffee table, TV area, side tables",
    },
    "bedroom": {
        "label": "спальня",
        "en": "bedroom",
        "furniture": (
            "replace sofas and living-room furniture with a large double bed as the main focal point, "
            "nightstands on both sides, wardrobe or dresser, soft bedding and pillows; "
            "the bed must be clearly visible"
        ),
    },
    "kitchen": {
        "label": "кухня",
        "en": "kitchen",
        "furniture": "kitchen cabinets, countertops, sink, stove, refrigerator, dining area if space allows",
    },
    "bathroom": {
        "label": "ванная",
        "en": "bathroom",
        "furniture": "vanity with sink, toilet, shower or bathtub, bathroom tiles, mirror",
    },
    "office": {
        "label": "кабинет",
        "en": "home office",
        "furniture": "desk, office chair, bookshelf, task lighting; remove bed and sofa",
    },
    "kids": {
        "label": "детская",
        "en": "kids bedroom",
        "furniture": "children's bed, toy storage, playful decor, study corner; remove adult sofa",
    },
}

STYLES = {
    "simple": {
        "label": "простой / бюджетный",
        "en": "simple practical budget-friendly everyday home",
        "hint": "Обычная квартира без дизайнерского ремонта — практично и недорого",
    },
    "modern": {"label": "современный минимализм", "en": "modern minimalist"},
    "scandi": {"label": "скандинавский", "en": "Scandinavian"},
    "loft": {"label": "лофт", "en": "industrial loft"},
    "classic": {"label": "классический", "en": "classic elegant"},
    "boho": {"label": "бохо", "en": "bohemian"},
    "japandi": {"label": "джапанди", "en": "Japandi"},
}

MAX_FURNITURE_REFS = 3

MOODS = {
    "light": {
        "label": "светлые тона, много естественного света",
        "en": "light bright palette, abundant natural light",
    },
    "dark": {
        "label": "тёмные акценты, уютная атмосфера",
        "en": "dark accents, cozy intimate atmosphere",
    },
    "warm": {
        "label": "тёплые бежевые и деревянные оттенки",
        "en": "warm beige and wood tones",
    },
    "cool": {
        "label": "холодные серые и синие оттенки",
        "en": "cool gray and blue tones",
    },
    "colorful": {
        "label": "яркие акцентные цвета",
        "en": "bold colorful accent colors",
    },
}


class DesignStates(StatesGroup):
    photo = State()
    room = State()
    style = State()
    mood = State()
    furniture = State()
    extra = State()
    revision = State()


def _kb(options: dict, prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=info["label"], callback_data=f"{prefix}:{key}")]
        for key, info in options.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_prompt(
    room_key: str,
    style_key: str,
    mood_key: str,
    extra: str = "",
    furniture_count: int = 0,
) -> str:
    room = ROOMS[room_key]
    style = STYLES[style_key]
    mood = MOODS[mood_key]

    if style_key == "simple":
        style_line = (
            f"Redesign this interior photo as a practical, budget-friendly {room['en']}. "
            "Simple everyday home, affordable mass-market furniture, clean and cozy, "
            "not luxury or designer showcase"
        )
    else:
        style_line = f"Redesign this interior photo as a {style['en']} {room['en']}"

    parts = [
        style_line,
        f"Change the furniture to suit a {room['en']}: {room['furniture']}",
        mood["en"],
        "Keep the same camera angle, walls, windows, doors and room proportions",
        "Remove furniture that does not belong in this room type",
        "photorealistic interior photography, high quality",
    ]
    if furniture_count:
        parts.append("Image 1 is the room to redesign")
        for i in range(furniture_count):
            parts.append(
                f"Image {i + 2} shows furniture the owner wants in the room — "
                "incorporate a similar item matching its shape, color and style"
            )
    if extra:
        parts.append(extra)
    return ". ".join(parts)


def build_revision_prompt(
    room_key: str,
    style_key: str,
    mood_key: str,
    feedback: str,
    furniture_count: int = 0,
) -> str:
    room = ROOMS[room_key]
    style = STYLES[style_key]
    mood = MOODS[mood_key]
    if style_key == "simple":
        style_line = f"This is a practical budget-friendly {room['en']} interior"
    else:
        style_line = f"This is a {style['en']} {room['en']} interior design"
    parts = [
        style_line,
        f"The room must include appropriate furniture: {room['furniture']}",
        mood["en"],
        f"Apply these changes: {feedback}",
        "Keep the same camera angle, walls, windows, doors and room proportions",
        "photorealistic interior photography, high quality",
    ]
    if furniture_count:
        parts.append("Image 1 is the room. Additional images are furniture references to keep in mind")
    return ". ".join(parts)


def _furniture_kb(count: int) -> InlineKeyboardMarkup:
    rows = []
    if count > 0:
        rows.append([InlineKeyboardButton(text="✔️ Готово → дальше", callback_data="furniture:done")])
    rows.append([InlineKeyboardButton(text="Пропустить →", callback_data="furniture:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Внести правки", callback_data="action:edit")],
            [InlineKeyboardButton(text="🔄 Новый дизайн", callback_data="action:new")],
        ]
    )


def _prepare_photo(image_bytes: bytes, max_side: int = 1024) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def download_telegram_photo(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download_file(file.file_path, buffer)
    return buffer.getvalue()


def _polza_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{POLZA_API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {POLZA_API_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode() if exc.fp else str(exc)
        raise RuntimeError(f"Polza HTTP {exc.code}: {detail}") from exc


def _download_polza_image(result: dict) -> bytes:
    data = result.get("data")
    if not data:
        raise RuntimeError("Polza не вернула изображение")
    item = data[0] if isinstance(data, list) else data
    image_url = item.get("url") if isinstance(item, dict) else None
    if not image_url:
        raise RuntimeError("В ответе Polza нет URL изображения")
    with urllib.request.urlopen(image_url, timeout=120) as resp:
        return resp.read()


def _generate_via_polza(
    prompt: str,
    photo_bytes_list: list[bytes],
    strength: float,
    guidance_scale: float = 3.5,
) -> bytes:
    images = []
    for idx, photo_bytes in enumerate(photo_bytes_list):
        max_side = 1024 if idx == 0 else 768
        prepared = _prepare_photo(photo_bytes, max_side=max_side)
        encoded = base64.b64encode(prepared).decode("ascii")
        images.append({"type": "base64", "data": f"data:image/jpeg;base64,{encoded}"})
    payload = {
        "model": POLZA_MODEL,
        "input": {
            "prompt": prompt,
            "images": images,
            "guidance_scale": guidance_scale,
            "strength": strength,
            "image_resolution": "1K",
        },
    }
    result = _polza_request("POST", "/v1/media", payload)
    if result.get("status") == "completed":
        usage = result.get("usage", {})
        logger.info("Generated via polza, cost_rub=%s", usage.get("cost_rub"))
        return _download_polza_image(result)

    gen_id = result["id"]
    for _ in range(90):
        time.sleep(2)
        status = _polza_request("GET", f"/v1/media/{gen_id}")
        state = status.get("status")
        if state == "completed":
            usage = status.get("usage", {})
            logger.info("Generated via polza, cost_rub=%s", usage.get("cost_rub"))
            return _download_polza_image(status)
        if state == "failed":
            err = status.get("error") or {}
            raise RuntimeError(err.get("message") or "Генерация на Polza не удалась")
    raise RuntimeError("Таймаут ожидания генерации на Polza.ai")


def _is_retryable_error(exc: Exception) -> bool:
    err = str(exc).lower()
    return "503" in err or "loading" in err or "rate" in err or "429" in err or "timeout" in err


def _is_billing_error(exc: Exception) -> bool:
    err = str(exc).lower()
    return (
        "402" in err
        or "payment" in err
        or "balance" in err
        or "insufficient" in err
        or "billing" in err
        or "недостаточно" in err
        or "баланс" in err
    )


def _is_auth_error(exc: Exception) -> bool:
    err = str(exc).lower()
    return "403" in err or "forbidden" in err or "unauthorized" in err or "401" in err


def format_generation_error(exc: Exception) -> str:
    err = str(exc)
    low = err.lower()
    if _is_billing_error(exc):
        return (
            "Недостаточно средств на Polza.ai.\n\n"
            "Пополните баланс: https://polza.ai\n"
            "Ориентир: ~5 ₽ за одну картинку.\n"
            "После пополнения снова нажмите /design."
        )
    if _is_auth_error(exc):
        return (
            "Ключ Polza не подходит или отозван.\n\n"
            "Создайте новый в личном кабинете Polza.ai\n"
            "и обновите POLZA_API_KEY в .env."
        )
    if len(err) > 400:
        err = err[:400] + "…"
    return f"Ошибка генерации: {err}\n\nПопробуйте ещё раз или /design."


def _is_video_message(message: Message) -> bool:
    if message.video:
        return True
    if (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("video/")
    ):
        return True
    return False


def _extract_media_file_id(message: Message) -> Optional[str]:
    if message.photo:
        return message.photo[-1].file_id
    if message.video:
        return message.video.file_id
    if message.document and message.document.mime_type:
        if message.document.mime_type.startswith(("image/", "video/")):
            return message.document.file_id
    return None


def _ffmpeg_executable() -> Optional[str]:
    import shutil

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _parse_ffmpeg_duration(stderr_text: str) -> float:
    match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d+)", stderr_text)
    if not match:
        return 10.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _frame_quality_score(image_bytes: bytes) -> float:
    import numpy as np

    gray = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("L"), dtype=np.float32)
    if gray.size < 100:
        return 0.0

    brightness = float(gray.mean())
    if brightness < 25 or brightness > 235:
        return 0.0

    laplacian = (
        -4 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(laplacian.var())


def _sample_timestamps(duration: float, samples: int = 15) -> list[float]:
    if duration <= 0.3:
        return [max(0.0, duration / 2)]
    start = duration * 0.05
    end = max(start + 0.1, duration * 0.95)
    if samples <= 1:
        return [start]
    step = (end - start) / (samples - 1)
    return [start + step * i for i in range(samples)]


def _extract_frame_at(ffmpeg: str, video_path: str, timestamp: float) -> Optional[bytes]:
    output_path = f"{video_path}.{int(timestamp * 1000)}.jpg"
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                video_path,
                "-vframes",
                "1",
                "-q:v",
                "2",
                output_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not os.path.exists(output_path):
            return None
        with open(output_path, "rb") as frame_file:
            return frame_file.read()
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def _extract_best_video_frame(video_bytes: bytes, samples: int = 15) -> bytes:
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        try:
            return _extract_best_video_frame_opencv(video_bytes, samples=samples)
        except ImportError as exc:
            raise RuntimeError(
                "Для видео установите: pip install imageio-ffmpeg numpy"
            ) from exc

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as video_file:
        video_file.write(video_bytes)
        video_path = video_file.name

    try:
        probe = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = _parse_ffmpeg_duration(probe.stderr or "")
        timestamps = _sample_timestamps(duration, samples=samples)

        best_bytes: Optional[bytes] = None
        best_score = -1.0
        for timestamp in timestamps:
            frame_bytes = _extract_frame_at(ffmpeg, video_path, timestamp)
            if not frame_bytes:
                continue
            score = _frame_quality_score(frame_bytes)
            if score > best_score:
                best_score = score
                best_bytes = frame_bytes

        if best_bytes:
            logger.info("Best video frame score=%.1f from %d samples", best_score, len(timestamps))
            return best_bytes
        raise RuntimeError("Не удалось извлечь кадры из видео")
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass


def _extract_best_video_frame_opencv(video_bytes: bytes, samples: int = 15) -> bytes:
    import cv2
    import numpy as np

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as video_file:
        video_file.write(video_bytes)
        video_path = video_file.name

    try:
        capture = cv2.VideoCapture(video_path)
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        if total <= 1:
            indices = [0]
        else:
            start_idx = max(0, int(total * 0.05))
            end_idx = max(start_idx, int(total * 0.95) - 1)
            if end_idx <= start_idx:
                indices = [total // 2]
            else:
                step = (end_idx - start_idx) / max(samples - 1, 1)
                indices = sorted({int(start_idx + step * i) for i in range(samples)})

        best_bytes: Optional[bytes] = None
        best_score = -1.0
        for index in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            buf = io.BytesIO()
            Image.fromarray(np.asarray(rgb)).save(buf, format="JPEG", quality=90)
            frame_bytes = buf.getvalue()
            score = _frame_quality_score(frame_bytes)
            if score > best_score:
                best_score = score
                best_bytes = frame_bytes
        capture.release()

        if best_bytes:
            logger.info("Best video frame (opencv) score=%.1f", best_score)
            return best_bytes
        raise RuntimeError("Не удалось прочитать кадры из видео")
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass


def _extract_video_frame(video_bytes: bytes) -> bytes:
    return _extract_best_video_frame(video_bytes)


async def _get_source_bytes(data: dict, file_id: Optional[str]) -> bytes:
    if file_id:
        return await download_telegram_photo(bot, file_id)
    cached = data.get("photo_bytes_b64")
    if cached:
        return base64.b64decode(cached)
    raise RuntimeError("Нет исходного изображения")


async def generate_redesign(
    prompt: str,
    photo_bytes: bytes,
    *,
    reference_bytes: Optional[list[bytes]] = None,
    is_revision: bool = False,
    strength: Optional[float] = None,
    guidance_scale: float = 3.5,
    max_side: int = 1024,
) -> bytes:
    if strength is None:
        strength = max(0.55, POLZA_STRENGTH - 0.07) if is_revision else POLZA_STRENGTH
    all_images = [photo_bytes] + list(reference_bytes or [])
    loop = asyncio.get_running_loop()
    last_err: Optional[Exception] = None

    for attempt in range(3):
        try:
            return await loop.run_in_executor(
                None, _generate_via_polza, prompt, all_images, strength, guidance_scale
            )
        except Exception as exc:
            last_err = exc
            if _is_billing_error(exc) or _is_auth_error(exc):
                raise
            if _is_retryable_error(exc):
                await asyncio.sleep(5 * (attempt + 1))
                continue
            raise

    raise last_err or RuntimeError("Не удалось сгенерировать изображение")


bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
sub_store = SubscriptionStore()
billing: Optional[YooKassaBilling] = None


def _is_subscription_admin(user_id: int, username: Optional[str] = None) -> bool:
    if user_id in SUBSCRIPTION_ADMIN_IDS:
        return True
    if username and username.lower() in SUBSCRIPTION_ADMIN_USERNAMES:
        return True
    return False


def _user_can_generate(user_id: int, username: Optional[str] = None) -> bool:
    if not SUBSCRIPTION_ENABLED:
        return True
    if _is_subscription_admin(user_id, username):
        return True
    return sub_store.can_generate(user_id)


def _user_consume_generation(user_id: int, username: Optional[str] = None) -> None:
    if not SUBSCRIPTION_ENABLED or _is_subscription_admin(user_id, username):
        return
    sub_store.consume_generation(user_id)


def _subscribe_prompt(user_id: Optional[int] = None) -> str:
    if user_id is not None:
        user = sub_store.get_user(user_id)
        if not user.get("trial_used"):
            return (
                "🎁 У вас есть 1 бесплатная пробная генерация.\n"
                "Нажмите /design, чтобы использовать её.\n\n"
                "После пробы понадобится подписка:\n"
                f"{SUBSCRIPTION_PRICE_RUB:.0f} ₽ / {SUBSCRIPTION_DAYS} дн., "
                f"{SUBSCRIPTION_GENERATIONS} генераций.\n"
                "/subscribe — оформить"
            )
    return (
        f"Для генерации нужна подписка: {SUBSCRIPTION_PRICE_RUB:.0f} ₽ / "
        f"{SUBSCRIPTION_DAYS} дн., {SUBSCRIPTION_GENERATIONS} генераций.\n\n"
        "/subscribe — оплатить\n"
        "/status — проверить подписку"
    )


def _payment_kb(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="pay:check")],
        ]
    )


async def _require_subscription(message: Message) -> bool:
    user = message.from_user
    if _user_can_generate(user.id, user.username):
        return True
    await message.answer(_subscribe_prompt(user.id), reply_markup=_subscribe_inline_kb())
    return False


def _subscribe_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="pay:subscribe")],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="pay:check")],
        ]
    )


async def _wrong_step_alert(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current == DesignStates.photo.state:
        text = "Сначала пришлите фото или видео комнаты (см. сообщение выше)."
    elif current is None:
        text = "Нажмите /design чтобы начать."
    else:
        text = "Эта кнопка устарела. Нажмите /design и начните заново."
    await callback.answer(text, show_alert=True)


async def begin_design(message: Message, state: FSMContext) -> None:
    if not await _require_subscription(message):
        return
    await state.clear()
    await state.set_state(DesignStates.photo)
    await message.answer(
        "📷 Шаг 1 из 6\n"
        "Пришлите одно из:\n"
        "• фото комнаты\n"
        "• короткое видео комнаты (выберу лучший кадр)\n\n"
        "⚠️ Не нажимайте старые кнопки выше.\n"
        "/cancel — отмена"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я переделаю дизайн комнаты по фото или видео.\n\n"
        "Есть стиль «простой / бюджетный» — для обычной квартиры без дизайнерского ремонта.\n"
        "Можно прислать фото мебели с сайта или из каталога — бот постарается её учесть.\n\n"
        "/design — начать\n"
        "/subscribe — подписка\n"
        "/status — остаток генераций\n"
        "/cancel — отмена"
    )


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    if not SUBSCRIPTION_ENABLED or billing is None:
        await message.answer("Оплата временно недоступна.")
        return
    try:
        loop = asyncio.get_running_loop()
        payment_id, url = await loop.run_in_executor(
            None, billing.create_payment, message.from_user.id
        )
        await message.answer(
            f"Подписка: {SUBSCRIPTION_PRICE_RUB:.0f} ₽ на {SUBSCRIPTION_DAYS} дн.\n"
            f"Включено {SUBSCRIPTION_GENERATIONS} генераций.\n\n"
            "1. Нажмите «Оплатить»\n"
            "2. После оплаты — «Проверить оплату»\n\n"
            f"ID платежа: `{payment_id}`",
            reply_markup=_payment_kb(url),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Payment create failed")
        await message.answer(f"Не удалось создать платёж: {exc}")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not SUBSCRIPTION_ENABLED:
        await message.answer("Подписка не настроена — генерации доступны всем.")
        return
    await message.answer(sub_store.status_text(message.from_user.id))


@dp.callback_query(F.data == "pay:subscribe")
async def on_pay_subscribe(callback: CallbackQuery):
    await callback.answer()
    await cmd_subscribe(callback.message)


@dp.callback_query(F.data == "pay:check")
async def on_pay_check(callback: CallbackQuery):
    if not SUBSCRIPTION_ENABLED or billing is None:
        await callback.answer("Оплата недоступна", show_alert=True)
        return

    payment_id = sub_store.latest_pending_payment(callback.from_user.id)
    if not payment_id:
        await callback.answer("Нет ожидающих платежей. Нажмите /subscribe", show_alert=True)
        return

    await callback.answer("Проверяю…")
    try:
        loop = asyncio.get_running_loop()
        ok, text, _ = await loop.run_in_executor(
            None, billing.try_activate_payment, payment_id
        )
        if ok and "активирована" in text.lower():
            await callback.message.answer(
                f"✅ {text}\n\n{sub_store.status_text(callback.from_user.id)}"
            )
        else:
            await callback.message.answer(text)
    except Exception as exc:
        logger.exception("Payment check failed")
        await callback.message.answer(f"Ошибка проверки: {exc}")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. /design — когда будете готовы.")


@dp.message(Command("design"))
async def cmd_design(message: Message, state: FSMContext):
    await begin_design(message, state)


@dp.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext):
    await message.answer(
        "Режим планировки отключён.\n\n"
        "Используйте /design — редизайн по фото или видео комнаты."
    )


@dp.message(F.text.lower().in_({"дизайн", "начать", "старт"}))
async def cmd_design_text(message: Message, state: FSMContext):
    await begin_design(message, state)


@dp.callback_query(F.data == "action:new")
async def on_new_design(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await begin_design(callback.message, state)


@dp.callback_query(F.data.in_({"action:plan_new", "action:plan_edit"}))
async def on_plan_disabled(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Режим планировки отключён", show_alert=True)


@dp.callback_query(F.data == "action:edit")
async def on_edit_request(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.revision.state:
        await callback.answer("Сначала сгенерируйте дизайн через /design", show_alert=True)
        return
    await callback.answer()
    await state.set_state(DesignStates.revision)
    await callback.message.answer(
        "✏️ Опишите правки одним сообщением.\n\n"
        "Например: «добавьте зелёное растение у окна», "
        "«сделайте стены светлее», «уберите ковёр».\n\n"
        "/cancel — отмена"
    )


@dp.message(DesignStates.photo, F.photo | F.video | F.document)
async def on_media(message: Message, state: FSMContext):
    file_id = _extract_media_file_id(message)
    if not file_id:
        await message.answer("Нужно фото (JPG, PNG) или видео (MP4).")
        return

    if _is_video_message(message):
        status = await message.answer("Ищу лучший кадр в видео…")
        try:
            video_bytes = await download_telegram_photo(bot, file_id)
            frame_bytes = await asyncio.get_running_loop().run_in_executor(
                None, _extract_video_frame, video_bytes
            )
            prepared = _prepare_photo(frame_bytes)
            await state.update_data(
                photo_bytes_b64=base64.b64encode(prepared).decode("ascii"),
                from_video=True,
            )
        except Exception as exc:
            await message.answer(f"Не удалось обработать видео: {exc}\n\nПришлите фото.")
            return
        finally:
            try:
                await status.delete()
            except Exception:
                pass

        await state.set_state(DesignStates.room)
        await message.answer(
            "Лучший кадр из видео ✅\n\n"
            "Шаг 2 из 6. Какая это комната?",
            reply_markup=_kb(ROOMS, "room"),
        )
        return

    await state.update_data(photo_file_id=file_id, from_video=False)
    await state.set_state(DesignStates.room)
    await message.answer(
        "Фото получено ✅\n\n"
        "Шаг 2 из 6. Какая это комната?",
        reply_markup=_kb(ROOMS, "room"),
    )


@dp.message(DesignStates.photo)
async def on_photo_required(message: Message):
    await message.answer(
        "Пришлите фото или видео комнаты.\n"
        "Или /design — начать заново."
    )


@dp.callback_query(F.data.startswith("room:"))
async def on_room(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.room.state:
        await _wrong_step_alert(callback, state)
        return

    key = callback.data.split(":", 1)[1]
    if key not in ROOMS:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return

    await state.update_data(room=ROOMS[key]["label"], room_key=key)
    await state.set_state(DesignStates.style)
    await callback.message.edit_text(
        f"Комната: {ROOMS[key]['label']}\n\nШаг 3 из 6. Какой стиль?",
        reply_markup=_kb(STYLES, "style"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("style:"))
async def on_style(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.style.state:
        await _wrong_step_alert(callback, state)
        return

    key = callback.data.split(":", 1)[1]
    if key not in STYLES:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return

    data = await state.get_data()
    await state.update_data(style=STYLES[key]["label"], style_key=key)
    await state.set_state(DesignStates.mood)
    hint = STYLES[key].get("hint", "")
    hint_line = f"\n💡 {hint}" if hint else ""
    await callback.message.edit_text(
        f"Комната: {data['room']}\n"
        f"Стиль: {STYLES[key]['label']}{hint_line}\n\n"
        "Шаг 4 из 6. Настроение и цвета?",
        reply_markup=_kb(MOODS, "mood"),
    )
    await callback.answer()


async def _go_to_furniture_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(furniture_refs_b64=[])
    await state.set_state(DesignStates.furniture)
    await message.answer(
        f"Комната: {data['room']}\n"
        f"Стиль: {data['style']}\n"
        f"Настроение: {data['mood']}\n\n"
        "Шаг 5 из 6. Фото мебели (необязательно)\n"
        "Пришлите до 3 фото вещей, которые хотите видеть в комнате "
        "(диван с сайта, шкаф, стол…).\n"
        "Или нажмите «Пропустить».",
        reply_markup=_furniture_kb(0),
    )


async def _go_to_extra_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    furn_count = len(data.get("furniture_refs_b64") or [])
    furn_line = f"\n🪑 Фото мебели: {furn_count}" if furn_count else ""
    await state.set_state(DesignStates.extra)
    skip_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить →", callback_data="extra:skip")]]
    )
    await message.answer(
        f"Комната: {data['room']}\n"
        f"Стиль: {data['style']}\n"
        f"Настроение: {data['mood']}"
        f"{furn_line}\n\n"
        "Шаг 6 из 6. Дополнительные пожелания текстом или «Пропустить».",
        reply_markup=skip_kb,
    )


@dp.callback_query(F.data.startswith("mood:"))
async def on_mood(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.mood.state:
        await _wrong_step_alert(callback, state)
        return

    key = callback.data.split(":", 1)[1]
    if key not in MOODS:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return

    data = await state.get_data()
    await state.update_data(mood=MOODS[key]["label"], mood_key=key)
    await callback.answer()
    await _go_to_furniture_step(callback.message, state)


@dp.callback_query(F.data == "furniture:skip")
async def on_furniture_skip(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.furniture.state:
        await _wrong_step_alert(callback, state)
        return
    await callback.answer()
    await state.update_data(furniture_refs_b64=[])
    await _go_to_extra_step(callback.message, state)


@dp.callback_query(F.data == "furniture:done")
async def on_furniture_done(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.furniture.state:
        await _wrong_step_alert(callback, state)
        return
    data = await state.get_data()
    if not data.get("furniture_refs_b64"):
        await callback.answer("Сначала пришлите фото мебели или нажмите «Пропустить»", show_alert=True)
        return
    await callback.answer()
    await _go_to_extra_step(callback.message, state)


@dp.message(DesignStates.furniture, F.photo | F.document)
async def on_furniture_photo(message: Message, state: FSMContext):
    file_id = _extract_media_file_id(message)
    if not file_id or _is_video_message(message):
        await message.answer("Пришлите фото мебели (JPG, PNG).")
        return

    data = await state.get_data()
    refs = list(data.get("furniture_refs_b64") or [])
    if len(refs) >= MAX_FURNITURE_REFS:
        await message.answer(
            f"Уже {MAX_FURNITURE_REFS} фото. Нажмите «Готово» или «Пропустить».",
            reply_markup=_furniture_kb(len(refs)),
        )
        return

    try:
        raw = await download_telegram_photo(bot, file_id)
        prepared = _prepare_photo(raw, max_side=768)
        refs.append(base64.b64encode(prepared).decode("ascii"))
    except Exception as exc:
        await message.answer(f"Не удалось загрузить фото: {exc}")
        return

    await state.update_data(furniture_refs_b64=refs)
    await message.answer(
        f"Фото {len(refs)} из {MAX_FURNITURE_REFS} добавлено ✅\n"
        + ("Можно прислать ещё или нажать «Готово»." if len(refs) < MAX_FURNITURE_REFS else "Нажмите «Готово»."),
        reply_markup=_furniture_kb(len(refs)),
    )


@dp.message(DesignStates.furniture)
async def on_furniture_required(message: Message):
    await message.answer(
        "Пришлите фото мебели или нажмите кнопку под сообщением.\n"
        "/cancel — отмена"
    )


@dp.callback_query(F.data == "extra:skip")
async def on_extra_skip(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != DesignStates.extra.state:
        await _wrong_step_alert(callback, state)
        return
    await callback.answer()
    await _generate_and_send(callback.message, state, extra="")


@dp.message(DesignStates.extra, F.text)
async def on_extra_text(message: Message, state: FSMContext):
    await _generate_and_send(message, state, extra=message.text.strip())


async def _generate_and_send(
    message: Message,
    state: FSMContext,
    extra: str = "",
    revision_text: str = "",
):
    data = await state.get_data()
    is_revision = bool(revision_text)
    user_id = message.from_user.id
    username = message.from_user.username

    if not _user_can_generate(user_id, username):
        await message.answer(_subscribe_prompt(user_id), reply_markup=_subscribe_inline_kb())
        return

    for key_field in ("room_key", "style_key", "mood_key"):
        if key_field not in data:
            await state.clear()
            await message.answer("Сессия устарела. Нажмите /design и пройдите заново.")
            return

    furniture_b64 = data.get("furniture_refs_b64") or []
    furniture_bytes = [base64.b64decode(b) for b in furniture_b64]
    furn_count = len(furniture_bytes)

    if is_revision:
        file_id = data.get("last_result_file_id") or data.get("photo_file_id")
        if not file_id and not data.get("photo_bytes_b64"):
            await state.clear()
            await message.answer("Нет изображения для правок. /design — начать заново.")
            return
        prompt = build_revision_prompt(
            data["room_key"], data["style_key"], data["mood_key"], revision_text, furn_count
        )
        caption_extra = revision_text
    else:
        file_id = data.get("photo_file_id")
        if not file_id and not data.get("photo_bytes_b64"):
            await state.clear()
            await message.answer("Фото не найдено. /design — начать заново.")
            return
        prompt = build_prompt(
            data["room_key"], data["style_key"], data["mood_key"], extra, furn_count
        )
        caption_extra = extra

    saved = {
        "room": data["room"],
        "style": data["style"],
        "mood": data["mood"],
    }
    source_label = "\n🎬 Лучший кадр из видео" if data.get("from_video") else ""
    if furn_count and not is_revision:
        source_label += f"\n🪑 С учётом {furn_count} фото мебели"

    status = await message.answer(
        "Вношу правки… 30–120 сек." if is_revision else "Генерирую дизайн… 30–120 сек."
    )

    try:
        photo_bytes = await _get_source_bytes(data, file_id)
        result_bytes = await generate_redesign(
            prompt,
            photo_bytes,
            reference_bytes=furniture_bytes if not is_revision else None,
            is_revision=is_revision,
            strength=POLZA_STRENGTH,
        )
        photo = BufferedInputFile(result_bytes, filename="interior-redesign.png")
        sent = await message.answer_photo(
            photo,
            caption=(
                f"🏠 {saved['room']}\n"
                f"🎨 {saved['style']}\n"
                f"💡 {saved['mood']}"
                + source_label
                + (f"\n📝 {caption_extra}" if caption_extra else "")
                + "\n\nМожно внести ещё правки или начать заново."
            ),
            reply_markup=_result_kb(),
        )
        await state.update_data(
            room=saved["room"],
            style=saved["style"],
            mood=saved["mood"],
            room_key=data["room_key"],
            style_key=data["style_key"],
            mood_key=data["mood_key"],
            from_video=data.get("from_video", False),
            furniture_refs_b64=data.get("furniture_refs_b64", []),
            photo_file_id=data.get("photo_file_id"),
            photo_bytes_b64=data.get("photo_bytes_b64"),
            last_result_file_id=sent.photo[-1].file_id,
            last_revision=revision_text if is_revision else data.get("last_revision", ""),
        )
        await state.set_state(DesignStates.revision)
        _user_consume_generation(user_id, username)
        user_after = sub_store.get_user(user_id) if SUBSCRIPTION_ENABLED else None
        if user_after is not None and not _is_subscription_admin(user_id, username):
            try:
                paid_until = user_after.get("paid_until")
                left = user_after.get("generations_left", 0)
                if paid_until and left >= 0:
                    await message.answer(f"Осталось генераций: {left}")
                elif user_after.get("trial_used"):
                    await message.answer(
                        "🎁 Пробная генерация использована.\n"
                        + _subscribe_prompt(user_id),
                        reply_markup=_subscribe_inline_kb(),
                    )
            except Exception:
                pass
    except Exception as exc:
        logger.exception("Generation failed")
        await message.answer(format_generation_error(exc))
    finally:
        try:
            await status.delete()
        except Exception:
            pass


@dp.message(DesignStates.revision, F.text)
async def on_revision_text(message: Message, state: FSMContext):
    if message.text.startswith("/"):
        return
    await _generate_and_send(message, state, revision_text=message.text.strip())


@dp.message(F.photo | F.video | F.document)
async def on_media_outside_flow(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Сначала нажмите /design")
    elif current == DesignStates.revision.state:
        await message.answer("Опишите правки текстом или нажмите «Внести правки».")


@dp.message()
async def fallback(message: Message, state: FSMContext):
    if await state.get_state() == DesignStates.revision.state:
        await message.answer(
            "Опишите правки текстом, например: «добавьте торшер у дивана».\n"
            "Или нажмите кнопку «Внести правки» под последним результатом."
        )
        return
    await message.answer("/design — начать, /start — справка")


def _http_listen_port() -> int:
    """Render задаёт PORT; локально — WEBHOOK_PORT (8765)."""
    port_env = os.getenv("PORT")
    if port_env:
        return int(port_env)
    return WEBHOOK_PORT


async def _start_http_server() -> Optional[Any]:
    from aiohttp import web

    on_render = bool(os.getenv("PORT"))
    if not on_render and not SUBSCRIPTION_ENABLED:
        return None

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    if SUBSCRIPTION_ENABLED and billing is not None:

        async def handle_yookassa(request: web.Request) -> web.Response:
            try:
                body = await request.read()
                loop = asyncio.get_running_loop()
                user_id = await loop.run_in_executor(
                    None, billing.process_webhook_body, body
                )
                if user_id:
                    await bot.send_message(
                        user_id,
                        "✅ Оплата получена! Подписка активирована.\n\n"
                        + sub_store.status_text(user_id),
                    )
            except Exception:
                logger.exception("YuKassa webhook error")
            return web.Response(text="ok")

        app.router.add_post("/webhook/yookassa", handle_yookassa)

    listen_port = _http_listen_port()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", listen_port)
    await site.start()
    logger.info("HTTP server on 0.0.0.0:%s (render=%s)", listen_port, on_render)
    return runner


async def main():
    global billing
    for attempt in range(5):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            break
        except Exception as exc:
            if attempt == 4:
                raise
            logger.warning("Telegram connect retry %s: %s", attempt + 1, exc)
            await asyncio.sleep(3 * (attempt + 1))

    me = await bot.get_me()
    if SUBSCRIPTION_ENABLED:
        billing = YooKassaBilling(
            YOOKASSA_SHOP_ID,
            YOOKASSA_SECRET_KEY,
            sub_store,
            price_rub=SUBSCRIPTION_PRICE_RUB,
            subscription_days=SUBSCRIPTION_DAYS,
            subscription_generations=SUBSCRIPTION_GENERATIONS,
            return_url=f"https://t.me/{me.username}",
        )
        logger.info(
            "Subscriptions enabled: %s RUB / %s days / %s gens",
            SUBSCRIPTION_PRICE_RUB,
            SUBSCRIPTION_DAYS,
            SUBSCRIPTION_GENERATIONS,
        )

    http_runner = await _start_http_server()
    logger.info("Bot started, provider=polza, model=%s", POLZA_MODEL)
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        if http_runner is not None:
            await http_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Bot crashed")
        raise
