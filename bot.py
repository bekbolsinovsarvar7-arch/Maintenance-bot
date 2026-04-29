import os
import asyncio
import logging
import re
import json
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from dotenv import load_dotenv

# Google AI – new SDK
from google import genai
from google.genai import types as genai_types   # <-- renamed to avoid conflict

from database import init_db, get_diagnosis, log_feedback

# -------------------------------------------------------------------
#  Logging
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
#  Environment & Tokens
# -------------------------------------------------------------------
load_dotenv(Path(__file__).parent / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
AI_KEY = os.getenv("GEMINI_API_KEY")
if not BOT_TOKEN or not AI_KEY:
    logger.critical("Missing BOT_TOKEN or GEMINI_API_KEY in .env")
    exit(1)

# -------------------------------------------------------------------
#  AI Client (new SDK)
# -------------------------------------------------------------------
genai_client = genai.Client(api_key=AI_KEY)

# Safety settings – all filters off
SAFETY_SETTINGS = [
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
]

# -------------------------------------------------------------------
#  Bot & Dispatcher (with FSM storage)
# -------------------------------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# -------------------------------------------------------------------
#  FSM State (conversation memory)
# -------------------------------------------------------------------
class DiagState(StatesGroup):
    last_diag = State()          # stores raw AI text for follow‑ups

# -------------------------------------------------------------------
#  Keyboards
# -------------------------------------------------------------------
def main_menu_kb():
    builder = ReplyKeyboardBuilder()
    builder.row(
        types.KeyboardButton(text="🔍 New Diagnostic"),
        types.KeyboardButton(text="📜 Recent Searches")
    )
    return builder.as_markup(resize_keyboard=True)

def action_inline_kb(prefix: str):
    """Generate inline buttons: Summary, Steps, Parts, Rate."""
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="📋 Summary", callback_data=f"summary:{prefix}"),
        types.InlineKeyboardButton(text="🔧 Detailed Steps", callback_data=f"steps:{prefix}"),
        types.InlineKeyboardButton(text="⚙️ Parts", callback_data=f"parts:{prefix}")
    )
    builder.row(
        types.InlineKeyboardButton(text="👍 Helpful", callback_data=f"rate:{prefix}:1"),
        types.InlineKeyboardButton(text="👎 Not helpful", callback_data=f"rate:{prefix}:0")
    )
    return builder.as_markup()

# -------------------------------------------------------------------
#  Helpers
# -------------------------------------------------------------------
FAULT_RE = re.compile(r"\b(SPN|FMI|SID|PID)\s*\d+\b", re.IGNORECASE)

def extract_fault_codes(text: str):
    return FAULT_RE.findall(text)

def parse_ai_json(text: str) -> dict:
    """Try to pull a JSON object from the model's response."""
    try:
        start = text.index('{')
        end = text.rindex('}') + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"raw": text}

def format_parsed(parsed: dict) -> str:
    if "raw" in parsed:
        return f"🛡 **AI Analysis:**\n{parsed['raw'][:1500]}"
    diag = parsed.get("diagnosis", "N/A")
    urg = parsed.get("urgency", "Medium")
    causes = ", ".join(parsed.get("causes", []))
    checks = ", ".join(parsed.get("checks", []))
    solution = ", ".join(parsed.get("solution", []))
    parts = ", ".join(parsed.get("parts", []))
    return (
        f"**Diagnosis:** {diag}\n"
        f"**Urgency:** {urg}\n"
        f"**Causes:** {causes}\n"
        f"**Checks:** {checks}\n"
        f"**Solution:** {solution}\n"
        f"**Parts Needed:** {parts}"
    )

# -------------------------------------------------------------------
#  Handlers
# -------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🚛 **Maintenance Specialist Pro**\n\n"
        "I can help you with:\n"
        "• Describing a fault (text)\n"
        "• Uploading photos of broken parts\n"
        "• Fault codes (e.g. SPN 123 FMI 4)\n\n"
        "Send me what you’ve got!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "📜 Recent Searches")
async def recent_searches(message: types.Message):
    await message.answer("📋 History feature coming soon – I'm working on it!")

@dp.message(F.text == "🔍 New Diagnostic")
async def new_diag(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Please describe the problem, upload a photo, or send a fault code.")

# --- Photo with optional caption ---
@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    caption = message.caption or ""
    loading = await message.answer("📸 *Analyzing image...*", parse_mode="Markdown")
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    file_path = f"temp_{file_id}.jpg"
    await bot.download_file(file.file_path, file_path)

    try:
        # Read image bytes
        with open(file_path, "rb") as f:
            img_data = f.read()

        prompt_text = f"""
You are a senior truck technician. Analyze the attached image.
User comment (if any): "{caption}"
Return ONLY a JSON object (no markdown) with these keys:
"diagnosis", "causes" (array), "checks" (array), "solution" (array), "parts" (array), "urgency" ("Low"/"Medium"/"High").
"""
        # Build contents for the new SDK
        contents = [
            genai_types.Part.from_bytes(data=img_data, mime_type="image/jpeg"),
            genai_types.Part.from_text(text=prompt_text)
        ]

        # Async generate
        response = await genai_client.aio.models.generate_content(
            model='models/gemini-2.5-flash',   # or 'gemini-1.5-flash-latest'
            contents=contents,
            config=genai_types.GenerateContentConfig(
                safety_settings=SAFETY_SETTINGS,
                temperature=0.2,
                max_output_tokens=900,
            ),
        )
        parsed = parse_ai_json(response.text)
        formatted = format_parsed(parsed)

        await state.update_data(last_ai=response.text)
        await loading.edit_text(formatted, reply_markup=action_inline_kb("img"))
    except Exception as e:
        logger.exception("Photo analysis failed")
        await loading.edit_text(f"❌ Analysis error: {str(e)[:200]}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)   # <-- synchronous remove is fine for temp files

# --- Text messages ---
@dp.message(F.text)
async def handle_text(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text

    # 1. Fault codes
    codes = extract_fault_codes(text)
    if codes:
        await message.answer(f"⚡ Detected codes: {', '.join(codes)}. Checking database...")
        rows = []
        for code in codes:
            res = await get_diagnosis(code)
            if res:
                rows.extend(res)
        if rows:
            seen = set()
            resp = ""
            for r in rows:
                if r[0] not in seen:
                    seen.add(r[0])
                    resp += f"🔧 **{r[1]}** [Urgency: {r[4]}]\n{r[3]}\n\n"
            await message.answer(resp, reply_markup=action_inline_kb("fc"))
            return
        else:
            await message.answer("Nothing in local DB. Asking AI...")

    # 2. Local FTS search
    db_rows = await get_diagnosis(text)
    if db_rows:
        resp = ""
        for row in db_rows:
            resp += f"✅ **{row[1]}**\n**Solution:** {row[3]}\n**Urgency:** {row[4]}\n\n"
        await message.answer(resp, reply_markup=action_inline_kb("db"))
        return

    # 3. AI fallback
    loading = await message.answer("⚙️ *Consulting AI knowledge base...*")
    try:
        prompt = f"""
You are a master diesel mechanic. A technician reports: "{text}"
Return ONLY a JSON (no markdown) with these keys:
"diagnosis", "causes" (array), "checks" (array), "solution" (array), "parts" (array), "urgency" ("Low"/"Medium"/"High").
"""
        response = await genai_client.aio.models.generate_content(
            model='models/gemini-2.5-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                safety_settings=SAFETY_SETTINGS,
                temperature=0.2,
                max_output_tokens=900,
            ),
        )
        parsed = parse_ai_json(response.text)
        formatted = format_parsed(parsed)

        await state.update_data(last_ai=response.text)
        await loading.edit_text(formatted, reply_markup=action_inline_kb("ai"))
    except Exception as e:
        logger.exception("AI text generation failed")
        await loading.edit_text("❌ AI service temporarily unavailable. Please try again.")

# -------------------------------------------------------------------
#  Inline Callbacks
# -------------------------------------------------------------------
@dp.callback_query(F.data.startswith("summary:"))
async def cb_summary(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    raw = data.get("last_ai", "")
    if not raw:
        await callback.answer("No previous diagnosis found.", show_alert=True)
        return
    lines = [l.strip() for l in raw.split('\n') if l.strip()][:12]
    summary = "\n".join(lines)
    await callback.message.answer(f"📝 **REPAIR LOG SUMMARY**\n\n```\n{summary}\n```", parse_mode="Markdown")
    await callback.answer("Summary generated!")

@dp.callback_query(F.data.startswith("steps:"))
async def cb_steps(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    raw = data.get("last_ai", "")
    if not raw:
        await callback.answer("No steps available.", show_alert=True)
        return
    parsed = parse_ai_json(raw)
    if "raw" not in parsed and "solution" in parsed:
        steps = "\n".join(f"• {s}" for s in parsed["solution"])
        await callback.message.answer(f"🔧 **Detailed Steps:**\n{steps}")
    else:
        await callback.message.answer(f"📋 Steps (raw):\n```\n{raw[:800]}\n```", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("parts:"))
async def cb_parts(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    raw = data.get("last_ai", "")
    if not raw:
        await callback.answer("No parts info.", show_alert=True)
        return
    parsed = parse_ai_json(raw)
    if "raw" not in parsed and "parts" in parsed:
        parts = "\n".join(f"• {p}" for p in parsed["parts"])
        await callback.message.answer(f"⚙️ **Likely Parts Needed:**\n{parts}")
    else:
        await callback.message.answer("Parts list not structured. Try the diagnosis again.")
    await callback.answer()

@dp.callback_query(F.data.startswith("rate:"))
async def cb_rate(callback: types.CallbackQuery):
    _, prefix, score = callback.data.split(":")
    await log_feedback(callback.from_user.id, prefix, int(score))
    emoji = "👍" if score == "1" else "👎"
    await callback.answer(f"Thank you for your feedback {emoji}!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# -------------------------------------------------------------------
#  Entry point
# -------------------------------------------------------------------
async def main():
    await init_db()
    logger.info("🚀 Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
