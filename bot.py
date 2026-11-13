import os
import re
import io
import time
import json
import html
import random
import base64
import asyncio
import threading
import http.server
import socketserver
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "-1004369449565"))
OWNER_ID = int(os.getenv("OWNER_ID", "8283878643"))
BOT_PASSWORD = os.getenv("BOT_PASSWORD", "Boss@2011123")
VAULT_AUTO_DELETE_SECONDS = 7200   # My Study Vault के लिए फिक्स 2 घंटे
LOGIN_VALIDITY_SECONDS = 86400     # 24 घंटे में सिर्फ़ 1 बार लॉगिन
DATA_FILE = "sanskari_study_data.json"
# =================================================

login_expiry_ts = 0.0
user_states = {}  # {user_id: {...}}


# ================= PERSISTENT DATA & AUTO-BACKUP =================
def default_data() -> dict:
    return {
        "counter": 1,
        "subjects": {},   # Study Vault: {sid: {"name": str, "topics": {tid: {"name": str, "subtopics": {subid: {"name": str, "files": [...]}}}}}}
        "mistakes": {},   # Mistake Bank: {"phy_0": [{"id": "m1", "title": str, "text": str, "msg_id": int|None, "type": "text"|"photo"}]}
        "tests": {},      # HTML Live Tests: {testid: {"name": str, "questions": [{"q": str, "options": [...], "ans": int, "exp": str}]}}
    }


def load_local_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                d.setdefault("counter", 1)
                d.setdefault("subjects", {})
                d.setdefault("mistakes", {})
                d.setdefault("tests", {})
                return d
        except Exception:
            pass
    return default_data()


vault_data = load_local_data()


def next_id(prefix: str) -> str:
    cid = vault_data.get("counter", 1)
    vault_data["counter"] = cid + 1
    return f"{prefix}{cid}"


async def save_and_pin_backup(bot=None):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(vault_data, f, ensure_ascii=False, indent=2)

    if bot and STORAGE_CHANNEL_ID:
        try:
            chat = await bot.get_chat(STORAGE_CHANNEL_ID)
            old_pinned_id = chat.pinned_message.message_id if chat.pinned_message else None

            with open(DATA_FILE, "rb") as f:
                sent = await bot.send_document(
                    chat_id=STORAGE_CHANNEL_ID,
                    document=f,
                    filename=DATA_FILE,
                    caption="🔄 Sanskari Study Auto-Backup (Pinned - डिलीट न करें)",
                )
            await bot.pin_chat_message(
                chat_id=STORAGE_CHANNEL_ID,
                message_id=sent.message_id,
                disable_notification=True,
            )
            if old_pinned_id and old_pinned_id != sent.message_id:
                try:
                    await bot.delete_message(chat_id=STORAGE_CHANNEL_ID, message_id=old_pinned_id)
                except Exception:
                    pass
        except Exception:
            pass


async def restore_from_pinned_backup(app: Application):
    global vault_data
    try:
        chat = await app.bot.get_chat(STORAGE_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.document:
            doc = chat.pinned_message.document
            if doc.file_name == DATA_FILE:
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(DATA_FILE)
                vault_data = load_local_data()
                print("✅ Sanskari Study backup restored!")
    except Exception as e:
        print("Backup restore skipped:", e)


# ================= FAST TEXT CHUNKER & AUTO-DELETE ENGINE =================
def split_long_text(text: str, max_len: int = 3800) -> list[str]:
    """लंबे टेक्स्ट/फॉर्मूला शीट को बिना HTML तोड़े सुरक्षित हिस्सों में बाँटता है ताकि टेलीग्राम पर तुरंत खुले।"""
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def parse_duration_seconds(raw_text: str) -> tuple[int, str]:
    txt = raw_text.strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)", txt)
    if not m:
        return 300, "5 मिनट"
    val = float(m.group(1))
    if any(k in txt for k in ["घंट", "hour", "hr", "h"]):
        secs = int(val * 3600)
        label = f"{int(val) if val.is_integer() else val} घंटा"
    elif any(k in txt for k in ["सेक", "sec", "s"]):
        secs = int(val)
        label = f"{int(val)} सेकंड"
    else:
        secs = int(val * 60)
        label = f"{int(val) if val.is_integer() else val} मिनट"
    secs = max(10, min(secs, 86400))
    return secs, label


def format_elapsed_time(seconds: float) -> str:
    sec = max(1, int(seconds))
    mins, rem_s = divmod(sec, 60)
    hrs, rem_m = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs} घंटा {rem_m} मिनट {rem_s} सेकंड"
    if mins > 0:
        return f"{mins} मिनट {rem_s} सेकंड"
    return f"{rem_s} सेकंड"


async def auto_delete_job(context: ContextTypes.DEFAULT_TYPE):
    jd = context.job.data
    for mid in jd.get("message_ids", []):
        try:
            await context.bot.delete_message(chat_id=jd["chat_id"], message_id=mid)
        except Exception:
            pass


def schedule_custom_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list[int], seconds: int):
    if message_ids and context.job_queue:
        context.job_queue.run_once(
            auto_delete_job,
            when=seconds,
            data={"chat_id": chat_id, "message_ids": message_ids},
        )


def duration_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱️ 5 मिनट", callback_data="dur_pick:300:5 मिनट"),
            InlineKeyboardButton("⏱️ 15 मिनट", callback_data="dur_pick:900:15 मिनट"),
        ],
        [
            InlineKeyboardButton("⏱️ 30 मिनट", callback_data="dur_pick:1800:30 मिनट"),
            InlineKeyboardButton("⏱️ 1 घंटा", callback_data="dur_pick:3600:1 घंटा"),
        ],
        [InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")],
    ])


# ================= SMART HTML MCQ PARSER (FOR UPLOADED HTML TESTS) =================
def strip_html_tags(raw: str) -> str:
    clean = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def parse_html_mcq_file(html_bytes: bytes) -> list[dict]:
    """
    किसी भी HTML क्विज़/टेस्ट फाइल (JS Array, JSON या HTML divs/lists) से
    सारे प्रश्न, 4 विकल्प (A,B,C,D), सही उत्तर और व्याख्या अपने आप निकाल लेता है।
    """
    raw = html_bytes.decode("utf-8", errors="ignore")
    questions = []

    # तरीका 1: JavaScript/JSON ऑब्जेक्ट पैटर्न (q/question, options/choices, ans/answer/correct)
    js_blocks = re.findall(
        r"\{[^{}]*?(?:q|question|title)\s*:\s*(['\"`])(.*?)\1[^{}]*?(?:options|choices)\s*:\s*\[(.*?)\][^{}]*?(?:ans|answer|correct|right)\s*:\s*(['\"`]?)(.*?)\4[^{}]*?\}",
        raw,
        flags=re.S | re.I,
    )
    for _, q_raw, opts_raw, _, ans_raw in js_blocks:
        q_txt = strip_html_tags(q_raw)
        opt_items = re.findall(r"(['\"`])(.*?)\1", opts_raw, flags=re.S)
        opts = [strip_html_tags(o[1]) for o in opt_items if strip_html_tags(o[1])]
        if not q_txt or len(opts) < 2:
            continue
        while len(opts) < 4:
            opts.append("उपरोक्त में से कोई नहीं")
        opts = opts[:4]

        ans_clean = strip_html_tags(ans_raw).strip()
        ans_idx = 0
        if ans_clean.isdigit():
            val = int(ans_clean)
            ans_idx = val if 0 <= val <= 3 else (val - 1 if 1 <= val <= 4 else 0)
        elif ans_clean.upper() in ("A", "B", "C", "D"):
            ans_idx = ["A", "B", "C", "D"].index(ans_clean.upper())
        else:
            for idx_o, o_txt in enumerate(opts):
                if ans_clean.lower() == o_txt.lower() or ans_clean.lower() in o_txt.lower():
                    ans_idx = idx_o
                    break
        questions.append({"q": q_txt, "options": opts, "ans": ans_idx, "exp": ""})

    if questions:
        return questions

    # तरीका 2: सामान्य HTML टेक्स्ट/लिस्ट पैटर्न (Q1. ... A) ... B) ... C) ... D) ... Ans: B)
    plain = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.S | re.I)
    plain = re.sub(r"<(br|p|div|li|h[1-6]|tr)[^>]*>", "\n", plain, flags=re.I)
    plain = html.unescape(re.sub(r"<[^>]+>", " ", plain))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in plain.splitlines() if ln.strip()]

    cur_q, cur_opts, cur_ans, cur_exp = "", [], None, ""
    for ln in lines:
        q_m = re.match(r"^(?:Q(?:uestion)?\s*\d*[\.\):\-]\s*|\d+[\.\)]\s+)(.+)", ln, flags=re.I)
        opt_m = re.match(r"^\(?([A-Da-d1-4])[\.\)\-:]\s*(.+)", ln)
        ans_m = re.match(r"^(?:Ans(?:wer)?|Correct(?:\s*Answer)?|उत्तर|सही\s*उत्तर)\s*[:\-]\s*(.+)", ln, flags=re.I)
        exp_m = re.match(r"^(?:Exp(?:lanation)?|Solution|व्याख्या|हल)\s*[:\-]\s*(.+)", ln, flags=re.I)

        if ans_m:
            val = ans_m.group(1).strip()
            letter_m = re.match(r"^\(?([A-Da-d])[\.\)\s]?", val)
            if letter_m:
                cur_ans = ["A", "B", "C", "D"].index(letter_m.group(1).upper())
            else:
                for idx_o, o_txt in enumerate(cur_opts):
                    if val.lower() in o_txt.lower():
                        cur_ans = idx_o
                        break
        elif exp_m:
            cur_exp = exp_m.group(1).strip()
        elif opt_m and cur_q and len(cur_opts) < 4:
            cur_opts.append(opt_m.group(2).strip())
        elif q_m:
            if cur_q and len(cur_opts) >= 2:
                while len(cur_opts) < 4:
                    cur_opts.append("इनमें से कोई नहीं")
                questions.append({
                    "q": cur_q,
                    "options": cur_opts[:4],
                    "ans": cur_ans if cur_ans is not None else 0,
                    "exp": cur_exp,
                })
            cur_q = q_m.group(1).strip()
            cur_opts, cur_ans, cur_exp = [], None, ""

    if cur_q and len(cur_opts) >= 2:
        while len(cur_opts) < 4:
            cur_opts.append("इनमें से कोई नहीं")
        questions.append({
            "q": cur_q,
            "options": cur_opts[:4],
            "ans": cur_ans if cur_ans is not None else 0,
            "exp": cur_exp,
        })

    return questions


# ================= OFFLINE 1-TO-1 MCQ GENERATOR (ZERO API NEEDED) =================
def build_formula_mcqs(formulas: list[tuple[str, str, str]]) -> list[dict]:
    if not formulas:
        return []
    mcqs = []
    all_exprs = [f[1] for f in formulas]
    all_names = [f[0] for f in formulas]

    for i, (fname, fexpr, funit) in enumerate(formulas):
        if i % 2 == 0 and len(set(all_exprs)) >= 4:
            q_text = f"'{fname}' का सही सूत्र / व्यंजक क्या है?"
            correct_val = fexpr
            pool = list({x for x in all_exprs if x != correct_val})
        else:
            q_text = f"सूत्र '{fexpr}' किसे व्यक्त करता है?"
            correct_val = fname
            pool = list({x for x in all_names if x != correct_val})

        distractors = random.sample(pool, min(3, len(pool)))
        while len(distractors) < 3:
            distractors.append(f"इनमें से कोई नहीं ({len(distractors)+1})")

        options = distractors + [correct_val]
        random.shuffle(options)
        ans_idx = options.index(correct_val)
        exp = f"{fname}: {fexpr} ({funit})" if funit else f"{fname}: {fexpr}"
        mcqs.append({"q": q_text, "options": options, "ans": ans_idx, "exp": exp})

    return mcqs


def build_lang_mcqs(item: dict, all_items: list[dict], is_hindi: bool) -> list[dict]:
    mcqs = []
    ch = item["ch"]

    def make_q(q_str: str, correct: str, field_key: str, fallback_pool: list[str]):
        if not correct or "उल्लेख नहीं" in correct or "Not specified" in correct or "अज्ञात" in correct:
            return
        pool = list({
            x[field_key]
            for x in all_items
            if x.get(field_key)
            and x[field_key] != correct
            and "उल्लेख नहीं" not in x[field_key]
            and "Not specified" not in x[field_key]
        })
        for fb in fallback_pool:
            if fb != correct and fb not in pool:
                pool.append(fb)
        distractors = random.sample(pool, min(3, len(pool)))
        options = distractors + [correct]
        random.shuffle(options)
        mcqs.append({"q": q_str, "options": options, "ans": options.index(correct), "exp": f"{ch} → {correct}"})

    if is_hindi:
        make_q(f"पाठ '{ch}' के लेखक / कवि कौन हैं?", item["author"], "author", ["प्रेमचंद", "महादेवी वर्मा", "हरिवंशराय बच्चन"])
        make_q(f"'{ch}' साहित्य की कौन-सी विधा है?", item["genre"], "genre", ["उपन्यास", "यात्रा-वृत्तांत", "जीवनी"])
        make_q(f"{item['author']} का जन्म कब हुआ था?", item["dob"], "dob", ["15 अगस्त 1905", "2 अक्टूबर 1880", "14 नवंबर 1915"])
        make_q(f"{item['author']} का निधन कब हुआ था?", item["dod"], "dod", ["10 मई 1960", "25 दिसंबर 1985", "18 मार्च 1952"])
        make_q(f"{item['author']} का जन्म स्थान कहाँ है?", item["birthplace"], "birthplace", ["पटना, बिहार", "भोपाल, मध्य प्रदेश", "अजमेर, राजस्थान"])
        make_q(f"{item['author']} की माता का नाम क्या था?", item["mother"], "mother", ["शारदा देवी", "कमला देवी", "सुमित्रा देवी"])
        make_q(f"{item['author']} के पिता का नाम क्या था?", item["father"], "father", ["रामनाथ शर्मा", "शिवप्रसाद सिंह", "गोपालदास"])
        make_q(f"निम्न में से कौन-सी प्रमुख रचनाएँ / सम्मान {item['author']} से संबंधित हैं?", item["works"], "works", ["गोदान, गबन, कफन", "यामा, नीहार, रश्मि", "मधुशाला, मधुकलश"])
    else:
        make_q(f"Who is the writer/poet of '{ch}'?", item["author"], "author", ["William Wordsworth", "Rabindranath Tagore", "R. K. Narayan"])
        make_q(f"What is the literary genre of '{ch}'?", item["genre"], "genre", ["Novel", "Biography", "Epic Poem"])
        make_q(f"When was {item['author']} born?", item["dob"], "dob", ["14 November 1889", "15 August 1901", "25 December 1875"])
        make_q(f"When did {item['author']} pass away?", item["dod"], "dod", ["12 April 1955", "19 July 1962", "5 September 1945"])
        make_q(f"Where was {item['author']} born?", item["birthplace"], "birthplace", ["Dublin, Ireland", "Kolkata, India", "Boston, USA"])
        make_q(f"What was the name of {item['author']}'s father?", item["father"], "father", ["Richard Williams", "Thomas Miller", "George Wilson"])
        make_q(f"What was the name of {item['author']}'s mother?", item["mother"], "mother", ["Mary Elizabeth", "Sarah Anne", "Margaret Rose"])
        make_q(f"Which works/honours belong to {item['author']}?", item["works"], "works", ["Gitanjali, The Home and the World", "Malgudi Days, The Guide", "Lyrical Ballads, The Prelude"])

    return mcqs
        # ================= COMPLETE PHYSICS FORMULAS (ALL 14 CHAPTERS) =================
PHYSICS_DATA = {
    "title": "⚛️ Physics (भौतिकी)",
    "chapters": [
        {
            "name": "1. वैद्युत आवेश तथा क्षेत्र",
            "formulas": [
                ("आवेश का क्वांटीकरण", "q = ±ne", "n = 1, 2, 3..., e = 1.6 × 10⁻¹⁹ C"),
                ("आवेश का SI मात्रक व विमा", "1 कूलॉम (C) = 1 A·s", "विमीय सूत्र: [AT]"),
                ("कूलॉम का नियम (निर्वात में)", "F = (1 / 4πε₀) · (q₁q₂ / r²)", "1/4πε₀ = 9 × 10⁹ N·m²/C²"),
                ("निर्वात की विद्युतशीलता (ε₀)", "ε₀ = 8.854 × 10⁻¹² C²/(N·m²)", "विमा: [M⁻¹L⁻³T⁴A²]"),
                ("परावैद्युतांक (आपेक्षिक विद्युतशीलता)", "εᵣ = K = ε / ε₀ = F_निर्वात / F_माध्यम", "विमाहीन राशि, K ≥ 1"),
                ("माध्यम में कूलॉम बल", "F_m = (1 / 4πε₀K) · (q₁q₂ / r²)", "SI मात्रक: न्यूटन (N)"),
                ("विद्युत क्षेत्र की तीव्रता", "E = F / q₀", "SI मात्रक: N/C या V/m, विमा: [MLT⁻³A⁻¹]"),
                ("बिंदु आवेश के कारण विद्युत क्षेत्र", "E = (1 / 4πε₀) · (q / r²)", "सदिश रूप: E⃗ = (kq / r³) r⃗"),
                ("रेखीय आवेश घनत्व (λ)", "λ = q / L", "SI मात्रक: C/m"),
                ("पृष्ठीय आवेश घनत्व (σ)", "σ = q / A", "SI मात्रक: C/m²"),
                ("आयतन आवेश घनत्व (ρ)", "ρ = q / V", "SI मात्रक: C/m³"),
                ("विद्युत द्विध्रुव आघूर्ण (p)", "p = q × 2l", "दिशा: -q से +q की ओर, मात्रक: C·m"),
                ("द्विध्रुव की अक्षीय स्थिति में विद्युत क्षेत्र", "E_axial = (1 / 4πε₀) · (2pr / (r² - l²)²)", "लघु द्विध्रुव (r >> l): E = 2kp / r³"),
                ("द्विध्रुव की निरक्षीय स्थिति में विद्युत क्षेत्र", "E_equatorial = (1 / 4πε₀) · (p / (r² + l²)^(3/2))", "लघु द्विध्रुव (r >> l): E = kp / r³"),
                ("अक्षीय व निरक्षीय क्षेत्र में अनुपात", "E_axial : E_equatorial = 2 : 1", "दिशाएँ परस्पर विपरीत"),
                ("एकसमान विद्युत क्षेत्र में द्विध्रुव पर बलाघूर्ण", "τ = pE sinθ  (τ⃗ = p⃗ × E⃗)", "अधिकतम τ_max = pE (θ = 90° पर)"),
                ("विद्युत फ्लक्स (Φ_E)", "Φ_E = E⃗ · A⃗ = EA cosθ", "SI मात्रक: N·m²/C या V·m"),
                ("गॉस का प्रमेय (Gauss's Law)", "Φ_E = ∮ E⃗ · dA⃗ = q_in / ε₀", "बंद पृष्ठ के भीतर कुल आवेश q_in"),
                ("अनंत लंबाई के सीधे आवेशित तार के कारण क्षेत्र", "E = λ / (2πε₀r)", "E ∝ 1/r"),
                ("अनंत समतल आवेशित अचालक चादर के कारण क्षेत्र", "E = σ / (2ε₀)", "दूरी r पर निर्भर नहीं करता"),
                ("आवेशित चालक पट्टिका के समीप विद्युत क्षेत्र", "E = σ / ε₀", "SI मात्रक: N/C"),
                ("आवेशित गोलीय कोश के बाहर विद्युत क्षेत्र (r > R)", "E = (1 / 4πε₀) · (q / r²)", "संपूर्ण आवेश केंद्र पर केंद्रित माना जाता है"),
                ("आवेशित गोलीय कोश के पृष्ठ पर विद्युत क्षेत्र (r = R)", "E = (1 / 4πε₀) · (q / R²) = σ / ε₀", "अधिकतम विद्युत क्षेत्र"),
                ("आवेशित गोलीय कोश के भीतर विद्युत क्षेत्र (r < R)", "E = 0", "क्योंकि भीतर आवेश q_in = 0"),
                ("अचालक ठोस गोले के भीतर विद्युत क्षेत्र (r < R)", "E = (1 / 4πε₀) · (qr / R³) = ρr / (3ε₀)", "E ∝ r"),
                ("विद्युत क्षेत्र में आवेशित कण का त्वरण", "a = qE / m", "SI मात्रक: m/s²"),
                ("दो समानांतर विपरीत आवेशित प्लेटों के बीच क्षेत्र", "E = σ / ε₀", "प्लेटों के बाहर E = 0"),
                ("दो समानांतर समान आवेशित प्लेटों के बीच क्षेत्र", "E = 0", "प्लेटों के बाहर E = σ / ε₀"),
            ],
        },
        {
            "name": "2. स्थिरवैद्युत विभव तथा धारिता",
            "formulas": [
                ("विद्युत विभव (Electric Potential)", "V = W / q₀", "SI मात्रक: वोल्ट (V) = J/C, विमा: [ML²T⁻³A⁻¹]"),
                ("विभवांतर (Potential Difference)", "V_B - V_A = W_AB / q₀", "अदिश राशि"),
                ("बिंदु आवेश के कारण विद्युत विभव", "V = (1 / 4πε₀) · (q / r)", "V ∝ 1/r"),
                ("द्विध्रुव की अक्षीय स्थिति में विभव", "V = (1 / 4πε₀) · (p / r²)", "V ∝ 1/r²"),
                ("द्विध्रुव की निरक्षीय स्थिति में विभव", "V = 0", "निरक्षीय तल समविभव पृष्ठ होता है"),
                ("द्विध्रुव के कारण किसी बिंदु (r, θ) पर विभव", "V = (1 / 4πε₀) · (p cosθ / r²)", "SI मात्रक: वोल्ट (V)"),
                ("विद्युत क्षेत्र व विभव प्रवणता में संबंध", "E = -dV / dr", "ऋणात्मक चिह्न: विभव घटने की दिशा में E"),
                ("दो आवेशों के निकाय की स्थितिज ऊर्जा", "U = (1 / 4πε₀) · (q₁q₂ / r)", "SI मात्रक: जूल (J)"),
                ("बाह्य क्षेत्र में द्विध्रुव को घुमाने में किया गया कार्य", "W = pE (cosθ₁ - cosθ₂)", "θ₁=0° से θ घुमाने पर: W = pE(1 - cosθ)"),
                ("विद्युत क्षेत्र में द्विध्रुव की स्थितिज ऊर्जा", "U = -pE cosθ = -p⃗ · E⃗", "स्थायी संतुलन (θ=0°): U_min = -pE"),
                ("गोलीय कोश के भीतर व पृष्ठ पर विभव (r ≤ R)", "V = (1 / 4πε₀) · (q / R)", "भीतर हर बिंदु पर विभव समान रहता है"),
                ("विद्युत धारिता (Capacitance)", "C = Q / V", "SI मात्रक: फैरड (F), विमा: [M⁻¹L⁻²T⁴A²]"),
                ("विलगित गोलीय चालक की धारिता", "C = 4πε₀R", "पृथ्वी की धारिता ≈ 711 μF"),
                ("समांतर पट्ट संधारित्र की धारिता (निर्वात)", "C₀ = ε₀A / d", "A = प्लेट क्षेत्रफल, d = प्लेटों के बीच दूरी"),
                ("परावैद्युत माध्यम से पूर्णतः भरे संधारित्र की धारिता", "C = Kε₀A / d = K·C₀", "K = परावैद्युतांक"),
                ("t मोटाई की परावैद्युत पट्टी वाले संधारित्र की धारिता", "C = ε₀A / (d - t + t/K)", "धातु की पट्टी (K=∞) के लिए: C = ε₀A / (d - t)"),
                ("गोलीय संधारित्र की धारिता", "C = 4πε₀ · (ab / (b - a))", "a = आंतरिक त्रिज्या, b = बाह्य त्रिज्या"),
                ("बेलनाकार संधारित्र की धारिता", "C = 2πε₀L / ln(b / a)", "L = बेलन की लंबाई"),
                ("संधारित्रों का श्रेणीक्रम संयोजन", "1/C_s = 1/C₁ + 1/C₂ + 1/C₃", "सभी पर आवेश Q समान रहता है"),
                ("संधारित्रों का समानांतर क्रम संयोजन", "C_p = C₁ + C₂ + C₃", "सभी पर विभवांतर V समान रहता है"),
                ("n समान संधारित्रों का अनुपात", "C_p / C_s = n²", "C_p = nC, C_s = C/n"),
                ("आवेशित संधारित्र में संचित स्थितिज ऊर्जा", "U = ½CV² = Q² / (2C) = ½QV", "SI मात्रक: जूल (J)"),
                ("संधारित्र का ऊर्जा घनत्व (Energy Density)", "u = ½ε₀E²", "परावैद्युत में: u = ½Kε₀E² (मात्रक: J/m³)"),
                ("दो आवेशित चालकों को जोड़ने पर उभयनिष्ठ विभव", "V = (C₁V₁ + C₂V₂) / (C₁ + C₂)", "कुल आवेश संरक्षित रहता है"),
                ("आवेश पुनर्वितरण में ऊर्जा ह्रास (Loss of Energy)", "ΔU = ½ · [C₁C₂ / (C₁ + C₂)] · (V₁ - V₂)²", "ऊष्मा के रूप में ऊर्जा क्षय"),
                ("परावैद्युत सामर्थ्य (Dielectric Strength)", "E_max = V_breakdown / d", "वायु के लिए ≈ 3 × 10⁶ V/m"),
            ],
        },
        {
            "name": "3. विद्युत धारा",
            "formulas": [
                ("विद्युत धारा (Electric Current)", "I = q / t = ne / t", "SI मात्रक: ऐम्पियर (A), अदिश राशि"),
                ("धारा घनत्व (Current Density)", "J = I / A  (I = J⃗ · A⃗)", "सदिश राशि, SI मात्रक: A/m²"),
                ("अपवाह वेग / अनुगमन वेग (Drift Velocity)", "v_d = (eE / m) · τ", "τ = श्रांतिकाल (Relaxation Time)"),
                ("धारा व अपवाह वेग में संबंध", "I = n e A v_d", "n = मुक्त इलेक्ट्रॉन घनत्व"),
                ("गतिशीलता (Mobility)", "μ = v_d / E = eτ / m", "SI मात्रक: m²/(V·s)"),
                ("ओम का नियम (Ohm's Law)", "V = I R", "R = प्रतिरोध, SI मात्रक: ओम (Ω)"),
                ("ओम के नियम का सदिश रूप", "J⃗ = σ E⃗", "σ = विद्युत चालकता"),
                ("प्रतिरोध (Resistance) की निर्भरता", "R = ρ (L / A)", "ρ = विशिष्ट प्रतिरोध / प्रतिरोधकता"),
                ("प्रतिरोधकता (Resistivity) सूत्र", "ρ = m / (n e² τ)", "SI मात्रक: Ω·m, केवल पदार्थ व ताप पर निर्भर"),
                ("विद्युत चालकता (Conductivity)", "σ = 1 / ρ = n e² τ / m", "SI मात्रक: Ω⁻¹m⁻¹ या S/m"),
                ("तार को खींचकर n गुना लंबा करने पर नया प्रतिरोध", "R' = n² R", "आयतन A·L नियत रहने पर"),
                ("ताप के साथ प्रतिरोध में परिवर्तन", "R_t = R₀ (1 + α Δt)", "α = प्रतिरोध ताप गुणांक (मात्रक: °C⁻¹ या K⁻¹)"),
                ("प्रतिरोधों का श्रेणीक्रम संयोजन", "R_s = R₁ + R₂ + R₃", "धारा I समान रहती है"),
                ("प्रतिरोधों का समानांतर क्रम संयोजन", "1/R_p = 1/R₁ + 1/R₂ + 1/R₃", "विभवांतर V समान रहता है"),
                ("सेल का विद्युत वाहक बल (EMF)", "E = W / q", "SI मात्रक: वोल्ट (V)"),
                ("सेल का टर्मिनल विभवांतर (डिस्चार्जिंग)", "V = E - Ir", "r = सेल का आंतरिक प्रतिरोध"),
                ("सेल को चार्ज करते समय टर्मिनल विभवांतर", "V = E + Ir", "V > E होता है"),
                ("सेल का आंतरिक प्रतिरोध (r)", "r = R ((E / V) - 1)", "SI मात्रक: ओम (Ω)"),
                ("परिपथ में कुल धारा", "I = E / (R + r)", "अधिकतम धारा: I_max = E / r (जब R = 0)"),
                ("n सेलों का श्रेणीक्रम संयोजन", "I = nE / (R + nr)", "जब R >> nr तो लाभदायक"),
                ("m सेलों का समानांतर क्रम संयोजन", "I = mE / (mR + r)", "जब r >> R तो लाभदायक"),
                ("मिश्रित क्रम में अधिकतम धारा की शर्त", "R = nr / m", "I_max = mnE / (2nr)"),
                ("किरचॉफ का प्रथम नियम (KCL - संधि नियम)", "ΣI = 0", "आवेश संरक्षण सिद्धांत पर आधारित"),
                ("किरचॉफ का द्वितीय नियम (KVL - पाश नियम)", "ΣΔV = 0  (ΣE = ΣIR)", "ऊर्जा संरक्षण सिद्धांत पर आधारित"),
                ("व्हीटस्टोन सेतु के संतुलन की शर्त", "P / Q = R / S", "गैल्वेनोमीटर में धारा I_g = 0"),
                ("मीटर सेतु (Meter Bridge) अज्ञात प्रतिरोध", "S = R · ((100 - l) / l)", "l = संतुलन लंबाई (cm में)"),
                ("विभवमापी का सिद्धांत (Potentiometer)", "V = k · l", "k = V/L = विभव प्रवणता (V/m)"),
                ("विभवमापी से दो सेलों के EMF की तुलना", "E₁ / E₂ = l₁ / l₂", "l₁, l₂ संतुलन लंबाइयाँ"),
                ("विभवमापी से सेल का आंतरिक प्रतिरोध", "r = R ((l₁ - l₂) / l₂)", "SI मात्रक: ओम (Ω)"),
                ("जूल का ऊष्मीय नियम (विद्युत ऊर्जा)", "H = I²Rt = VIt = (V² / R)t", "कैलोरी में: H = I²Rt / 4.2 cal"),
                ("विद्युत शक्ति (Electric Power)", "P = VI = I²R = V² / R", "SI मात्रक: वाट (W), 1 HP = 746 W, 1 kWh = 3.6 × 10⁶ J"),
            ],
        },
        {
            "name": "4. गतिमान आवेश और चुंबकत्व",
            "formulas": [
                ("चुंबकीय बल (गतिमान आवेश पर)", "F⃗ = q (v⃗ × B⃗)  ⇒  F = qvB sinθ", "B का SI मात्रक: टेस्ला (T) = Wb/m² = 10⁴ गॉस"),
                ("लॉरेंज बल (विद्युत + चुंबकीय बल)", "F⃗ = q [E⃗ + (v⃗ × B⃗)]", "वेग चयनक: v = E / B"),
                ("चुंबकीय क्षेत्र में वृत्तीय पथ की त्रिज्या", "r = mv / (qB) = √(2mK) / (qB)", "K = गतिज ऊर्जा"),
                ("आवर्तकाल (Time Period)", "T = 2πm / (qB)", "वेग v व त्रिज्या r पर निर्भर नहीं"),
                ("साइक्लोट्रॉन आवृत्ति (Cyclotron Frequency)", "ν = qB / (2πm)", "कोणीय आवृत्ति: ω = qB / m"),
                ("साइक्लोट्रॉन अधिकतम गतिज ऊर्जा", "K_max = q²B²R² / (2m)", "R = डीज़ की अधिकतम त्रिज्या"),
                ("धारावाही चालक पर चुंबकीय बल", "F⃗ = I (L⃗ × B⃗)  ⇒  F = BIL sinθ", "SI मात्रक: न्यूटन (N)"),
                ("बायो-सावार्ट का नियम (Biot-Savart Law)", "dB = (μ₀ / 4π) · (I dl sinθ / r²)", "μ₀/4π = 10⁻⁷ T·m/A, μ₀ = 4π × 10⁻⁷ T·m/A"),
                ("निर्वात की चुंबकशीलता व विद्युतशीलता में संबंध", "c = 1 / √(μ₀ε₀)", "c = 3 × 10⁸ m/s"),
                ("सीधे अनंत धारावाही तार के कारण चुंबकीय क्षेत्र", "B = μ₀I / (2πr)", "B ∝ 1/r"),
                ("परिमित लंबाई के सीधे तार के कारण क्षेत्र", "B = (μ₀I / 4πr) (sinφ₁ + sinφ₂)", "फ्लेमिंग दाएँ हाथ अंगुष्ठ नियम"),
                ("वृत्ताकार धारावाही कुंडली के केंद्र पर क्षेत्र", "B = μ₀NI / (2R)", "N = फेरों की संख्या, R = त्रिज्या"),
                ("वृत्ताकार कुंडली की अक्ष पर चुंबकीय क्षेत्र", "B = μ₀NIR² / [2(R² + x²)^(3/2)]", "केंद्र पर x = 0 रखने पर B = μ₀NI/(2R)"),
                ("ऐम्पियर का परिपथीय नियम (Ampere's Law)", "∮ B⃗ · dl⃗ = μ₀ I_enclosed", "बंद पथ के लिए"),
                ("लंबी परिनालिका (Solenoid) के भीतर क्षेत्र", "B = μ₀ n I", "n = N/L (प्रति एकांक लंबाई में फेरों की संख्या)"),
                ("परिनालिका के सिरों पर चुंबकीय क्षेत्र", "B_end = ½ μ₀ n I", "केंद्र का आधा"),
                ("टोरॉइड (Toroid) के क्रोड में चुंबकीय क्षेत्र", "B = μ₀ n I = μ₀NI / (2πr)", "बाहर व केंद्र के रिक्त स्थान में B = 0"),
                ("दो समानांतर धारावाही तारों के बीच प्रति एकांक लंबाई बल", "F / L = (μ₀ I₁ I₂) / (2πd)", "समान दिशा में आकर्षण, विपरीत में प्रतिकर्षण"),
                ("धारावाही लूप का चुंबकीय द्विध्रुव आघूर्ण", "M = N I A", "SI मात्रक: A·m² या J/T"),
                ("चुंबकीय क्षेत्र में धारावाही कुंडली पर बलाघूर्ण", "τ = N I A B sinθ  (τ⃗ = M⃗ × B⃗)", "θ = अभिलंब व B के बीच कोण"),
                ("चल कुंडली गैल्वेनोमीटर का सिद्धांत", "I = (k / NAB) θ = G · θ", "G = गैल्वेनोमीटर नियतांक"),
                ("धारा सुग्राहिता (Current Sensitivity)", "I_s = θ / I = NAB / k", "मात्रक: rad/A या div/A"),
                ("वोल्टेज सुग्राहिता (Voltage Sensitivity)", "V_s = θ / V = NAB / (kR)", "मात्रक: rad/V"),
                ("गैल्वेनोमीटर को अमीटर में बदलना (Shunt)", "S = (I_g · G) / (I - I_g)", "शंट S को समानांतर क्रम में जोड़ते हैं"),
                ("गैल्वेनोमीटर को वोल्टमीटर में बदलना", "R = (V / I_g) - G", "उच्च प्रतिरोध R को श्रेणीक्रम में जोड़ते हैं"),
            ],
        },
        {
            "name": "5. चुंबकत्व एवं द्रव्य",
            "formulas": [
                ("चुंबकीय द्विध्रुव आघूर्ण (छड़ चुंबक)", "M = m × 2l", "m = ध्रुव प्राबल्य (A·m), 2l = प्रभावकारी लंबाई"),
                ("चुंबकीय क्षेत्र का गॉस नियम", "∮ B⃗ · dA⃗ = 0", "एकल चुंबकीय ध्रुव का अस्तित्व नहीं है"),
                ("छड़ चुंबक की अक्षीय स्थिति में क्षेत्र", "B_axial = (μ₀ / 4π) · (2M / r³)", "लघु चुंबक के लिए"),
                ("छड़ चुंबक की निरक्षीय स्थिति में क्षेत्र", "B_equatorial = (μ₀ / 4π) · (M / r³)", "B_axial = 2 B_equatorial"),
                ("चुंबक को घुमाने में किया गया कार्य", "W = MB (cosθ₁ - cosθ₂)", "0° से θ घुमाने पर: W = MB(1 - cosθ)"),
                ("चुंबक की स्थितिज ऊर्जा", "U = -MB cosθ = -M⃗ · B⃗", "SI मात्रक: जूल (J)"),
                ("चुंबकीय क्षेत्र में चुंबक के दोलन का आवर्तकाल", "T = 2π √(I / MB)", "I = जड़त्व आघूर्ण"),
                ("पृथ्वी के चुंबकीय क्षेत्र का क्षैतिज घटक (B_H)", "B_H = B cosδ", "δ = नमन कोण (Angle of Dip)"),
                ("पृथ्वी के चुंबकीय क्षेत्र का ऊर्ध्वाधर घटक (B_V)", "B_V = B sinδ", "भूमध्य रेखा पर δ = 0°, ध्रुवों पर δ = 90°"),
                ("नमन कोण (δ) व घटकों में संबंध", "tanδ = B_V / B_H", "कुल क्षेत्र: B = √(B_H² + B_V²)"),
                ("चुंबकन तीव्रता (Intensity of Magnetisation)", "I = M_net / V", "SI मात्रक: A/m"),
                ("चुंबकीय तीव्रता (Magnetic Intensity H)", "H = B₀ / μ₀", "SI मात्रक: A/m"),
                ("चुंबकीय प्रवृत्ति (Magnetic Susceptibility χ)", "χ = I / H", "विमाहीन राशि"),
                ("आपेक्षिक चुंबकशीलता (μᵣ) व χ में संबंध", "μᵣ = 1 + χ", "μ = μ₀(1 + χ)"),
                ("कुल चुंबकीय क्षेत्र (B)", "B = μ₀ (H + I) = μ₀μᵣ H", "SI मात्रक: टेस्ला (T)"),
                ("क्यूरी का नियम (Curie's Law)", "χ = C / T  ⇒  I ∝ B / T", "अनुचुंबकीय पदार्थों के लिए (C = क्यूरी नियतांक)"),
                ("क्यूरी-वाइस नियम (Curie-Weiss Law)", "χ = C / (T - T_c)", "लौहचुंबकीय पदार्थों के लिए (T > T_c)"),
                ("चुंबकीय पदार्थों की प्रवृत्ति (χ)", "प्रतिचुंबकीय: χ < 0 | अनुचुंबकीय: χ > 0 (अल्प) | लौहचुंबकीय: χ >> 1", "महत्वपूर्ण बोर्ड तथ्य"),
            ],
        },
        {
            "name": "6. वैद्युतचुंबकीय प्रेरण",
            "formulas": [
                ("चुंबकीय फ्लक्स (Magnetic Flux Φ_B)", "Φ_B = B⃗ · A⃗ = BA cosθ", "SI मात्रक: वेबर (Wb) = T·m², 1 Wb = 10⁸ मैक्सवेल"),
                ("फैराडे का विद्युतचुंबकीय प्रेरण नियम", "e = -N (dΦ_B / dt)", "ऋणात्मक चिह्न लेंज के नियम को दर्शाता है"),
                ("प्रेरित धारा (Induced Current)", "I = e / R = -(N / R) (dΦ_B / dt)", "SI मात्रक: ऐम्पियर (A)"),
                ("प्रेरित आवेश (Induced Charge)", "q = -N ΔΦ_B / R", "समय Δt पर निर्भर नहीं करता"),
                ("गतिक विद्युत वाहक बल (Motional EMF)", "e = B l v", "जब B, l, v परस्पर लंबवत हों"),
                ("चुंबकीय क्षेत्र में घूर्णन करती छड़ में प्रेरित EMF", "e = ½ B ω L² = π B f L²", "ω = कोणीय वेग, L = छड़ की लंबाई"),
                ("कुंडली के घूर्णन से उत्पन्न प्रत्यावर्ती EMF", "e = N B A ω sin(ωt) = e₀ sin(ωt)", "e₀ = NBAω (शिखर मान)"),
                ("स्वप्रेरण गुणांक / स्वप्रेरकत्व (Self Inductance L)", "Φ = L I  ⇒  e = -L (dI / dt)", "SI मात्रक: हेनरी (H) = Wb/A, विमा: [ML²T⁻²A⁻²]"),
                ("लंबी परिनालिका का स्वप्रेरकत्व", "L = μ₀ N² A / l = μ₀ n² A l", "क्रोड में μᵣ रखने पर L = μ₀μᵣN²A/l"),
                ("समतल वृत्ताकार कुंडली का स्वप्रेरकत्व", "L = μ₀ π N² R / 2", "R = कुंडली की त्रिज्या"),
                ("अन्योन्य प्रेरण गुणांक (Mutual Inductance M)", "Φ₂ = M I₁  ⇒  e₂ = -M (dI₁ / dt)", "SI मात्रक: हेनरी (H)"),
                ("दो समाक्षीय परिनालिकाओं का अन्योन्य प्रेरकत्व", "M = μ₀ N₁ N₂ A / l", "परस्पर निर्भरता बराबर (M₁₂ = M₂₁)"),
                ("युग्मन गुणांक (Coupling Coefficient K)", "M = K √(L₁ L₂)", "0 ≤ K ≤ 1"),
                ("श्रेणीक्रम में प्रेरकत्व संयोजन (बिना अन्योन्य प्रेरण)", "L_eq = L₁ + L₂", "अन्योन्य प्रेरण सहित: L = L₁ + L₂ ± 2M"),
                ("समानांतर क्रम में प्रेरकत्व संयोजन", "1 / L_eq = 1 / L₁ + 1 / L₂", "L_eq = L₁L₂ / (L₁ + L₂)"),
                ("प्रेरक (Inductor) में संचित चुंबकीय ऊर्जा", "U = ½ L I²", "SI मात्रक: जूल (J)"),
                ("चुंबकीय ऊर्जा घनत्व", "u_B = B² / (2μ₀)", "SI मात्रक: J/m³"),
            ],
        },
        {
            "name": "7. प्रत्यावर्ती धारा",
            "formulas": [
                ("प्रत्यावर्ती वोल्टता व धारा का तात्क्षणिक मान", "V = V₀ sin(ωt),  I = I₀ sin(ωt ± φ)", "ω = 2πf = 2π/T (भारत में f = 50 Hz)"),
                ("अर्धचक्र के लिए औसत मान (Mean Value)", "I_avg = 2I₀ / π ≈ 0.637 I₀", "पूर्ण चक्र के लिए औसत मान = 0"),
                ("वर्ग माध्य मूल मान (RMS Value)", "I_rms = I₀ / √2 ≈ 0.707 I₀", "V_rms = V₀ / √2 ≈ 0.707 V₀"),
                ("शिखर मान व RMS मान का अनुपात", "I₀ / I_rms = √2 ≈ 1.414", "फैक्टर = √2"),
                ("शुद्ध प्रतिरोधी (R) परिपथ", "I₀ = V₀ / R,  कलांतर φ = 0°", "V और I समान कला में"),
                ("प्रेरणिक प्रतिघात (Inductive Reactance X_L)", "X_L = ωL = 2πfL", "SI मात्रक: ओम (Ω), DC (f=0) के लिए X_L = 0"),
                ("शुद्ध प्रेरकीय (L) परिपथ में कलांतर", "वोल्टता V धारा I से 90° (π/2) आगे", "औसत शक्ति क्षय P = 0"),
                ("धारितीय प्रतिघात (Capacitive Reactance X_C)", "X_C = 1 / (ωC) = 1 / (2πfC)", "SI मात्रक: ओम (Ω), DC (f=0) के लिए X_C = ∞"),
                ("शुद्ध धारितीय (C) परिपथ में कलांतर", "धारा I वोल्टता V से 90° (π/2) आगे", "औसत शक्ति क्षय P = 0"),
                ("श्रेणी LCR परिपथ की प्रतिबाधा (Impedance Z)", "Z = √[R² + (X_L - X_C)²]", "SI मात्रक: ओम (Ω)"),
                ("श्रेणी LCR परिपथ में परिणामी वोल्टता", "V = √[V_R² + (V_L - V_C)²]", "V_R, V_L, V_C सदिश योग"),
                ("श्रेणी LCR परिपथ में कलांतर (φ)", "tanφ = (X_L - X_C) / R = (V_L - V_C) / V_R", "cosφ = R / Z"),
                ("विद्युत अनुनाद की शर्त (Resonance)", "X_L = X_C  ⇒  Z_min = R", "धारा अधिकतम: I_max = V / R"),
                ("अनुनादी आवृत्ति (Resonant Frequency)", "f₀ = 1 / [2π √(LC)]", "कोणीय आवृत्ति: ω₀ = 1 / √(LC)"),
                ("गुणता कारक (Quality Factor Q)", "Q = (1 / R) √(L / C) = ω₀L / R = f₀ / Δf", "विमाहीन राशि, तीक्ष्णता का माप"),
                ("AC परिपथ में औसत शक्ति (Average Power)", "P_avg = V_rms · I_rms · cosφ", "SI मात्रक: वाट (W)"),
                ("शक्ति गुणांक (Power Factor)", "cosφ = R / Z", "शुद्ध R में cosφ = 1; शुद्ध L या C में cosφ = 0"),
                ("वाटहीन धारा (Wattless Current)", "I_wattless = I_rms sinφ", "कोई शक्ति व्यय नहीं करती"),
                ("LC दोलन की आवृत्ति", "f = 1 / [2π √(LC)]", "कुल ऊर्जा: U = q₀²/(2C) = ½LI₀²"),
                ("ट्रांसफॉर्मर का परिणमन अनुपात (Transformation Ratio)", "V_s / V_p = N_s / N_p = I_p / I_s = K", "उच्चायी: K > 1, अपचायी: K < 1"),
                ("ट्रांसफॉर्मर की दक्षता (Efficiency η)", "η = (निर्गत शक्ति / निवेशी शक्ति) × 100% = (V_s I_s / V_p I_p) × 100%", "आदर्श ट्रांसफॉर्मर के लिए η = 100%"),
            ],
        },
        {
            "name": "8. वैद्युतचुंबकीय तरंगें",
            "formulas": [
                ("विस्थापन धारा (Displacement Current I_d)", "I_d = ε₀ (dΦ_E / dt)", "SI मात्रक: ऐम्पियर (A)"),
                ("ऐम्पियर-मैक्सवेल नियम", "∮ B⃗ · dl⃗ =                ("ऐम्पियर-मैक्सवेल नियम", "∮ B⃗ · dl⃗ = μ₀ (I_c + ε₀ dΦ_E/dt)", "I_c = चालन धारा"),
                ("निर्वात में विद्युतचुंबकीय तरंग की चाल", "c = 1 / √(μ₀ε₀) = 3 × 10⁸ m/s", "सभी EM तरंगों की निर्वात में चाल समान"),
                ("माध्यम में विद्युतचुंबकीय तरंग की चाल", "v = 1 / √(με) = c / √(μᵣεᵣ) = c / n", "n = माध्यम का अपवर्तनांक"),
                ("विद्युत व चुंबकीय क्षेत्र के आयामों में संबंध", "E₀ / B₀ = c", "E⃗ ⊥ B⃗ ⊥ तरंग संचरण की दिशा (E⃗ × B⃗)"),
                ("तरंग समीकरण (E और B)", "E = E₀ sin(kx - ωt),  B = B₀ sin(kx - ωt)", "k = 2π/λ, ω = 2πf"),
                ("विद्युत क्षेत्र का औसत ऊर्जा घनत्व", "u_E = ¼ ε₀ E₀² = ½ ε₀ E_rms²", "SI मात्रक: J/m³"),
                ("चुंबकीय क्षेत्र का औसत ऊर्जा घनत्व", "u_B = B₀² / (4μ₀) = B_rms² / (2μ₀)", "u_E = u_B (दोनों बराबर होते हैं)"),
                ("कुल औसत ऊर्जा घनत्व (u)", "u = u_E + u_B = ½ ε₀ E₀² = B₀² / (2μ₀)", "SI मात्रक: J/m³"),
                ("तरंग की तीव्रता (Intensity I)", "I = u · c = ½ ε₀ c E₀²", "SI मात्रक: W/m²"),
                ("विकिरण संवेग (Momentum p)", "p = U / c", "पूर्ण अवशोषण पर; परावर्तन पर p = 2U / c"),
                ("विकिरण दाब (Radiation Pressure)", "P_rad = I / c", "परावर्तक पृष्ठ के लिए: P_rad = 2I / c"),
                ("पॉइंटिंग सदिश (Poynting Vector S⃗)", "S⃗ = (1 / μ₀) (E⃗ × B⃗)", "ऊर्जा प्रवाह की दिशा व दर बताता है"),
                ("EM स्पेक्ट्रम (बढ़ती आवृत्ति / घटती तरंगदैर्ध्य क्रम)", "रेडियो < सूक्ष्म < अवरक्त < दृश्य < पराबैंगनी < X-किरणें < गामा किरणें", "गामा किरणों की भेदन क्षमता सर्वाधिक"),
            ],
        },
        {
            "name": "9. किरण प्रकाशिकी एवं प्रकाशिक यंत्र",
            "formulas": [
                ("गोलीय दर्पण की फोकस दूरी व वक्रता त्रिज्या", "f = R / 2", "अवतल दर्पण: f (-), उत्तल दर्पण: f (+)"),
                ("दर्पण सूत्र (Mirror Formula)", "1 / f = 1 / v + 1 / u", "u = वस्तु दूरी, v = प्रतिबिंब दूरी"),
                ("दर्पण का रेखीय आवर्धन (Magnification m)", "m = h_i / h_o = -v / u = f / (f - u)", "वास्तविक: m (-), आभासी: m (+)"),
                ("निरपेक्ष अपवर्तनांक (Refractive Index n)", "n = c / v = λ_निर्वात / λ_माध्यम", "विमाहीन राशि, काँच n ≈ 1.5, जल n ≈ 1.33"),
                ("स्नेल का अपवर्तन नियम (Snell's Law)", "n₁ sin i = n₂ sin r  ⇒  sin i / sin r = ₁n₂", "आवृत्ति अपरिवर्तित रहती है"),
                ("वास्तविक व आभासी गहराई में संबंध", "n = वास्तविक गहराई (d) / आभासी गहराई (d')", "आभासी विस्थापन: Δt = t (1 - 1/n)"),
                ("क्रांतिक कोण (Critical Angle i_c)", "sin i_c = 1 / n  (सघन से विरल में)", "हीरे का n = 2.42, i_c ≈ 24.4°"),
                ("गोलीय पृष्ठ पर अपवर्तन सूत्र", "(n₂ / v) - (n₁ / u) = (n₂ - n₁) / R", "उत्तल व अवतल दोनों पृष्ठों के लिए"),
                ("लेंस मेकर सूत्र (Lens Maker's Formula)", "1 / f = (n₂/n₁ - 1) (1/R₁ - 1/R₂)", "वायु में: 1/f = (n - 1)(1/R₁ - 1/R₂)"),
                ("पतले लेंस का सूत्र (Lens Formula)", "1 / f = 1 / v - 1 / u", "उत्तल लेंस: f (+), अवतल लेंस: f (-)"),
                ("लेंस का रेखीय आवर्धन", "m = h_i / h_o = v / u = f / (f + u)", "विमाहीन राशि"),
                ("लेंस की क्षमता (Power of Lens P)", "P = 1 / f (मीटर में) = 100 / f (cm में)", "SI मात्रक: डायोप्टर (D)"),
                ("संपर्क में रखे दो पतले लेंसों की संयुक्त फोकस दूरी", "1 / F = 1 / f₁ + 1 / f₂  ⇒  P = P₁ + P₂", "d दूरी पर रखने पर: 1/F = 1/f₁ + 1/f₂ - d/(f₁f₂)"),
                ("द्रव में डुबोने पर लेंस की फोकस दूरी", "f_l / f_a = (n_g - 1) / (n_g / n_l - 1)", "यदि n_l > n_g तो लेंस की प्रकृति बदल जाती है"),
                ("प्रिज्म कोण (A) व अपवर्तन कोणों में संबंध", "A = r₁ + r₂", "प्रिज्म के भीतर"),
                ("प्रिज्म द्वारा उत्पन्न कुल विचलन कोण (δ)", "δ = i + e - A", "e = निर्गत कोण"),
                ("न्यूनतम विचलन (δ_m) की शर्त", "i = e  तथा  r₁ = r₂ = A / 2", "i = (A + δ_m) / 2"),
                ("प्रिज्म का अपवर्तनांक सूत्र (Prism Formula)", "n = sin[(A + δ_m) / 2] / sin(A / 2)", "पतले प्रिज्म के लिए: δ = (n - 1)A"),
                ("कोणीय विक्षेपण (Angular Dispersion θ)", "θ = δ_v - δ_r = (n_v - n_r) A", "बैंगनी का विचलन सर्वाधिक"),
                ("वर्ण विक्षेपण क्षमता (Dispersive Power ω)", "ω = (δ_v - δ_r) / δ_y = (n_v - n_r) / (n_y - 1)", "केवल प्रिज्म के पदार्थ पर निर्भर"),
                ("रैले का प्रकीर्णन नियम (Rayleigh Scattering)", "प्रकीर्णित तीव्रता I ∝ 1 / λ⁴", "नीले रंग का प्रकीर्णन लाल से ~16 गुना अधिक"),
                ("सरल सूक्ष्मदर्शी की आवर्धन क्षमता", "स्पष्ट दृष्टि (D): m = 1 + D/f | अनंत पर: m = D/f", "D = 25 cm"),
                ("संयुक्त सूक्ष्मदर्शी (Compound Microscope) आवर्धन", "m = (v_o / u_o)(1 + D/f_e) | अनंत पर: m = (L / f_o)(D / f_e)", "नली की लंबाई L = v_o + u_e"),
                ("खगोलीय दूरदर्शी (Astronomical Telescope) आवर्धन", "स्पष्ट दृष्टि: m = (f_o / f_e)(1 + f_e/D) | अनंत पर: m = f_o / f_e", "अनंत पर नली लंबाई L = f_o + f_e"),
            ],
        },
        {
            "name": "10. तरंग-प्रकाशिकी",
            "formulas": [
                ("कलांतर (Δφ) व पथांतर (Δx) में संबंध", "Δφ = (2π / λ) · Δx", "1 λ पथांतर = 2π कलांतर"),
                ("परिणामी तरंग की तीव्रता (व्यतिकरण)", "I = I₁ + I₂ + 2√(I₁I₂) cosφ", "यदि I₁ = I₂ = I₀ तो I = 4I₀ cos²(φ/2)"),
                ("संपोषी व्यतिकरण (Constructive Interference)", "Δφ = 2nπ  या  Δx = nλ  (n = 0, 1, 2...)", "I_max = (√I₁ + √I₂)² = (a₁ + a₂)²"),
                ("विनाशी व्यतिकरण (Destructive Interference)", "Δφ = (2n+1)π  या  Δx = (2n+1)λ/2", "I_min = (√I₁ - √I₂)² = (a₁ - a₂)²"),
                ("अधिकतम व न्यूनतम तीव्रता का अनुपात", "I_max / I_min = [(a₁ + a₂) / (a₁ - a₂)]² = [(r + 1)/(r - 1)]²", "यहाँ r = a₁ / a₂ = √(I₁ / I₂)"),
                ("यंग द्वि-झिरी प्रयोग (YDSE) में पथांतर", "Δx = y d / D = d sinθ", "d = झिरियों की दूरी, D = पर्दे की दूरी"),
                ("n-वीं दीप्त फ्रिंज (Bright Fringe) की स्थिति", "y_n = n λ D / d", "केंद्रीय फ्रिंज (n=0) सदैव दीप्त होती है"),
                ("n-वीं अदीप्त फ्रिंज (Dark Fringe) की स्थिति", "y'_n = (2n - 1) λ D / (2d)", "प्रथम अदीप्त फ्रिंज: y'₁ = λD / (2d)"),
                ("फ्रिंज चौड़ाई (Fringe Width β)", "β = λ D / d", "दीप्त व अदीप्त दोनों फ्रिंजों की चौड़ाई समान"),
                ("कोणीय फ्रिंज चौड़ाई (θ)", "θ = β / D = λ / d", "मात्रक: रेडियन"),
                ("माध्यम (अपवर्तनांक n) में फ्रिंज चौड़ाई", "β' = β / n", "फ्रिंज चौड़ाई घट जाती है"),
                ("t मोटाई की पारदर्शी पट्टी रखने पर फ्रिंज विस्थापन", "Δy = (D / d)(n - 1)t = (β / λ)(n - 1)t", "जिधर पट्टी रखी जाती है उधर फ्रिंज खिसकती है"),
                ("एकल झिरी विवर्तन (Diffraction) में निम्निष्ठ की शर्त", "a sinθ = nλ  (n = 1, 2, 3...)", "a = झिरी की चौड़ाई"),
                ("केंद्रीय उच्चिष्ठ (Central Maximum) की चौड़ाई", "रेखीय चौड़ाई = 2λD / a,  कोणीय चौड़ाई = 2λ / a", "अन्य द्वितीयक उच्चिष्ठों की दोगुनी"),
                ("सूक्ष्मदर्शी की विभेदन सीमा व विभेदन क्षमता", "d_min = 1.22λ / (2n sinθ),  RP = 1 / d_min", "n sinθ = संख्यात्मक द्वारक (NA)"),
                ("दूरदर्शी की विभेदन क्षमता", "RP = D / (1.22 λ)", "D = अभिदृश्यक का व्यास"),
                ("ब्रूस्टर का ध्रुवण नियम (Brewster's Law)", "n = tan i_p", "i_p + r = 90° (परावर्तित व अपवर्तित किरणें लंबवत)"),
                ("मैलस का नियम (Malus's Law)", "I = I₀ cos²θ", "अध्रुवित प्रकाश पोलेरॉइड से गुजरने पर I = I₀ / 2"),
            ],
        },
        {
            "name": "11. विकिरण तथा द्रव्य की द्वैत प्रकृति",
            "formulas": [
                ("फोटॉन की ऊर्जा (Photon Energy E)", "E = hν = hc / λ", "h = 6.626 × 10⁻³⁴ J·s, hc ≈ 1242 eV·nm"),
                ("इलेक्ट्रॉन-वोल्ट (eV) व जूल में संबंध", "1 eV = 1.6 × 10⁻¹⁹ J", "1 MeV = 1.6 × 10⁻¹³ J"),
                ("फोटॉन का गतिक द्रव्यमान (m)", "m = E / c² = hν / c² = h / (cλ)", "फोटॉन का विराम द्रव्यमान m₀ = 0 होता है"),
                ("फोटॉन का संवेग (Momentum p)", "p = E / c = hν / c = h / λ", "SI मात्रक: kg·m/s"),
                ("प्रति सेकंड उत्सर्जित फोटॉनों की संख्या", "n = P / E = Pλ / (hc)", "P = प्रकाश स्रोत की शक्ति (वाट में)"),
                ("कार्यफलन (Work Function φ₀)", "φ₀ = hν₀ = hc / λ₀", "ν₀ = देहली आवृत्ति, λ₀ = देहली तरंगदैर्ध्य"),
                ("आइंस्टीन का प्रकाश-विद्युत समीकरण", "hν = φ₀ + K_max  ⇒  K_max = h(ν - ν₀)", "प्रकाश की कण प्रकृति की पुष्टि"),
                ("अधिकतम गतिज ऊर्जा व निरोधी विभव (V₀)", "K_max = ½ m v_max² = e V₀", "V₀ = निरोधी विभव (Stopping Potential)"),
                ("निरोधी विभव व आवृत्ति में संबंध", "V₀ = (h / e)ν - (φ₀ / e)", "ग्राफ की ढाल (Slope) = h / e"),
                ("डी-ब्रॉग्ली तरंगदैर्ध्य (de Broglie Wavelength)", "λ = h / p = h / (mv)", "द्रव्य तरंगें (Matter Waves)"),
                ("गतिज ऊर्जा K के पदों में डी-ब्रॉग्ली तरंगदैर्ध्य", "λ = h / √(2mK)", "ताप T पर गैस अणु: λ = h / √(3mk_B T)"),
                ("V वोल्ट से त्वरित आवेशित कण की तरंगदैर्ध्य", "λ = h / √(2mqV)", "सामान्य आवेश q के लिए"),
                ("V वोल्ट से त्वरित इलेक्ट्रॉन की डी-ब्रॉग्ली तरंगदैर्ध्य", "λ = 12.27 / √V  Å  = 1.227 / √V  nm", "अत्यंत महत्वपूर्ण सीधा सूत्र"),
            ],
        },
        {
            "name": "12. परमाणु",
            "formulas": [
                ("निकटतम पहुँच की दूरी (Distance of Closest Approach)", "r₀ = (1 / 4πε₀) · (2Ze² / K)", "α-कण प्रकीर्णन में K = ½mv²"),
                ("संघट्ट प्राचल (Impact Parameter b)", "b = (1 / 4πε₀) · [Ze² cot(θ/2) / K]", "θ = प्रकीर्णन कोण"),
                ("बोहर का कोणीय संवेग क्वांटीकरण नियम", "L = mvr = n h / (2π)", "n = 1, 2, 3... (मुख्य क्वांटम संख्या)"),
                ("बोहर की द्वितीय अभिधारणा (विकिरण उत्सर्जन)", "hν = E_i - E_f", "उच्च कक्षा से निम्न कक्षा में संक्रमण"),
                ("n-वीं बोहर कक्षा की त्रिज्या (r_n)", "r_n = (ε₀ h² n²) / (π m Z e²) = 0.529 (n² / Z) Å", "r_n ∝ n² / Z (हाइड्रोजन r₁ = 0.529 Å)"),
                ("n-वीं कक्षा में इलेक्ट्रॉन का वेग (v_n)", "v_n = (Z e²) / (2 ε₀ h n) = (2.18 × 10⁶) (Z / n) m/s", "v_n ∝ Z / n (प्रथम कक्षा v₁ = c / 137)"),
                ("n-वीं कक्षा में इलेक्ट्रॉन की कुल ऊर्जा (E_n)", "E_n = -13.6 (Z² / n²) eV", "E_n ∝ -Z² / n² (H के लिए: E₁ = -13.6 eV, E₂ = -3.4 eV)"),
                ("गतिज ऊर्जा (K), स्थितिज ऊर्जा (U) व कुल ऊर्जा (E) संबंध", "K = -E,  U = 2E,  U = -2K", "हाइड्रोजन आयनन ऊर्जा = +13.6 eV"),
                ("हाइड्रोजन स्पेक्ट्रम का रिडबर्ग सूत्र (Rydberg Formula)", "1 / λ = R Z² (1/n₁² - 1/n₂²)", "रिडबर्ग नियतांक R = 1.097 × 10⁷ m⁻¹"),
                ("लाइमन श्रेणी (Lyman Series)", "1 / λ = R (1/1² - 1/n₂²),  n₂ = 2, 3, 4...", "पराबैंगनी क्षेत्र (UV Region)"),
                ("बामर श्रेणी (Balmer Series)", "1 / λ = R (1/2² - 1/n₂²),  n₂ = 3, 4, 5...", "दृश्य प्रकाश क्षेत्र (Visible Region)"),
                ("पाश्चन, ब्रैकेट व फुंड श्रेणियाँ", "पाश्चन: n₁=3 | ब्रैकेट: n₁=4 | फुंड: n₁=5", "तीनों अवरक्त क्षेत्र (Infrared Region) में"),
                ("कुल उत्सर्जित स्पेक्ट्रमी रेखाओं की संख्या", "N = n(n - 1) / 2", "n-वीं उत्तेजित अवस्था से मूल अवस्था तक"),
            ],
        },
        {
            "name": "13. नाभिक",
            "formulas": [
                ("परमाणु द्रव्यमान मात्रक (1 amu या 1 u)", "1 u = 1.6605 × 10⁻²⁷ kg ≈ 931.5 MeV / c²", "आइंस्टीन द्रव्यमान-ऊर्जा: E = Δm · c²"),
                ("नाभिक की त्रिज्या (Nuclear Radius R)", "R = R₀ A^(1/3)", "R₀ = 1.2 × 10⁻¹⁵ m = 1.2 fm, A = द्रव्यमान संख्या"),
                ("नाभिक का आयतन (V)", "V = (4/3)π R₀³ A  ⇒  V ∝ A", "द्रव्यमान संख्या के समानुपाती"),
                ("नाभिकीय घनत्व (Nuclear Density ρ)", "ρ = 3m / (4π R₀³) ≈ 2.3 × 10¹⁷ kg/m³", "सभी नाभिकों के लिए नियत (A पर निर्भर नहीं)"),
                ("द्रव्यमान क्षति (Mass Defect Δm)", "Δm = [Z m_p + (A - Z) m_n] - M_nucleus", "Z = प्रोटॉन, A - Z = न्यूट्रॉन"),
                ("नाभिकीय बंधन ऊर्जा (Binding Energy BE)", "BE = Δm × 931.5 MeV", "प्रति न्यूक्लिऑन बंधन ऊर्जा = BE / A (Fe-56 सर्वाधिक स्थायी)"),
                ("रेडियोएक्टिव क्षय नियम (Rutherford-Soddy Law)", "N = N₀ e^(-λt)", "λ = क्षय नियतांक (Decay Constant)"),
                ("सक्रियता (Activity A / R)", "A = -dN/dt = λN = A₀ e^(-λt)", "SI मात्रक: बेकुरल (Bq), 1 Ci = 3.7 × 10¹⁰ Bq"),
                ("अर्ध-आयु काल (Half-Life T_1/2)", "T_1/2 = ln(2) / λ = 0.693 / λ", "N₀ से N₀/2 होने में लगा समय"),
                ("माध्य आयु (Mean Life τ)", "τ = 1 / λ = T_1/2 / 0.693 = 1.44 T_1/2", "τ > T_1/2"),
                ("n अर्ध-आयु के पश्चात शेष नाभिकों की संख्या", "N = N₀ (1 / 2)ⁿ", "यहाँ n = कुल समय (t) / अर्ध-आयु (T_1/2)"),
                ("α-क्षय (Alpha Decay) समीकरण", "₂Xᴬ → ₂₋₂Yᴬ⁻⁴ + ₂He⁴ + Q", "परमाणु क्रमांक में 2 व द्रव्यमान में 4 की कमी"),
                ("β⁻-क्षय व β⁺-क्षय समीकरण", "β⁻: ₂Xᴬ → ₂₊₁Yᴬ + e⁻ + ν̄ | β⁺: ₂Xᴬ → ₂₋₁Yᴬ + e⁺ + ν", "द्रव्यमान संख्या A अपरिवर्तित रहती है"),
                ("γ-क्षय (Gamma Decay)", "₂X*ᴬ → ₂Xᴬ + γ (फोटॉन)", "Z और A दोनों अपरिवर्तित रहते हैं"),
            ],
        },
        {
            "name": "14. अर्धचालक इलेक्ट्रॉनिकी",
            "formulas": [
                ("नैज (शुद्ध) अर्धचालक में आवेश वाहक सांद्रता", "n_e = n_h = n_i", "n_i = नैज वाहक सांद्रता (Si: E_g = 1.1 eV, Ge: 0.7 eV)"),
                ("द्रव्यमान क्रिया नियम (Mass Action Law)", "n_e · n_h = n_i²", "n-प्रकार में n_e >> n_h; p-प्रकार में n_h >> n_e"),
                ("अर्धचालक में कुल विद्युत धारा", "I = I_e + I_h = e A (n_e v_e + n_h v_h)", "SI मात्रक: ऐम्पियर (A)"),
                ("अर्धचालक की विद्युत चालकता (σ)", "σ = e (n_e μ_e + n_h μ_h)", "ताप बढ़ाने पर अर्धचालक की चालकता बढ़ती है"),
                ("गतिक प्रतिरोध (Dynamic Resistance of Diode)", "r_d = ΔV / ΔI", "अग्र अभिनति में कम, उत्क्रम अभिनति में बहुत अधिक"),
                ("अर्ध-तरंग दिष्टकारी (Half-Wave Rectifier)", "निर्गत आवृत्ति = f_in,  दक्षता η_max = 40.6%", "V_dc = V₀ / π"),
                ("पूर्ण-तरंग दिष्टकारी (Full-Wave Rectifier)", "निर्गत आवृत्ति = 2 f_in,  दक्षता η_max = 81.2%", "V_dc = 2V₀ / π"),
                ("सौर सेल व LED की अभिनति", "LED: अग्र अभिनति (Forward Bias) | फोटोडायोड: उत्क्रम अभिनति (Reverse Bias)", "ज़ेनर डायोड: वोल्टता नियंत्रक (उत्क्रम भंजन में)"),
                ("ट्रांजिस्टर में धाराओं का संबंध", "I_E = I_B + I_C", "I_B बहुत अल्प (μA में), I_E व I_C (mA में)"),
                ("उभयनिष्ठ आधार धारा लाभ (α)", "α = ΔI_C / ΔI_E", "α < 1 (लगभग 0.95 से 0.99)"),
                ("उभयनिष्ठ उत्सर्जक धारा लाभ (β)", "β = ΔI_C / ΔI_B", "β >> 1 (लगभग 20 से 500)"),
                ("α और β में संबंध", "β = α / (1 - α)  तथा  α = β / (1 + β)", "अत्यंत महत्वपूर्ण बोर्ड सूत्र"),
                ("CE प्रवर्धक का वोल्टता लाभ (Voltage Gain A_v)", "A_v = β · (R_out / R_in)", "शक्ति लाभ: A_p = β² · (R_out / R_in)"),
                ("NOT गेट बूलियन व्यंजक", "Y = Ā", "1 इनपुट, 1 आउटपुट (व्युत्क्रमक)"),
                ("OR गेट व AND गेट बूलियन व्यंजक", "OR: Y = A + B  |  AND: Y = A · B", "मूल लॉजिक गेट्स"),
                ("NOR गेट व NAND गेट (सार्वत्रिक गेट्स)", "NOR: Y = (A + B)̄  |  NAND: Y = (A · B)̄", "NAND और NOR सार्वत्रिक (Universal) गेट हैं"),
                ("डी-मॉर्गन प्रमेय (De Morgan's Theorems)", "(A + B)̄ = Ā · B̄  तथा  (A · B)̄ = Ā + B̄", "बूलियन बीजगणित के मुख्य नियम"),
            ],
        },
    ],
}

# ================= COMPLETE CHEMISTRY FORMULAS & REACTIONS (ALL 16 CHAPTERS) =================
CHEMISTRY_DATA = {
    "title": "🧪 Chemistry (रसायन शास्त्र)",
    "chapters": [
        {
            "name": "1. ठोस अवस्था",
            "formulas": [
                ("सरल घनीय एकक कोष्ठिका (SCC) में परमाणुओं की संख्या", "Z = 8 × (1/8) = 1", "समन्वय संख्या (CN) = 6"),
                ("अंतःकेंद्रित घनीय (BCC) में परमाणुओं की संख्या", "Z = 8 × (1/8) + 1 = 2", "समन्वय संख्या (CN) = 8"),
                ("फलक केंद्रित घनीय (FCC / CCP) में परमाणुओं की संख्या", "Z = 8 × (1/8) + 6 × (1/2) = 4", "समन्वय संख्या (CN) = 12"),
                ("अंत्य-केंद्रित घनीय (ECC) में परमाणुओं की संख्या", "Z = 8 × (1/8) + 2 × (1/2) = 2", "दो विपरीत फलकों पर परमाणु"),
                ("SCC में परमाणु त्रिज्या (r) व कोर लंबाई (a) संबंध", "r = a / 2  ⇒  a = 2r", "निकटतम पड़ोसी दूरी d = a"),
                ("BCC में परमाणु त्रिज्या (r) व कोर लंबाई (a) संबंध", "r = (√3 / 4) a  ⇒  a = 4r / √3", "निकटतम पड़ोसी दूरी d = (√3/2)a"),
                ("FCC में परमाणु त्रिज्या (r) व कोर लंबाई (a) संबंध", "r = a / (2√2) = (√2 / 4) a", "निकटतम पड़ोसी दूरी d = a / √2"),
                ("संकुलन क्षमता (Packing Efficiency) का सूत्र", "PE = [Z × (4/3)πr³ / a³] × 100%", "रिक्त स्थान % = 100% - PE"),
                ("SCC की संकुलन क्षमता व रिक्ति प्रतिशत", "PE = 52.4%  |  रिक्ति = 47.6%", "न्यूनतम संकुलन क्षमता"),
                ("BCC की संकुलन क्षमता व रिक्ति प्रतिशत", "PE = 68%  |  रिक्ति = 32%", "उदाहरण: Na, K, Fe, CsCl"),
                ("FCC / HCP की संकुलन क्षमता व रिक्ति प्रतिशत", "PE = 74%  |  रिक्ति = 26%", "सर्वाधिक संकुलन क्षमता (Cu, Ag, Au)"),
                ("क्रिस्टल एकक कोष्ठिका का घनत्व (Density ρ)", "ρ = (Z × M) / (a³ × N_A)", "N_A = 6.022 × 10²³ mol⁻¹, M = मोलर द्रव्यमान"),
                ("चतुष्फलकीय व अष्टफलकीय रिक्तियों की संख्या", "अष्टफलकीय रिक्ति = N  |  चतुष्फलकीय रिक्ति = 2N", "N = निविड संकुलित गोलों की संख्या"),
                ("चतुष्फलकीय रिक्ति के लिए त्रिज्या अनुपात", "r_void / R_sphere = 0.225", "समन्वय संख्या = 4"),
                ("अष्टफलकीय रिक्ति के लिए त्रिज्या अनुपात", "r_void / R_sphere = 0.414", "समन्वय संख्या = 6"),
                ("घनीय रिक्ति के लिए त्रिज्या अनुपात (CsCl प्रकार)", "r⁺ / r⁻ = 0.732 से 1.000", "समन्वय संख्या = 8"),
                ("ब्रैग का विवर्तन समीकरण (Bragg's Equation)", "n λ = 2 d sinθ", "d = क्रिस्टल तलों के बीच की दूरी"),
                ("शॉट्की त्रुटि (Schottky Defect) का प्रभाव", "समान संख्या में धनायन व ऋणायन लुप्त ⇒ घनत्व घटता है", "उदाहरण: NaCl, KCl, CsCl, AgBr"),
                ("फ्रेंकेल त्रुटि (Frenkel Defect) का प्रभाव", "धनायन अंतराकाशी स्थान में जाता है ⇒ घनत्व अपरिवर्तित", "उदाहरण: ZnS, AgCl, AgBr, AgI"),
                ("F-केंद्र (F-Center) का सूत्र व गुण", "ऋणायन रिक्तिका में फँसा अयुग्मित इलेक्ट्रॉन", "क्रिस्टल को रंग एवं अनुचुंबकीय गुण देता है"),
            ],
        },
        {
            "name": "2. विलयन",
            "formulas": [
                ("द्रव्यमान प्रतिशत (% w/w)", "% w/w = (विलेय का द्रव्यमान / विलयन का द्रव्यमान) × 100", "ताप से अप्रभावित"),
                ("आयतन प्रतिशत (% v/v)", "% v/v = (विलेय का आयतन / विलयन का आयतन) × 100", "ताप पर निर्भर"),
                ("पार्ट्स प्रति मिलियन (ppm)", "ppm = (विलेय के भागों की संख्या / कुल भाग) × 10⁶", "अति तनु विलयनों की सांद्रता हेतु"),
                ("मोल अंश / मोल प्रभाज (Mole Fraction χ)", "χ_A = n_A / (n                ("परासरण दाब से विलेय का अणुभार (M_B)", "M_B = (w_B × R × T) / (π × V)", "बृहदाणुओं (प्रोटीन/बहुलक) हेतु सर्वोत्तम विधि"),
                ("वांट हॉफ गुणांक (van't Hoff Factor i)", "i = प्रेक्षित अणुसंख्य गुणधर्म / सैद्धांतिक गुणधर्म", "सैद्धांतिक अणुभार / प्रेक्षित अणुभार"),
                ("वियोजन की मात्रा (Degree of Dissociation α)", "α = (i - 1) / (n - 1)", "n = प्रति अणु उत्पन्न कुल आयनों की संख्या"),
                ("संगुणन की मात्रा (Degree of Association α)", "α = (1 - i) / (1 - 1/n)", "डाइमर (n=2) हेतु: α = 2(1 - i)"),
                ("असामान्य अणुसंख्य गुणधर्मों के संशोधित सूत्र", "ΔT_b = i K_b m,  ΔT_f = i K_f m,  π = i C R T", "NaCl हेतु i ≈ 2, CaCl₂ हेतु i ≈ 3"),
            ],
        },
        {
            "name": "3. वैद्युतरसायन",
            "formulas": [
                ("मानक सेल विभव (Standard Cell Potential E°_cell)", "E°_cell = E°_cathode - E°_anode = E°_right - E°_left", "SHE (मानक हाइड्रोजन इलेक्ट्रोड) का E° = 0.00 V"),
                ("नेर्नस्ट समीकरण (Nernst Equation - किसी ताप पर)", "E_cell = E°_cell - (RT / nF) ln Q", "n = स्थानांतरित इलेक्ट्रॉनों के मोल, F = 96500 C/mol"),
                ("298 K (25°C) पर नेर्नस्ट समीकरण", "E_cell = E°_cell - (0.0591 / n) log₁₀ Q", "Q = [उत्पाद] / [अभिकारक]"),
                ("साम्यावस्था पर सेल विभव व साम्य स्थिरांक (K_c)", "E°_cell = (0.0591 / n) log₁₀ K_c  (298 K पर)", "साम्यावस्था पर E_cell = 0 होता है"),
                ("मानक गिब्स मुक्त ऊर्जा परिवर्तन (ΔG°)", "ΔG° = -n F E°_cell = -2.303 R T log₁₀ K_c", "स्वतः प्रवर्तित (Spontaneous) हेतु ΔG° < 0, E°_cell > 0"),
                ("विद्युत प्रतिरोध (R) व चालकता (G)", "G = 1 / R = κ (A / l)", "G का SI मात्रक: सीमेंस (S) या Ω⁻¹"),
                ("विशिष्ट चालकता / चालकता (Conductivity κ)", "κ = (1 / R) × (l / A) = G × G*", "SI मात्रक: S/m या S/cm"),
                ("सेल स्थिरांक (Cell Constant G*)", "G* = l / A = R × κ", "SI मात्रक: m⁻¹ या cm⁻¹"),
                ("मोलर चालकता (Molar Conductivity Λ_m)", "Λ_m = (κ × 1000) / M", "SI मात्रक: S·cm²·mol⁻¹ (M = मोलरता)"),
                ("तुल्यांकी चालकता (Equivalent Conductivity Λ_eq)", "Λ_eq = (κ × 1000) / N", "मात्रक: S·cm²·eq⁻¹ (N = नॉर्मलता)"),
                ("डेबाई-हकल-ऑनसेगर समीकरण (प्रबल विद्युत अपघट्य)", "Λ_m = Λ°_m - A √C", "तनुता बढ़ाने पर Λ_m बढ़ती है पर κ घटती है"),
                ("कोलराउश का स्वतंत्र अभिगमन नियम", "Λ°_m(A_x B_y) = x λ°₊ + y λ°₋", "अनंत तनुता पर मोलर चालकता"),
                ("दुर्बल विद्युत अपघट्य के वियोजन की मात्रा (α)", "α = Λ_m / Λ°_m", "वियोजन स्थिरांक K_a = Cα² / (1 - α)"),
                ("फैराडे का विद्युत अपघटन प्रथम नियम", "w = Z Q = Z I t = (E / 96500) I t", "Z = विद्युत रासायनिक तुल्यांक = E / F"),
                ("फैराडे का विद्युत अपघटन द्वितीय नियम", "w₁ / w₂ = E₁ / E₂", "श्रेणीक्रम में समान आवेश प्रवाहित करने पर"),
                ("1 फैराडे (1 F) का मान", "1 F = N_A × e = 96487 ≈ 96500 C/mol", "1 मोल इलेक्ट्रॉन पर कुल आवेश"),
                ("सीसा संचायक सेल (Lead Storage Battery) अभिक्रिया", "Pb + PbO₂ + 2H₂SO₄ ⇌ 2PbSO₄ + 2H₂O", "एनोड: Pb, कैथोड: PbO₂"),
                ("H₂-O₂ ईंधन सेल (Fuel Cell) की अभिक्रिया व दक्षता", "2H₂(g) + O₂(g) → 2H₂O(l)", "ऊष्मागतिक दक्षता η = ΔG / ΔH ≈ 70%"),
            ],
        },
        {
            "name": "4. रासायनिक बलगतिकी",
            "formulas": [
                ("अभिक्रिया का औसत वेग (Average Rate)", "r_avg = -Δ[R] / Δt = +Δ[P] / Δt", "SI मात्रक: mol·L⁻¹·s⁻¹"),
                ("तात्क्षणिक अभिक्रिया वेग (Instantaneous Rate)", "r_inst = -d[R] / dt = +d[P] / dt", "सामान्य: aA + bB → cC हेतु r = -(1/a)d[A]/dt"),
                ("वेग नियम / दर व्यंजक (Rate Law)", "Rate = k [A]ˣ [B]ʸ", "अभिक्रिया की कोटि n = x + y"),
                ("n-वीं कोटि के वेग स्थिरांक (k) का मात्रक", "k का मात्रक = (mol·L⁻¹)^(1-n) · s⁻¹", "शून्य: mol·L⁻¹·s⁻¹ | प्रथम: s⁻¹ | द्वितीय: L·mol⁻¹·s⁻¹"),
                ("शून्य कोटि अभिक्रिया का समाकलित वेग समीकरण", "k = ([R]₀ - [R]) / t  ⇒  [R] = [R]₀ - kt", "उदाहरण: स्वर्ण पृष्ठ पर NH₃ का विघटन"),
                ("शून्य कोटि अभिक्रिया का अर्ध-आयु काल (t_1/2)", "t_1/2 = [R]₀ / (2k)", "t_1/2 ∝ [R]₀ (प्रारंभिक सांद्रता के समानुपाती)"),
                ("शून्य कोटि अभिक्रिया के पूर्ण होने का समय", "t_100% = [R]₀ / k = 2 t_1/2", "निश्चित समय में पूर्ण होती है"),
                ("प्रथम कोटि अभिक्रिया का समाकलित वेग समीकरण", "k = (2.303 / t) log₁₀ ([R]₀ / [R])", "चरघातांकी रूप: [R] = [R]₀ e^(-kt)"),
                ("प्रथम कोटि अभिक्रिया का अर्ध-आयु काल (t_1/2)", "t_1/2 = 0.693 / k", "प्रारंभिक सांद्रता [R]₀ पर निर्भर नहीं करता"),
                ("गैसीय प्रथम कोटि अभिक्रिया हेतु वेग स्थिरांक", "k = (2.303 / t) log₁₀ [P_i / (2P_i - P_t)]", "P_i = प्रारंभिक दाब, P_t = t समय पर कुल दाब"),
                ("प्रथम कोटि में 99.9% व 90% पूर्णता में संबंध", "t_99.9% = 10 × t_50%  |  t_99% = 2 × t_90%", "महत्वपूर्ण बोर्ड शॉर्टकट"),
                ("छद्म प्रथम कोटि अभिक्रिया (Pseudo First Order)", "CH₃COOC₂H₅ + H₂O → CH₃COOH + C₂H₅OH", "कोटि = 1, आणविकता = 2"),
                ("ताप गुणांक (Temperature Coefficient μ)", "μ = k_(T+10) / k_T ≈ 2 से 3", "10°C ताप बढ़ाने पर वेग दोगुना-तिगुना हो जाता है"),
                ("आरहेनियस समीकरण (Arrhenius Equation)", "k = A e^(-E_a / RT)", "A = आवृत्ति कारक, E_a = सक्रियण ऊर्जा"),
                ("आरहेनियस समीकरण का लघुगणकीय रूप", "log₁₀ k = log₁₀ A - E_a / (2.303 R T)", "ग्राफ log k बनाम 1/T की ढाल = -E_a / (2.303 R)"),
                ("दो अलग-अलग तापों T₁ व T₂ पर आरहेनियस समीकरण", "log₁₀ (k₂ / k₁) = (E_a / 2.303 R) [(T₂ - T₁) / (T₁ T₂)]", "R = 8.314 J/(K·mol)"),
                ("सक्रियण ऊर्जा (E_a) व अभिक्रिया एन्थैल्पी (ΔH)", "ΔH = E_a(अग्र) - E_a(पश्च)", "ऊष्माक्षेपी: ΔH < 0, ऊष्माशोषी: ΔH > 0"),
            ],
        },
        {
            "name": "5. पृष्ठ रसायन",
            "formulas": [
                ("अधिशोषण में ऊष्मागतिक परिवर्तन", "ΔH < 0,  ΔS < 0,  तथा  ΔG = ΔH - TΔS < 0", "ऊष्माक्षेपी व स्वतः प्रवर्तित प्रक्रम"),
                ("फ्रॉयंडलिक अधिशोषण समतापी समीकरण", "x / m = k · P^(1/n)  (n > 1)", "x = अधिशोष्य का द्रव्यमान, m = अधिशोषक का द्रव्यमान"),
                ("फ्रॉयंडलिक समीकरण का लघुगणकीय रूप", "log₁₀ (x / m) = log₁₀ k + (1 / n) log₁₀ P", "0 ≤ 1/n ≤ 1 (मध्यम दाब पर लागू)"),
                ("विलयन प्रावस्था से अधिशोषण समीकरण", "x / m = k · C^(1/n)", "C = साम्य सांद्रता"),
                ("हार्डी-शुल्जे नियम (Hardy-Schulze Rule)", "स्कंदन शक्ति ∝ (ऊर्णन आयन की संयोजकता)ⁿ", "धनायन क्रम: Al³⁺ > Ba²⁺ > Na⁺ | ऋणायन: [Fe(CN)₆]⁴⁻ > PO₄³⁻ > SO₄²⁻ > Cl⁻"),
                ("ऊर्णन मान (Coagulation / Flocculation Value)", "ऊर्णन मान = 1 / स्कंदन शक्ति (mmol/L में)", "न्यूनतम मिलीमोल सांद्रता जो 2 घंटे में स्कंदन करे"),
                ("स्वर्ण संख्या (Gold Number - ज़िगमोन्डी)", "रक्षी शक्ति ∝ 1 / स्वर्ण संख्या", "जिलेटिन की स्वर्ण संख्या न्यूनतम (0.005–0.01) ⇒ सर्वश्रेष्ठ रक्षी कोलॉइड"),
                ("कणों के आकार के आधार पर वर्गीकरण", "वास्तविक विलयन: < 1 nm | कोलॉइड: 1–1000 nm | निलंबन: > 1000 nm", "टिंडल प्रभाव व ब्राउनी गति कोलॉइड के गुण हैं"),
            ],
        },
        {
            "name": "6. तत्वों के निष्कर्षण के सिद्धांत एवं प्रक्रम",
            "formulas": [
                ("ऐलुमिनियम (Al) के प्रमुख अयस्क", "बॉक्साइट: Al₂O₃·2H₂O | क्रायोलाइट: Na₃AlF₆ | कोरंडम: Al₂O₃", "निष्कर्षण: हॉल-हेरॉल्ट विद्युत अपघटनी विधि"),
                ("लोहा (Fe) के प्रमुख अयस्क", "हेमेटाइट: Fe₂O₃ | मैग्नेटाइट: Fe₃O₄ | सिडेराइट: FeCO₃ | आयरन पाइराइट: FeS₂", "निष्कर्षण: वात्या भट्टी (Blast Furnace)"),
                ("ताँबा (Cu) के प्रमुख अयस्क", "कॉपर पाइराइट: CuFeS₂ | क्यूप्राइट: Cu₂O | मैलाकाइट: CuCO₃·Cu(OH)₂", "प्रगलन द्वारा कॉपर मैट (Cu₂S + FeS) प्राप्त"),
                ("जिंक (Zn) के प्रमुख अयस्क", "जिंक ब्लेंड: ZnS | कैलामाइन: ZnCO₃ | जिंकाइट: ZnO", "निष्कर्षण: भर्जन व कार्बन अपचयन"),
                ("सीसा (Pb) व पारा (Hg) के मुख्य अयस्क", "गैलेना: PbS  |  सिनेबार: HgS", "सल्फाइड अयस्कों का सांद्रण: फेन प्लवन विधि"),
                ("भर्जन (Roasting) अभिक्रिया", "2ZnS + 3O₂ → 2ZnO + 2SO₂↑", "वायु की अधिकता में सल्फाइड अयस्क को गर्म करना"),
                ("निस्तापन (Calcination) अभिक्रिया", "ZnCO₃ → ZnO + CO₂↑  (या CaCO₃ → CaO + CO₂)", "वायु की अनुपस्थिति/सीमित वायु में गर्म करना"),
                ("वात्या भट्टी में धातुमल (Slag) निर्माण", "CaO (गालक) + SiO₂ (अधात्री) → CaSiO₃ (धातुमल)", "कॉपर निष्कर्षण में: FeO + SiO₂ → FeSiO₃"),
                ("हॉल-हेरॉल्ट प्रक्रम (Al निष्कर्षण)", "2Al₂O₃ + 3C → 4Al + 3CO₂↑", "क्रायोलाइट (Na₃AlF₆) गलनांक घटाता व चालकता बढ़ाता है"),
                ("मॉन्ड प्रक्रम (Nickel शोधन)", "Ni + 4CO (330–350 K) → Ni(CO)₄ → Ni + 4CO (450–470 K)", "वाष्पशील संकुल निर्माण विधि"),
                ("वान-आर्केल प्रक्रम (Zr व Ti शोधन)", "Zr + 2I₂ (870 K) → ZrI₄ → Zr + 2I₂ (2075 K)", "अतिशुद्ध धातु प्राप्त करने की विधि"),
            ],
        },
        {
            "name": "7. p-ब्लॉक के तत्व",
            "formulas": [
                ("नाइट्रोजन निर्माण की हैबर विधि (Haber's Process)", "N₂(g) + 3H₂(g) ⇌ 2NH₃(g),  ΔH = -46.1 kJ/mol", "उत्प्रेरक: Fe + Mo/K₂O, ताप: 700 K, दाब: 200 atm"),
                ("नाइट्रिक अम्ल निर्माण की ओस्टवाल्ड विधि", "4NH₃ + 5O₂ (Pt/Rh, 500 K) → 4NO + 6H₂O → NO₂ → HNO₃", "HNO₃ एक प्रबल ऑक्सीकारक है"),
                ("सल्फ्यूरिक अम्ल निर्माण की संपर्क विधि (Contact Process)", "2SO₂ + O₂ ⇌ 2SO₃ (उत्प्रेरक: V₂O₅),  SO₃ + H₂SO₄ → H₂S₂O₇ (ओलियम)", "ओलियम + H₂O → 2H₂SO₄"),
                ("क्लोरीन निर्माण की डीकन विधि (Deacon's Process)", "4HCl + O₂ (CuCl₂ उत्प्रेरक, 723 K) → 2Cl₂ + 2H₂O", "Cl₂ का विरंजन गुण नवजात ऑक्सीजन [O] के कारण"),
                ("वर्ग-15 के हाइड्राइडों की क्षारीयता व स्थायित्व क्रम", "NH₃ > PH₃ > AsH₃ > SbH₃ > BiH₃", "अपचायक क्षमता विपरीत क्रम में बढ़ती है"),
                ("फॉस्फोरस के ऑक्सीअम्लों की क्षारकता (Basicity)", "H₃PO₂ (एकक्षारकीय) | H₃PO₃ (द्विक्षारकीय) | H₃PO₄ (त्रिक्षारकीय)", "P-OH बंधों की संख्या = क्षारकता"),
                ("हैलोजन अम्लों की अम्लीय प्रबलता क्रम", "HF < HCl < HBr < HI", "बंध वियोजन ऊर्जा घटने के कारण"),
                ("अंतरहैलोजन यौगिकों की ज्यामिति व संकरण", "XX' (रेखीय) | XX'₃ (sp³d, T-आकार) | XX'₅ (sp³d², वर्ग पिरामिडी) | XX'₇ (sp³d³, पंचभुजीय द्विपिरामिडी)", "अत्यंत महत्वपूर्ण ऑब्जेक्टिव"),
                ("जीनॉन फ्लोराइड्स की आकृति व संकरण", "XeF₂ (sp³d, रेखीय) | XeF₄ (sp³d², वर्ग समतलीय) | XeF₆ (sp³d³, विकृत अष्टफलकीय)", "नील बार्टलेट द्वारा प्रथम उत्कृष्ट गैस यौगिक खोजा गया"),
                ("XeO₃ व XeOF₄ की ज्यामिति व संकरण", "XeO₃: sp³ (पिरामिडी)  |  XeOF₄: sp³d² (वर्ग पिरामिडी)", "उत्कृष्ट गैसों में सर्वाधिक यौगिक Xe बनाता है"),
            ],
        },
        {
            "name": "8. d- एवं f-ब्लॉक के तत्व",
            "formulas": [
                ("संक्रमण तत्वों का सामान्य इलेक्ट्रॉनिक विन्यास", "(n - 1)d¹⁻¹⁰ ns¹⁻²", "अपवाद: Cr (3d⁵4s¹) तथा Cu (3d¹⁰4s¹)"),
                ("चुंबकीय आघूर्ण (Spin-Only Magnetic Moment μ)", "μ = √[n(n + 2)] BM", "n = अयुग्मित इलेक्ट्रॉनों की संख्या, BM = बोर मैग्नेटॉन"),
                ("प्रथम संक्रमण श्रेणी में सर्वाधिक ऑक्सीकरण अवस्था", "मैंगनीज (Mn): +2 से +7 तक", "3d व 4s कक्षकों में कम ऊर्जा अंतर के कारण"),
                ("Zn, Cd, Hg संक्रमण तत्व क्यों नहीं हैं?", "पूर्णतः भरे (n-1)d¹⁰ विन्यास के कारण (मूल व आयनित अवस्था में)", "परिवर्ती ऑक्सीकरण अवस्था व रंग नहीं दिखाते"),
                ("लैंथेनॉइड का सामान्य इलेक्ट्रॉनिक विन्यास", "[Xe] 4f¹⁻¹⁴ 5d⁰⁻¹ 6s²", "सामान्य ऑक्सीकरण अवस्था: +3 (Ce⁴⁺ ऑक्सीकारक, Eu²⁺ अपचायक)"),
                ("लैंथेनॉइड आकुंचन (Lanthanoid Contraction)", "La से Lu तक परमाणु व आयनिक त्रिज्या में क्रमिक कमी", "कारण: 4f इलेक्ट्रॉनों का दुर्बल परिरक्षण प्रभाव; परिणाम: Zr व Hf की समान त्रिज्या"),
                ("पोटैशियम डाइक्रोमेट (K₂Cr₂O₇) का अम्लीय ऑक्सीकरण", "Cr₂O₇²⁻ + 14H⁺ + 6e⁻ → 2Cr³⁺ (हरा) + 7H₂O", "क्रोमेट (पीला) ⇌ डाइक्रोमेट (नारंगी) pH निर्भर"),
                ("पोटैशियम परमैंगनेट (KMnO₄) का ऑक्सीकरण", "अम्लीय: MnO₄⁻ + 8H⁺ + 5e⁻ → Mn²⁺ | उदासीन: MnO₄⁻ → MnO₂", "बैंगनी रंग: आवेश स्थानांतरण (Charge Transfer) के कारण"),
                ("प्रमुख मिश्रधातुएँ (Alloys)", "पीतल (Brass): Cu + Zn | काँसा (Bronze): Cu + Sn | स्टेनलेस स्टील: Fe + Cr + Ni", "मिशधातु (Mischmetall): ~95% लैंथेनॉइड + 5% Fe"),
            ],
        },
        {
            "name": "9. उपसहसंयोजन यौगिक",
            "formulas": [
                ("प्रभावी परमाणु क्रमांक (EAN Rule - सिडविक)", "EAN = Z - (ऑक्सीकरण संख्या) + 2 × (समन्वय संख्या)", "यदि EAN उत्कृष्ट गैस (36, 54, 86) के बराबर हो तो संकुल स्थायी"),
                ("समन्वय संख्या (Coordination Number CN)", "CN = केंद्रीय धातु से जुड़े कुल एकदंती दाता परमाणुओं की संख्या", "द्विदंती लिगैंड (en, C₂O₄²⁻) दो उपसहसंयोजक बंध बनाता है"),
                ("उभयदंती लिगैंड (Ambidentate Ligands)", "NO₂⁻ (नाइट्रिटो-N / नाइट्रिटो-O),  CN⁻ / NC⁻,  SCN⁻ / NCS⁻", "बंधनीय समावयवता (Linkage Isomerism) दर्शाते हैं"),
                ("षट्दंती लिगैंड (Hexadentate Ligand)", "EDTA⁴⁻ (एथिलीनडाइऐमीनटेट्राऐसीटेट आयन)", "6 दाता परमाणु (2 N तथा 4 O) — कीलेट प्रभाव"),
                ("वर्नर का सिद्धांत (Werner's Theory)", "प्राथमिक संयोजकता = ऑक्सीकरण संख्या (आयननीय) | द्वितीयक संयोजकता = CN (अनायननीय)", "द्वितीयक संयोजकता दिशात्मक व ज्यामिति निर्धारक"),
                ("CN = 4 वाले संकुलों की ज्यामिति व संकरण", "sp³ → चतुष्फलकीय (Tetrahedral)  |  dsp² → वर्ग समतलीय (Square Planar)", "उदाहरण: [Ni(CO)₄] (sp³), [Ni(CN)₄]²⁻ (dsp²)"),
                ("CN = 6 वाले संकुलों की ज्यामिति व संकरण", "d²sp³ (आंतरिक कक्षक / निम्न चक्रण)  |  sp³d² (बाह्य कक्षक / उच्च चक्रण)", "दोनों की ज्यामिति अष्टफलकीय (Octahedral)"),
                ("स्पेक्ट्रमी रासायनिक श्रेणी (लिगैंड प्रबलता क्रम)", "I⁻ < Br⁻ < Cl⁻ < F⁻ < OH⁻ < H₂O < NH₃ < en < CN⁻ < CO", "CO व CN⁻ प्रबल क्षेत्र लिगैंड (युग्मन कराते हैं)"),
                ("क्रिस्टल क्षेत्र विपाटन ऊर्जा संबंध (Δ_t व Δ_o)", "Δ_t = (4 / 9) Δ_o", "चतुष्फलकीय विपाटन सदैव अष्टफलकीय से कम होता है"),
                ("अष्टफलकीय संकुलों में CFSE सूत्र", "CFSE = (-0.4 n_t₂g + 0.6 n_eg) Δ_o + m P", "P = युग्मन ऊर्जा (Pairing Energy)"),
            ],
        },
        {
            "name": "10. हैलोऐल्केन तथा हैलोऐरीन",
            "formulas": [
                ("डार्जन प्रक्रम (Darzen's Process)", "R-OH + SOCl₂ (पिरीडीन) → R-Cl + SO₂↑ + HCl↑", "ऐल्किल क्लोराइड बनाने की सर्वोत्तम विधि"),
                ("फिंकेलस्टाइन अभिक्रिया (Finkelstein Reaction)", "R-X + NaI (शुष्क ऐसीटोन) → R-I + NaX↓  (X = Cl, Br)", "हैलोजन विनिमय द्वारा ऐल्किल आयोडाइड निर्माण"),
                ("स्वार्ट्ज अभिक्रिया (Swarts Reaction)", "R-Br + AgF (या Hg₂F₂ / SbF₃) → R-F + AgBr", "ऐल्किल फ्लोराइड निर्माण की मुख्य विधि"),
                ("हुंसडीकर अभिक्रिया (Hunsdiecker Reaction)", "R-COOAg + Br₂ (CCl₄) → R-Br + CO₂↑ + AgBr↓", "एक कार्बन कम वाला ब्रोमोऐल्केन बनता है"),
                ("वुर्ट्ज़ अभिक्रिया (Wurtz Reaction)", "2R-X + 2Na (शुष्क ईथर) → R-R + 2NaX", "सममित उच्चतर ऐल्केन निर्माण"),
                ("वुर्ट्ज़-फिटिग अभिक्रिया (Wurtz-Fittig)", "C₆H₅X + R-X + 2Na (शुष्क ईथर) → C₆H₅-R + 2NaX", "ऐल्किल बेन्जीन (जैसे टॉलूईन) निर्माण"),
                ("फिटिग अभिक्रिया (Fittig Reaction)", "2C₆H₅X + 2Na (शुष्क ईथर) → C₆H₅-C₆H₅ (डाइफेनिल) + 2NaX", "दो ऐरिल समूहों का युग्मन"),
                ("सैंडमेयर अभिक्रिया (Sandmeyer Reaction)", "C₆H₅N₂⁺Cl⁻ + Cu₂Cl₂ / HCl → C₆H₅Cl + N₂↑", "Cu₂Br₂/HBr से ब्रोमोबेन्जीन बनता है"),
                ("गाटरमान अभिक्रिया (Gattermann Reaction)", "C₆H₅N₂⁺Cl⁻ + Cu चूर्ण / HCl → C₆H₅Cl + N₂↑", "ताँबा चूर्ण व हैलोजन अम्ल का उपयोग"),
                ("बॉलज-शीमान अभिक्रिया (Balz-Schiemann)", "C₆H₅N₂⁺Cl⁻ + HBF₄ → C₆H₅N₂⁺BF₄⁻ (Δ) → C₆H₅F + BF₃ + N₂", "फ्लोरोबेन्जीन बनाने की विधि"),
                ("सेटजेफ नियम (Saytzeff's Rule - विहाइड्रोहैलोजनीकरण)", "R-CH₂-CHX-CH₃ + ऐल्को. KOH → R-CH=CH-CH₃ (मुख्य)", "अधिक प्रतिस्थापित ऐल्कीन मुख्य उत्पाद बनता है"),
                ("SN¹ व SN² अभिक्रिया क्रियाशीलता क्रम", "SN¹: 3° > 2° > 1° (कार्बधनायन मध्यवर्ती) | SN²: 1° > 2° > 3° (वाल्डन प्रतीपन)", "हैलोजन क्रम: R-I > R-Br > R-Cl > R-F"),
                ("क्लोरोफॉर्म का ऑक्सीकरण (फॉस्जीन गैस)", "2CHCl₃ + O₂ (सूर्यप्रकाश) → 2COCl₂ (विषैली फॉस्जीन) + 2HCl", "बचाव: रंगीन बोतल में भरकर 1% एथेनॉल मिलाते हैं"),
            ],
        },
        {
            "name": "11. ऐल्कोहॉल, फीनॉल एवं ईथर",
            "formulas": [
                ("अम्ल उत्प्रेरित जलयोजन (मार्कोनीकॉफ नियम)", "CH₃-CH=CH₂ + H₂O (H⁺) → CH₃-CH(OH)-CH₃", "द्वितीयक ऐल्कोहॉल मुख्य उत्पाद"),
                ("हाइड्रोबोरेशन-ऑक्सीकरण (प्रति-मार्कोनीकॉफ)", "R-CH=CH₂ + (BH₃)₂ → H₂O₂ / OH⁻ → R-CH₂-CH₂-OH", "प्राथमिक (1°) ऐल्कोहॉल मुख्य उत्पाद"),
                ("ग्रिगनार्ड अभिकर्मक (RMgX) से 1°, 2°, 3° ऐल्कोहॉल", "HCHO → 1° ऐल्कोहॉल | अन्य ऐल्डिहाइड → 2° | कीटोन → 3°", "जल-अपघटन के पश्चात"),
                ("ल्यूकास परीक्षण (Lucas Test: सांद्र HCl + निर्जल ZnCl₂)", "3° ऐल्कोहॉल: तुरंत धुँधलापन | 2°: 5 मिनट में | 1°: कमरे के ताप पर कोई धुँधलापन नहीं", "1°, 2°, 3° ऐल्कोहॉल में विभेद हेतु"),
                ("विक्टर मेयर परीक्षण (Victor Meyer Test - RBC ट्रिक)", "1° ऐल्कोहॉल → लाल (Red) | 2° → नीला (Blue) | 3° → रंगहीन (Colourless)", "अत्यंत प्रसिद्ध बोर्ड परीक्षण"),
                ("ऐल्कोहॉल का अम्लीय निर्जलीकरण (Dehydration)", "C₂H₅OH (सांद्र H₂SO₄, 443 K) → CH₂=CH₂ + H₂O", "413 K पर डाइएथिल ईथर (C₂H₅-O-C₂H₅) बनता है"),
                ("क्यूमीन से फीनॉल निर्माण (Cumene Process)", "क्यूमीन + O₂ → क्यूमीन हाइड्रोपरॉक्साइड (H⁺) → फीनॉल + ऐसीटोन", "औद्योगिक निर्माण विधि"),
                ("कोल्बे अभिक्रिया (Kolbe's Reaction)", "फीनॉल + NaOH + CO₂ (400 K, 4-7 atm) → H⁺ → सैलिसिलिक अम्ल", "2-हाइड्रॉक्सीबेन्जोइक अम्ल बनता है"),
                ("राइमर-टीमन अभिक्रिया (Reimer-Tiemann Reaction)", "फीनॉल + CHCl₃ + जलीय NaOH → H⁺ → सैलिसिलैल्डिहाइड", "CCl₄ लेने पर सैलिसिलिक अम्ल बनता है"),
                ("फीनॉल का यशद चूर्ण (Zn Dust) से आसवन", "C₆H₅OH + Zn (Δ) → C₆H₆ (बेन्जीन) + ZnO", "फीनॉल का अपचयन"),
                ("विलियमसन ईथर संश्लेषण (Williamson Synthesis)", "R-X + R'-ONa → R-O-R' + NaX  (SN² क्रियाविधि)", "ऐल्किल हैलाइड (R-X) सदैव 1° होना चाहिए"),
                ("ईथर की HI के साथ अभिक्रिया (ज़ाइसेल विधि)", "R-O-R' + HI → R-I + R'-OH", "3° समूह होने पर SN¹ द्वारा 3° ऐल्किल आयोडाइड बनता है"),
            ],
        },
        {
            "name": "12. ऐल्डिहाइड, कीटोन एवं कार्बोक्सिलिक अम्ल",
            "formulas": [
                ("रोजेनमुंड अपचयन (Rosenmund Reduction)", "R-COCl + H₂ (Pd / BaSO₄, उबलती ज़ाइलीन) → R-CHO + HCl", "अम्ल क्लोराइड से ऐल्डिहाइड निर्माण"),
                ("स्टीफेन अभिक्रिया (Stephen Reaction)", "R-C≡N + SnCl₂ + HCl → R-CH=NH·HCl (H₃O⁺) → R-CHO", "नाइट्राइल से ऐल्डिहाइड निर्माण"),
                ("इटार्ड अभिक्रिया (Etard Reaction)", "C₆H₅CH₃ + CrO₂Cl₂ (CS₂) → H₃O⁺ → C₆H₅CHO (बेन्जैल्डिहाइड)", "टॉलूईन का नियंत्रित ऑक्सीकरण"),
                ("गाटरमान-कोच अभिक्रिया (Gattermann-Koch)", "C₆H₆ + CO + HCl (निर्जल AlCl₃ / CuCl) → C₆H₅CHO", "बेन्जीन से सीधे बेन्जैल्डिहाइड"),
                ("क्लीमेंसन अपचयन (Clemmensen Reduction)", ">C=O + 4[H] (Zn-Hg / सांद्र HCl) → >CH₂ + H₂O", "कार्बोनिल समूह का सीधे ऐल्केन में अपचयन"),
                ("वोल्फ-किश्नर अपचयन (Wolff-Kishner Reduction)", ">C=O + NH₂NH₂ → KOH / एथिलीन ग्लाइकॉल (Δ) → >CH₂ + N₂", "क्षारीय माध्यम में कार्बोनिल का अपचयन"),
                ("ऐल्डोल संघनन (Aldol Condensation)", "2CH₃CHO (तनु NaOH) → CH₃-CH(OH)-CH₂-CHO (Δ) → CH₃-CH=CH-CHO", "कम से कम एक α-हाइड्रोजन होना अनिवार्य"),
                ("कैनिजारो अभिक्रिया (Cannizzaro Reaction)", "2HCHO + सांद्र NaOH → CH₃OH + HCOONa (असमानुपातन)", "α-हाइड्रोजन विहीन ऐल्डिहाइड (HCHO, C₆H₅CHO) देते हैं"),
                ("टॉलेन परीक्षण (Tollens' Test - रजत दर्पण)", "R-CHO + 2[Ag(NH₃)₂]⁺ + 3OH⁻ → R-COO⁻ + 2Ag↓ + 4NH₃ + 2H₂O", "ऐल्डिहाइड देते हैं, कीटोन नहीं देते"),
                ("फेहलिंग परीक्षण (Fehling's Test)", "R-CHO + 2Cu²⁺ + 5OH⁻ → R-COO⁻ + Cu₂O↓ (लाल-भूरा) + 3H₂O", "ऐलिफैटिक ऐल्डिहाइड देते हैं, बेन्जैल्डिहाइड नहीं देता"),
                ("आयोडोफॉर्म परीक्षण (Iodoform Test)", "CH₃-CO- या CH₃-CH(OH)- समूह + I₂ + NaOH → CHI₃↓ (पीला अवक्षेप)", "एथेनॉल, एसीटैल्डिहाइड व मेथिल कीटोन देते हैं"),
                ("हैल-वोल्हार्ड-ज़ेलिंस्की अभिक्रिया (HVZ Reaction)", "R-CH₂-COOH + X₂ (लाल P) → H₂O → R-CH(X)-COOH", "α-हैलोकार्बोक्सिलिक अम्ल निर्माण"),
                ("कार्बोक्सिलिक अम्लों की अम्लीय प्रबलता नियम", "अम्लीय प्रबलता ∝ -I प्रभाव  तथा  ∝ 1 / (+I प्रभाव)", "CF₃COOH > CCl₃COOH > HCOOH > CH₃COOH"),
            ],
        },
        {
            "name": "13. ऐमीन",
            "formulas": [
                ("हॉफमान ब्रोमामाइड निम्नीकरण अभिक्रिया", "R-CONH₂ + Br₂ + 4NaOH → R-NH₂ + Na₂CO₃ + 2NaBr + 2H₂O", "एमाइड से एक कार्बन कम वाला 1° ऐमीन बनता है"),
                ("गैब्रिएल थैलिमाइड संश्लेषण (Gabriel Synthesis)", "थैलिमाइड + KOH + R-X → NaOH(aq) → R-NH₂ (शुद्ध 1° ऐलिफैटिक ऐमीन)", "ऐरोमैटिक ऐमीन (ऐनिलीन) इससे नहीं बन सकता"),
                ("कार्बिलऐमीन अभिक्रिया (आइसोसायनाइड परीक्षण)", "R-NH₂ + CHCl₃ + 3KOH (ऐल्को.) → R-NC (दुर्गंधयुक्त) + 3KCl + 3H₂O", "केवल प्राथमिक (1°) ऐमीन देते हैं (2° व 3° नहीं)"),
                ("हिंसबर्ग अभिकर्मक (Hinsberg Reagent) परीक्षण", "अभिकर्मक: बेन्जीन सल्फोनिल क्लोराइड (C₆H₅SO₂Cl)", "1° उत्पाद KOH में विलेय | 2° अविलेय | 3° अभिक्रिया नहीं करता"),
                ("ऐलिफैटिक ऐमीनों की क्षारीय प्रबलता (जलीय विलयन)", "मेथिल: (CH₃)₂NH > CH₃NH₂ > (CH₃)₃N > NH₃ (2° > 1° > 3°)", "एथिल: (C₂H₅)₂NH > (C₂H₅)₃N > C₂H₅NH₂ > NH₃ (2° > 3° > 1°)"),
                ("गैसीय प्रावस्था में ऐमीनों की क्षारीयता क्रम", "3° ऐमीन > 2° ऐमीन > 1° ऐमीन > NH₃", "केवल +I प्रभाव पर निर्भर"),
                ("डाइऐजोकरण अभिक्रिया (Diazotisation)", "C₆H₅NH₂ + NaNO₂ + 2HCl (273–278 K / 0–5°C) → C₆H₅N₂⁺Cl⁻ + NaCl + 2H₂O", "बेन्जीन डाइऐजोनियम क्लोराइड निर्माण"),
                ("युग्मन अभिक्रिया (Coupling Reaction - ऐजो रंजक)", "C₆H₅N₂⁺Cl⁻ + फीनॉल (OH⁻) → p-हाइड्रॉक्सीऐजोबेन्जीन (नारंगी रंजक)", "ऐनिलीन के साथ पीला रंजक बनता है"),
                ("ऐनिलीन का ब्रोमीनीकरण (Bromination)", "C₆H₅NH₂ + 3Br₂ (जल) → 2,4,6-ट्राइब्रोमोऐनिलीन (सफेद अवक्षेप)", "-NH₂ समूह अत्यधिक सक्रियणकारी है"),
            ],
        },
        {
            "name": "14. जैव-अणु",
            "formulas": [
                ("कार्बोहाइड्रेट का सामान्य सूत्र", "C_x(H₂O)_y", "ग्लूकोज व फ्रक्टोज दोनों का अणुसूत्र: C₆H₁₂O₆"),
                ("ग्लूकोज व फ्रक्टोज में क्रियात्मक समूह अंतर", "ग्लूकोज: ऐल्डोहेक्सोज (-CHO समूह) | फ्रक्टोज: कीटोहेक्सोज (>C=O समूह)", "ग्लूकोज में 4 किरैल (असममित) कार्बन होते हैं"),
                ("प्रमुख डाइसैकेराइड्स के जल-अपघटन उत्पाद", "सुक्रोज → ग्लूकोज + फ्रक्टोज | माल्टोज → 2 ग्लूकोज | लैक्टोज → ग्लूकोज + गैलैक्टोज", "सुक्रोज एक अनअपचायी शर्करा (Non-reducing) है"),
                ("स्टार्च के दो मुख्य घटक", "ऐमिलोज (15-20%, जल में विलेय) + ऐमिलोपेक्टिन (80-85%, जल में अविलेय)", "दोनों α-D-ग्लूकोज के बहुलक हैं"),
                ("पेप्टाइड बंध (Peptide Linkage)", "-CO-NH- बंध (दो α-ऐमीनो अम्लों के संघनन से निर्मित)", "प्रोटीन α-ऐमीनो अम्लों के बहुलक हैं"),
                ("ज़्विटर आयन (Zwitter Ion)", "H₃N⁺-CHR-COO⁻ (उभयधर्मी द्विध्रुवीय आयन)", "समविभव बिंदु (Isoelectric Point) पर कुल आवेश शून्य"),
                ("DNA व RNA के नाइट्रोजनी क्षारक", "DNA: A, G, C, T (थायमीन)  |  RNA: A, G, C, U (यूरैसिल)", "DNA में द्विकुंडली, RNA में एकल रज्जुक संरचना"),
                ("चारगाफ का क्षार युग्मन नियम (Chargaff's Rule)", "A = T (2 हाइड्रोजन बंध)  तथा  G ≡ C (3 हाइड्रोजन बंध)", "(A + G) / (T + C) = 1"),
                ("जल व वसा में विलेय विटामिन", "जल में विलेय: विटामिन B समूह व C | वसा में विलेय: विटामिन A, D, E, K", "विटामिन B₁₂ में कोबाल्ट (Co) धातु पाई जाती है"),
                ("विटामिनों के रासायनिक नाम व कमी से रोग", "A (रेटिनॉल - रतौंधी) | B₁ (थायमीन - बेरी-बेरी) | C (एस्कॉर्बिक अम्ल - स्कर्वी) | D (कैल्सिफेरॉल - रिकेट्स)", "K (फिलोक्विनोन - रक्त का थक्का न जमना)"),
            ],
        },
        {
            "name": "15. बहुलक",
            "formulas": [
                ("पॉलिथीन (Polythene) का एकलक व प्रकार", "एकलक: एथीन (CH₂=CH₂)  →  -(CH₂-CH₂)ₙ-", "योगात्मक समबहुलक (Ziegler-Natta उत्प्रेरक से HDPE)"),
                ("टेफ्लॉन (PTFE - नॉन-स्टिक बर्तन)", "एकलक: टेट्राफ्लोरोएथीन (CF₂=CF₂)  →  -(CF₂-CF₂)ₙ-", "ऊष्मा व रसायन प्रतिरोधी"),
                ("PVC (पॉलिवाइनिल क्लोराइड)", "एकलक: वाइनिल क्लोराइड (CH₂=CHCl)  →  -(CH₂-CHCl)ₙ-", "पाइप व रेनकोट निर्माण में"),
                ("ऑरलॉन / एक्रिलन (PAN)", "एकलक: एक्रिलोनाइट्राइल (CH₂=CH-CN)", "कृत्रिम ऊन (स्वेटर) निर्माण में"),
                ("प्राकृतिक रबर (Natural Rubber)", "एकलक: आइसोप्रीन (2-मेथिल-1,3-ब्यूटाडाइन)", "सिस-1,4-पॉलिआइसोप्रीन; वल्कनीकरण में 3-5% सल्फर मिलाते हैं"),
                ("निओप्रीन (संश्लेषित रबर)", "एकलक: क्लोरोप्रीन (2-क्लोरो-1,3-ब्यूटाडाइन)", "तेल व पेट्रोल प्रतिरोधी रबर"),
                ("ब्यूना-S (Buna-S) सहबहुलक", "एकलक: 1,3-ब्यूटाडाइन + स्टाइरीन", "टायर व जूते के सोल निर्माण में"),
                ("ब्यूना-N (Buna-N) सहबहुलक", "एकलक: 1,3-ब्यूटाडाइन + एक्रिलोनाइट्राइल", "तेल सील व टैंक अस्तर निर्माण में"),
                ("नायलॉन-6,6 (Nylon-6,6 - पॉलिऐमाइड)", "एकलक: हेक्सामेथिलीनडाइऐमीन + ऐडिपिक अम्ल", "संघनन बहुलक (ऐमाइड बंध -CONH-)"),
                ("नायलॉन-6 (Nylon-6)", "एकलक: कैप्रोलैक्टम (Caprolactam)", "रस्सियाँ व टायर कॉर्ड बनाने में"),
                ("टेरिलीन / डेक्रॉन (Terylene - पॉलिएस्टर)", "एकलक: एथिलीन ग्लाइकॉल + टेरेफ्थैलिक अम्ल", "एस्टर बंध (-COO-) युक्त संघनन बहुलक"),
                ("बैकेलाइट (Bakelite - तापद्रढ़ बहुलक)", "एकलक: फीनॉल + फॉर्मेल्डिहाइड (HCHO)", "विद्युत स्विच व कंघी निर्माण (तिर्यकबद्ध बहुलक)"),
                ("जैव-निम्नीकरणीय बहुलक (Biodegradable)", "PHBV (3-हाइड्रॉक्सीब्यूटाइरेट + 3-हाइड्रॉक्सीवैलेरेट) तथा नायलॉन-2-नायलॉन-6", "पर्यावरण अनुकूल बहुलक"),
            ],
        },
        {
            "name": "16. दैनिक जीवन में रसायन",
            "formulas": [
                ("प्रशांतक (Tranquilizers - तनाव निवारक)", "इक्वैनिल, वेरोनल, ल्यूमिनल, क्लोरडाइएजेपॉक्साइड", "केंद्रीय तंत्रिका तंत्र पर कार्य करते हैं"),
                ("पीड़ाहारी (Analgesics - दर्द निवारक)", "अस्वापक: ऐस्पिरिन, पैरासिटामोल | स्वापक (Narcotic): मॉर्फीन, हेरोइन, कोडीन", "ऐस्पिरिन = ऐसीटिल सैलिसिलिक अम्ल (ज्वरनाशी भी है)"),
                ("प्रतिजैविक (Antibiotics)", "जीवाणुनाशी: पेनिसिलिन, ऑफ्लॉक्सासिन | जीवाणुनिरोधी: टेट्रासाइक्लिन, क्लोरैम्फेनिकॉल", "प्रथम प्रतिजैविक पेनिसिलिन (अलेक्जेंडर फ्लेमिंग)"),
                ("पूतिरोधी (Antiseptics - जीवित ऊतकों पर)", "डेटॉल (क्लोरोजाइलिनॉल + टर्पिनिऑल), बिथियोनल (साबुन में), टिंक्चर आयोडीन (2-3% I₂)", "घाव साफ करने हेतु सुरक्षित"),
                ("विसंक्रामी (Disinfectants - निर्जीव वस्तुओं पर)", "1% फीनॉल विलयन, क्लोरीन (0.2–0.4 ppm), SO₂", "0.2% फीनॉल पूतिरोधी, जबकि 1% फीनॉल विसंक्रामी है"),
                ("प्रतिअम्ल (Antacids - एसिडिटी निवारक)", "रैनिटिडीन (ज़ैन्टैक), सिमेटिडीन, ओमेप्राजोल, Mg(OH)₂", "आमाशय में अत्यधिक HCl को उदासीन करते हैं"),
                ("प्रतिहिस्टैमिन (Antihistamines - एलर्जी निवारक)", "ब्रोमफेनिरामिन (डिमेंटैप), टर्फेनाडिन (सेल्डेन)", "हिस्टैमिन के प्रभाव को रोकते हैं"),
                ("कृत्रिम मधुरक (Artificial Sweeteners)", "सैकेरीन (550 गुना मीठा), ऐस्पार्टेम (100 गुना - केवल ठंडे खाद्य में), सुक्रालोज (600 गुना), ऐलिटेम (2000 गुना)", "मधुमेह रोगियों हेतु शून्य कैलोरी विकल्प"),
                ("खाद्य परिरक्षक (Food Preservatives)", "सोडियम बेन्जोएट (C₆H₅COONa), साधारण नमक, शर्करा, वनस्पति तेल", "भोजन को सूक्ष्मजीवों से खराब होने से बचाते हैं"),
                ("साबुन व संश्लेषित अपमार्जक में अंतर", "साबुन कठोर जल (Ca²⁺, Mg²⁺) में अवक्षेप बनाता है; अपमार्जक कठोर जल में भी झाग देता है", "धनायनिक अपमार्जक: सेटिलट्राइमेथिलअमोनियम ब्रोमाइड"),
            ],
        },
    ],
}
                ("कार्बोक्सिलिक अम्लों की अम्लीय प्रबलता नियम", "अम्लीय प्रबलता ∝ -I प्रभाव  तथा  ∝ 1 / (+I प्रभाव)", "CF₃COOH > CCl₃COOH > HCOOH > CH₃COOH"),
            ],
        },
        {
            "name": "13. ऐमीन",
            "formulas": [
                ("हॉफमान ब्रोमामाइड निम्नीकरण अभिक्रिया", "R-CONH₂ + Br₂ + 4NaOH → R-NH₂ + Na₂CO₃ + 2NaBr + 2H₂O", "एमाइड से एक कार्बन कम वाला 1° ऐमीन बनता है"),
                ("गैब्रिएल थैलिमाइड संश्लेषण (Gabriel Synthesis)", "थैलिमाइड + KOH + R-X → NaOH(aq) → R-NH₂ (शुद्ध 1° ऐलिफैटिक ऐमीन)", "ऐरोमैटिक ऐमीन (ऐनिलीन) इससे नहीं बन सकता"),
                ("कार्बिलऐमीन अभिक्रिया (आइसोसायनाइड परीक्षण)", "R-NH₂ + CHCl₃ + 3KOH (ऐल्को.) → R-NC (दुर्गंधयुक्त) + 3KCl + 3H₂O", "केवल प्राथमिक (1°) ऐमीन देते हैं (2° व 3° नहीं)"),
                ("हिंसबर्ग अभिकर्मक (Hinsberg Reagent) परीक्षण", "अभिकर्मक: बेन्जीन सल्फोनिल क्लोराइड (C₆H₅SO₂Cl)", "1° उत्पाद KOH में विलेय | 2° अविलेय | 3° अभिक्रिया नहीं करता"),
                ("ऐलिफैटिक ऐमीनों की क्षारीय प्रबलता (जलीय विलयन)", "मेथिल: (CH₃)₂NH > CH₃NH₂ > (CH₃)₃N > NH₃ (2° > 1° > 3°)", "एथिल: (C₂H₅)₂NH > (C₂H₅)₃N > C₂H₅NH₂ > NH₃ (2° > 3° > 1°)"),
                ("गैसीय प्रावस्था में ऐमीनों की क्षारीयता क्रम", "3° ऐमीन > 2° ऐमीन > 1° ऐमीन > NH₃", "केवल +I प्रभाव पर निर्भर"),
                ("डाइऐजोकरण अभिक्रिया (Diazotisation)", "C₆H₅NH₂ + NaNO₂ + 2HCl (273–278 K / 0–5°C) → C₆H₅N₂⁺Cl⁻ + NaCl + 2H₂O", "बेन्जीन डाइऐजोनियम क्लोराइड निर्माण"),
                ("युग्मन अभिक्रिया (Coupling Reaction - ऐजो रंजक)", "C₆H₅N₂⁺Cl⁻ + फीनॉल (OH⁻) → p-हाइड्रॉक्सीऐजोबेन्जीन (नारंगी रंजक)", "ऐनिलीन के साथ पीला रंजक बनता है"),
                ("ऐनिलीन का ब्रोमीनीकरण (Bromination)", "C₆H₅NH₂ + 3Br₂ (जल) → 2,4,6-ट्राइब्रोमोऐनिलीन (सफेद अवक्षेप)", "-NH₂ समूह अत्यधिक सक्रियणकारी है"),
            ],
        },
        {
            "name": "14. जैव-अणु",
            "formulas": [
                ("कार्बोहाइड्रेट का सामान्य सूत्र", "C_x(H₂O)_y", "ग्लूकोज व फ्रक्टोज दोनों का अणुसूत्र: C₆H₁₂O₆"),
                ("ग्लूकोज व फ्रक्टोज में क्रियात्मक समूह अंतर", "ग्लूकोज: ऐल्डोहेक्सोज (-CHO समूह) | फ्रक्टोज: कीटोहेक्सोज (>C=O समूह)", "ग्लूकोज में 4 किरैल (असममित) कार्बन होते हैं"),
                ("प्रमुख डाइसैकेराइड्स के जल-अपघटन उत्पाद", "सुक्रोज → ग्लूकोज + फ्रक्टोज | माल्टोज → 2 ग्लूकोज | लैक्टोज → ग्लूकोज + गैलैक्टोज", "सुक्रोज एक अनअपचायी शर्करा (Non-reducing) है"),
                ("स्टार्च के दो मुख्य घटक", "ऐमिलोज (15-20%, जल में विलेय) + ऐमिलोपेक्टिन (80-85%, जल में अविलेय)", "दोनों α-D-ग्लूकोज के बहुलक हैं"),
                ("पेप्टाइड बंध (Peptide Linkage)", "-CO-NH- बंध (दो α-ऐमीनो अम्लों के संघनन से निर्मित)", "प्रोटीन α-ऐमीनो अम्लों के बहुलक हैं"),
                ("ज़्विटर आयन (Zwitter Ion)", "H₃N⁺-CHR-COO⁻ (उभयधर्मी द्विध्रुवीय आयन)", "समविभव बिंदु (Isoelectric Point) पर कुल आवेश शून्य"),
                ("DNA व RNA के नाइट्रोजनी क्षारक", "DNA: A, G, C, T (थायमीन)  |  RNA: A, G, C, U (यूरैसिल)", "DNA में द्विकुंडली, RNA में एकल रज्जुक संरचना"),
                ("चारगाफ का क्षार युग्मन नियम (Chargaff's Rule)", "A = T (2 हाइड्रोजन बंध)  तथा  G ≡ C (3 हाइड्रोजन बंध)", "(A + G) / (T + C) = 1"),
                ("जल व वसा में विलेय विटामिन", "जल में विलेय: विटामिन B समूह व C | वसा में विलेय: विटामिन A, D, E, K", "विटामिन B₁₂ में कोबाल्ट (Co) धातु पाई जाती है"),
                ("विटामिनों के रासायनिक नाम व कमी से रोग", "A (रेटिनॉल - रतौंधी) | B₁ (थायमीन - बेरी-बेरी) | C (एस्कॉर्बिक अम्ल - स्कर्वी) | D (कैल्सिफेरॉल - रिकेट्स)", "K (फिलोक्विनोन - रक्त का थक्का न जमना)"),
            ],
        },
        {
            "name": "15. बहुलक",
            "formulas": [
                ("पॉलिथीन (Polythene) का एकलक व प्रकार", "एकलक: एथीन (CH₂=CH₂)  →  -(CH₂-CH₂)ₙ-", "योगात्मक समबहुलक (Ziegler-Natta उत्प्रेरक से HDPE)"),
                ("टेफ्लॉन (PTFE - नॉन-स्टिक बर्तन)", "एकलक: टेट्राफ्लोरोएथीन (CF₂=CF₂)  →  -(CF₂-CF₂)ₙ-", "ऊष्मा व रसायन प्रतिरोधी"),
                ("PVC (पॉलिवाइनिल क्लोराइड)", "एकलक: वाइनिल क्लोराइड (CH₂=CHCl)  →  -(CH₂-CHCl)ₙ-", "पाइप व रेनकोट निर्माण में"),
                ("ऑरलॉन / एक्रिलन (PAN)", "एकलक: एक्रिलोनाइट्राइल (CH₂=CH-CN)", "कृत्रिम ऊन (स्वेटर) निर्माण में"),
                ("प्राकृतिक रबर (Natural Rubber)", "एकलक: आइसोप्रीन (2-मेथिल-1,3-ब्यूटाडाइन)", "सिस-1,4-पॉलिआइसोप्रीन; वल्कनीकरण में 3-5% सल्फर मिलाते हैं"),
                ("निओप्रीन (संश्लेषित रबर)", "एकलक: क्लोरोप्रीन (2-क्लोरो-1,3-ब्यूटाडाइन)", "तेल व पेट्रोल प्रतिरोधी रबर"),
                ("ब्यूना-S (Buna-S) सहबहुलक", "एकलक: 1,3-ब्यूटाडाइन + स्टाइरीन", "टायर व जूते के सोल निर्माण में"),
                ("ब्यूना-N (Buna-N) सहबहुलक", "एकलक: 1,3-ब्यूटाडाइन + एक्रिलोनाइट्राइल", "तेल सील व टैंक अस्तर निर्माण में"),
                ("नायलॉन-6,6 (Nylon-6,6 - पॉलिऐमाइड)", "एकलक: हेक्सामेथिलीनडाइऐमीन + ऐडिपिक अम्ल", "संघनन बहुलक (ऐमाइड बंध -CONH-)"),
                ("नायलॉन-6 (Nylon-6)", "एकलक: कैप्रोलैक्टम (Caprolactam)", "रस्सियाँ व टायर कॉर्ड बनाने में"),
                ("टेरिलीन / डेक्रॉन (Terylene - पॉलिएस्टर)", "एकलक: एथिलीन ग्लाइकॉल + टेरेफ्थैलिक अम्ल", "एस्टर बंध (-COO-) युक्त संघनन बहुलक"),
                ("बैकेलाइट (Bakelite - तापद्रढ़ बहुलक)", "एकलक: फीनॉल + फॉर्मेल्डिहाइड (HCHO)", "विद्युत स्विच व कंघी निर्माण (तिर्यकबद्ध बहुलक)"),
                ("जैव-निम्नीकरणीय बहुलक (Biodegradable)", "PHBV (3-हाइड्रॉक्सीब्यूटाइरेट + 3-हाइड्रॉक्सीवैलेरेट) तथा नायलॉन-2-नायलॉन-6", "पर्यावरण अनुकूल बहुलक"),
            ],
        },
        {
            "name": "16. दैनिक जीवन में रसायन",
            "formulas": [
                ("प्रशांतक (Tranquilizers - तनाव निवारक)", "इक्वैनिल, वेरोनल, ल्यूमिनल, क्लोरडाइएजेपॉक्साइड", "केंद्रीय तंत्रिका तंत्र पर कार्य करते हैं"),
                ("पीड़ाहारी (Analgesics - दर्द निवारक)", "अस्वापक: ऐस्पिरिन, पैरासिटामोल | स्वापक (Narcotic): मॉर्फीन, हेरोइन, कोडीन", "ऐस्पिरिन = ऐसीटिल सैलिसिलिक अम्ल (ज्वरनाशी भी है)"),
                ("प्रतिजैविक (Antibiotics)", "जीवाणुनाशी: पेनिसिलिन, ऑफ्लॉक्सासिन | जीवाणुनिरोधी: टेट्रासाइक्लिन, क्लोरैम्फेनिकॉल", "प्रथम प्रतिजैविक पेनिसिलिन (अलेक्जेंडर फ्लेमिंग)"),
                ("पूतिरोधी (Antiseptics - जीवित ऊतकों पर)", "डेटॉल (क्लोरोजाइलिनॉल + टर्पिनिऑल), बिथियोनल (साबुन में), टिंक्चर आयोडीन (2-3% I₂)", "घाव साफ करने हेतु सुरक्षित"),
                ("विसंक्रामी (Disinfectants - निर्जीव वस्तुओं पर)", "1% फीनॉल विलयन, क्लोरीन (0.2–0.4 ppm), SO₂", "0.2% फीनॉल पूतिरोधी, जबकि 1% फीनॉल विसंक्रामी है"),
                ("प्रतिअम्ल (Antacids - एसिडिटी निवारक)", "रैनिटिडीन (ज़ैन्टैक), सिमेटिडीन, ओमेप्राजोल, Mg(OH)₂", "आमाशय में अत्यधिक HCl को उदासीन करते हैं"),
                ("प्रतिहिस्टैमिन (Antihistamines - एलर्जी निवारक)", "ब्रोमफेनिरामिन (डिमेंटैप), टर्फेनाडिन (सेल्डेन)", "हिस्टैमिन के प्रभाव को रोकते हैं"),
                ("कृत्रिम मधुरक (Artificial Sweeteners)", "सैकेरीन (550 गुना मीठा), ऐस्पार्टेम (100 गुना - केवल ठंडे खाद्य में), सुक्रालोज (600 गुना), ऐलिटेम (2000 गुना)", "मधुमेह रोगियों हेतु शून्य कैलोरी विकल्प"),
                ("खाद्य परिरक्षक (Food Preservatives)", "सोडियम बेन्जोएट (C₆H₅COONa), साधारण नमक, शर्करा, वनस्पति तेल", "भोजन को सूक्ष्मजीवों से खराब होने से बचाते हैं"),
                ("साबुन व संश्लेषित अपमार्जक में अंतर", "साबुन कठोर जल (Ca²⁺, Mg²⁺) में अवक्षेप बनाता है; अपमार्जक कठोर जल में भी झाग देता है", "धनायनिक अपमार्जक: सेटिलट्राइमेथिलअमोनियम ब्रोमाइड"),
            ],
        },
    ],
}
# ================= COMPLETE MATHEMATICS FORMULAS (ALL 13 CHAPTERS) =================
MATH_DATA = {
    "title": "📐 Mathematics (गणित)",
    "chapters": [
        {
            "name": "1. संबंध एवं फलन",
            "formulas": [
                ("समुच्चय A से B में कुल संबंधों की संख्या", "N = 2^(m × n)", "जहाँ n(A) = m तथा n(B) = n"),
                ("समुच्चय A (n अवयव) पर स्वतुल्य संबंधों की संख्या", "N_reflexive = 2^(n² - n)", "प्रत्येक a ∈ A के लिए (a, a) ∈ R"),
                ("समुच्चय A (n अवयव) पर सममित संबंधों की संख्या", "N_symmetric = 2^[n(n + 1) / 2]", "(a, b) ∈ R ⇒ (b, a) ∈ R"),
                ("तुल्यता संबंध (Equivalence Relation) की शर्त", "स्वतुल्य + सममित + संक्रामक तीनों होना अनिवार्य", "समुच्चय को असंयुक्त तुल्यता वर्गों में बाँटता है"),
                ("एकैकी फलन (One-One / Injective) की शर्त व संख्या", "f(x₁) = f(x₂) ⇒ x₁ = x₂ | कुल संख्या = ⁿP_m (n ≥ m)", "जहाँ n(A) = m तथा n(B) = n"),
                ("आच्छादक फलन (Onto / Surjective) की शर्त", "परिसर (Range) = सहप्रांत (Codomain)", "प्रत्येक y ∈ B का पूर्व-प्रतिबिंब A में विद्यमान"),
                ("एकैकी आच्छादक (Bijective) फलनों की कुल संख्या", "कुल Bijective फलन = n! (जब n(A) = n(B) = n)", "केवल Bijective फलन ही व्युत्क्रमणीय होते हैं"),
                ("फलनों का संयोजन (Composition of Functions)", "(gof)(x) = g(f(x))  तथा  (fog)(x) = f(g(x))", "सामान्यतः fog ≠ gof (क्रमविनिमेय नहीं)"),
                ("व्युत्क्रमणीय फलन (Invertible Function) गुणधर्म", "fog = I_Y,  gof = I_X  तथा  (gof)⁻¹ = f⁻¹ o g⁻¹", "उत्क्रमण नियम (Reversal Law)"),
                ("द्विआधारी संक्रियाओं (Binary Operations) की कुल संख्या", "कुल संक्रियाएँ = n^(n²)", "A × A → A पर परिभाषित फलन"),
                ("क्रमविनिमेय द्विआधारी संक्रियाओं की कुल संख्या", "क्रमविनिमेय संक्रियाएँ = n^[n(n + 1) / 2]", "a * b = b * a"),
                ("तत्समक अवयव (e) व प्रतिलोम अवयव (a⁻¹)", "a * e = e * a = a  तथा  a * a⁻¹ = a⁻¹ * a = e", "तत्समक अवयव सदैव अद्वितीय होता है"),
            ],
        },
        {
            "name": "2. प्रतिलोम त्रिकोणमितीय फलन",
            "formulas": [
                ("sin⁻¹x का प्रांत व मुख्य मान परिसर", "प्रांत: [-1, 1]  |  मुख्य परिसर: [-π/2, π/2]", "sin(sin⁻¹x) = x, x ∈ [-1, 1]"),
                ("cos⁻¹x का प्रांत व मुख्य मान परिसर", "प्रांत: [-1, 1]  |  मुख्य परिसर: [0, π]", "cos(cos⁻¹x) = x, x ∈ [-1, 1]"),
                ("tan⁻¹x का प्रांत व मुख्य मान परिसर", "प्रांत: R (-∞, ∞)  |  मुख्य परिसर: (-π/2, π/2)", "विवृत अंतराल (Open Interval)"),
                ("cot⁻¹x का प्रांत व मुख्य मान परिसर", "प्रांत: R (-∞, ∞)  |  मुख्य परिसर: (0, π)", "विवृत अंतराल (0, π)"),
                ("sec⁻¹x का प्रांत व मुख्य मान परिसर", "प्रांत: R - (-1, 1)  |  मुख्य परिसर: [0, π] - {π/2}", "|x| ≥ 1"),
                ("cosec⁻¹x का प्रांत व मुख्य मान परिसर", "प्रांत: R - (-1, 1)  |  मुख्य परिसर: [-π/2, π/2] - {0}", "|x| ≥ 1"),
                ("व्युत्क्रम गुणधर्म (Reciprocal Properties)", "sin⁻¹(1/x) = cosec⁻¹x,  cos⁻¹(1/x) = sec⁻¹x,  tan⁻¹(1/x) = cot⁻¹x", "x ≥ 1 या x > 0 के लिए"),
                ("ऋणात्मक कोण गुणधर्म (समूह-1: सीधे माइनस)", "sin⁻¹(-x) = -sin⁻¹x,  tan⁻¹(-x) = -tan⁻¹x,  cosec⁻¹(-x) = -cosec⁻¹x", "अत्यंत महत्वपूर्ण बोर्ड सूत्र"),
                ("ऋणात्मक कोण गुणधर्म (समूह-2: π में से घटता है)", "cos⁻¹(-x) = π - cos⁻¹x,  sec⁻¹(-x) = π - sec⁻¹x,  cot⁻¹(-x) = π - cot⁻¹x", "cos⁻¹(-1/2) = π - π/3 = 2π/3"),
                ("पूरक कोण योग सूत्र (Complementary Identities)", "sin⁻¹x + cos⁻¹x = π/2,  tan⁻¹x + cot⁻¹x = π/2,  sec⁻¹x + cosec⁻¹x = π/2", "सभी वैध x के लिए सत्य"),
                ("tan⁻¹x + tan⁻¹y का सूत्र", "tan⁻¹x + tan⁻¹y = tan⁻¹[(x + y) / (1 - xy)]", "शर्त: xy < 1 (यदि xy > 1 तो π जोड़ें)"),
                ("tan⁻¹x - tan⁻¹y का सूत्र", "tan⁻¹x - tan⁻¹y = tan⁻¹[(x - y) / (1 + xy)]", "शर्त: xy > -1"),
                ("sin⁻¹x ± sin⁻¹y का सूत्र", "sin⁻¹x ± sin⁻¹y = sin⁻¹[x√(1 - y²) ± y√(1 - x²)]", "महत्वपूर्ण रूपान्तरण सूत्र"),
                ("cos⁻¹x ± cos⁻¹y का सूत्र", "cos⁻¹x ± cos⁻¹y = cos⁻¹[xy ∓ √(1 - x²)√(1 - y²)]", "+ में - तथा - में + चिह्न आता है"),
                ("2tan⁻¹x के तीनों रूप (sin⁻¹, cos⁻¹, tan⁻¹)", "2tan⁻¹x = sin⁻¹[2x/(1+x²)] = cos⁻¹[(1-x²)/(1+x²)] = tan⁻¹[2x/(1-x²)]", "सबसे अधिक पूछा जाने वाला सूत्र"),
                ("2sin⁻¹x व 2cos⁻¹x के सूत्र", "2sin⁻¹x = sin⁻¹(2x√(1 - x²))  |  2cos⁻¹x = cos⁻¹(2x² - 1)", "त्रिकोणमितीय प्रतिस्थापन में उपयोगी"),
                ("3sin⁻¹x, 3cos⁻¹x व 3tan⁻¹x के सूत्र", "3sin⁻¹x = sin⁻¹(3x - 4x³) | 3cos⁻¹x = cos⁻¹(4x³ - 3x) | 3tan⁻¹x = tan⁻¹[(3x-x³)/(1-3x²)]", "x = sinθ, cosθ, tanθ रखकर सिद्ध होता है"),
            ],
        },
        {
            "name": "3. आव्यूह (Matrices)",
            "formulas": [
                ("m × n कोटि के आव्यूह में कुल अवयव", "कुल अवयव = m × n", "m = पंक्तियाँ (Rows), n = स्तंभ (Columns)"),
                ("आव्यूह गुणन की शर्त (Matrix Multiplication)", "A_(m×n) · B_(n×p) = (AB)_(m×p)", "A के स्तंभ = B की पंक्तियाँ; सामान्यतः AB ≠ BA"),
                ("परिवर्त आव्यूह (Transpose A') के गुणधर्म", "(A')' = A,  (A ± B)' = A' ± B',  (kA)' = kA'", "पंक्तियों व स्तंभों को परस्पर बदलने पर प्राप्त"),
                ("गुणनफल के परिवर्त का उत्क्रमण नियम", "(A B)' = B' A'", "Reversal Law of Transpose"),
                ("सममित आव्यूह (Symmetric Matrix) की शर्त", "A' = A  अर्थात  a_ij = a_ji", "केवल वर्ग आव्यूहों के लिए परिभाषित"),
                ("विषम सममित आव्यूह (Skew-Symmetric Matrix)", "A' = -A  अर्थात  a_ij = -a_ji", "सभी मुख्य विकर्ण अवयव शून्य (a_ii = 0) होते हैं"),
                ("वर्ग आव्यूह का सममित व विषम सममित में विघटन", "A = ½(A + A') + ½(A - A')", "½(A + A') सममित तथा ½(A - A') विषम सममित है"),
                ("लांबिक आव्यूह (Orthogonal Matrix) की शर्त", "A · A' = A' · A = I  ⇒  A⁻¹ = A'", "|A| = ±1 होता है"),
                ("व्युत्क्रमणीय आव्यूह (Invertible Matrix) गुणधर्म", "A A⁻¹ = A⁻¹ A = I,  (A⁻¹)⁻¹ = A,  (A')⁻¹ = (A⁻¹)'", "व्युत्क्रम सदैव अद्वितीय होता है"),
                ("दो आव्यूहों के गुणनफल का व्युत्क्रम", "(A B)⁻¹ = B⁻¹ A⁻¹", "Reversal Law of Inverse"),
            ],
        },
        {
            "name": "4. सारणिक (Determinants)",
            "formulas": [
                ("2 × 2 सारणिक का प्रसार", "|a  b; c  d| = a d - b c", "मुख्य विकर्ण गुणनफल - पश्च विकर्ण गुणनफल"),
                ("अदिश गुणन का सारणिक (n कोटि का वर्ग आव्यूह)", "|k A| = kⁿ |A|", "उदाहरण: 3×3 आव्यूह हेतु |2A| = 2³|A| = 8|A|"),
                ("सारणिक के मुख्य गुणधर्म", "|A B| = |A| |B|,  |A'| = |A|,  |Aⁿ| = |A|ⁿ", "दो पंक्तियाँ/स्तंभ समान होने पर |A| = 0"),
                ("तीन शीर्षों वाले त्रिभुज का क्षेत्रफल", "Δ = ½ |x₁(y₂ - y₃) + x₂(y₃ - y₁) + x₃(y₁ - y₂)|", "क्षेत्रफल सदैव धनात्मक लिया जाता है"),
                ("तीन बिंदुओं के संरेख (Collinear) होने की शर्त", "Δ = 0  ⇒  |x₁ y₁ 1; x₂ y₂ 1; x₃ y₃ 1| = 0", "तीनों बिंदु एक ही सरल रेखा पर स्थित"),
                ("उपसारणिक (M_ij) व सहखंड (A_ij) में संबंध", "A_ij = (-1)^(i + j) M_ij", "यदि i+j सम तो +M_ij, विषम तो -M_ij"),
                ("सहखंडज आव्यूह (Adjoint of Matrix A)", "adj(A) = [A_ij]'", "सहखंडों के आव्यूह का परिवर्त"),
                ("सहखंडज आव्यूह का मूलभूत प्रमेय", "A · (adj A) = (adj A) · A = |A| I", "अत्यंत महत्वपूर्ण प्रमेय"),
                ("सहखंडज सारणिक के शॉर्टकट सूत्र (n कोटि हेतु)", "|adj A| = |A|^(n - 1)  तथा  adj(adj A) = |A|^(n - 2) A", "3×3 आव्यूह हेतु |adj A| = |A|²"),
                ("व्युत्क्रम आव्यूह (A⁻¹) का सूत्र", "A⁻¹ = (1 / |A|) · adj(A)", "अस्तित्व की शर्त: |A| ≠ 0 (व्युत्क्रमणीय आव्यूह)"),
                ("व्युत्क्रम का सारणिक (|A⁻¹|)", "|A⁻¹| = 1 / |A| = |A|⁻¹", "|A| ≠ 0"),
                ("आव्यूह विधि से रैखिक समीकरण निकाय (AX = B) का हल", "X = A⁻¹ B  (जब |A| ≠ 0 तो अद्वितीय हल)", "यदि |A| = 0 व (adj A)B ≠ 0 तो कोई हल नहीं (असंगत)"),
            ],
        },
        {
            "name": "5. सांतत्य तथा अवकलनीयता",
            "formulas": [
                ("बिंदु x = a पर फलन के सांतत्य (Continuity) की शर्त", "lim(x→a⁻) f(x) = lim(x→a⁺) f(x) = f(a)", "LHL = RHL = फलन का मान"),
                ("अवकलनीयता (Differentiability) की शर्त", "LHD at a = RHD at a", "प्रत्येक अवकलनीय फलन संतत होता है (विलोम सदैव सत्य नहीं)"),
                ("बीजगणितीय व चरघातांकी अवकलन सूत्र", "d(xⁿ)/dx = n x^(n-1),  d(eˣ)/dx = eˣ,  d(aˣ)/dx = aˣ log_e a", "d(अचर)/dx = 0,  d(√x)/dx = 1 / (2√x)"),
                ("लघुगणकीय फलनों का अवकलन", "d(log_e x)/dx = 1 / x  |  d(log_a x)/dx = 1 / (x log_e a)", "x > 0 के लिए परिभाषित"),
                ("त्रिकोणमितीय फलनों का अवकलन (sin, cos, tan)", "d(sin x)/dx = cos x,  d(cos x)/dx = -sin x,  d(tan x)/dx = sec²x", "'c' से शुरू होने वाले फलनों का अवकलज ऋणात्मक होता है"),
                ("त्रिकोणमितीय फलनों का अवकलन (cot, sec, cosec)", "d(cot x)/dx = -cosec²x,  d(sec x)/dx = sec x tan x,  d(cosec x)/dx = -cosec x cot x", "मानक बोर्ड सूत्र"),
                ("प्रतिलोम फलन अवकलन (sin⁻¹x व cos⁻¹x)", "d(sin⁻¹x)/dx = 1 / √(1 - x²)  |  d(cos⁻¹x)/dx = -1 / √(1 - x²)", "|x| < 1"),
                ("प्रतिलोम फलन अवकलन (tan⁻¹x व cot⁻¹x)", "d(tan⁻¹x)/dx = 1 / (1 + x²)  |  d(cot⁻¹x)/dx = -1 / (1 + x²)", "सभी x ∈ R के लिए"),
                ("प्रतिलोम फलन अवकलन (sec⁻¹x व cosec⁻¹x)", "d(sec⁻¹x)/dx = 1 / (|x|√(x² - 1))  |  d(cosec⁻¹x)/dx = -1 / (|x|√(x² - 1))", "|x| > 1"),
                ("गुणन नियम (Product Rule - Leibniz)", "d(u · v)/dx = u (dv/dx) + v (du/dx)", "दो फलनों के गुणनफल का अवकलन"),
                ("भागफल नियम (Quotient Rule)", "d(u / v)/dx = [v (du/dx) - u (dv/dx)] / v²", "v ≠ 0"),
                ("शृंखला नियम (Chain Rule)", "dy / dx = (dy / du) · (du / dx)", "संयुक्त फलनों (Composite Functions) हेतु"),
                ("प्राचलिक फलनों (Parametric) का अवकलन", "dy / dx = (dy / dt) / (dx / dt)", "द्वितीय कोटि: d²y/dx² = [d/dt(dy/dx)] · (dt/dx)"),
                ("लघुगणकीय अवकलन (y = [f(x)]^g(x))", "log y = g(x) log f(x)  ⇒  (1/y)dy/dx = g'(x)log f(x) + g(x)f'(x)/f(x)", "चर की घात चर होने पर उपयोगी"),
                ("रोले का प्रमेय (Rolle's Theorem)", "f(a) = f(b) होने पर कम से कम एक c ∈ (a, b) जहाँ f'(c) = 0", "[a, b] में संतत व (a, b) में अवकलनीय"),
                ("लैग्रांज माध्यमान प्रमेय (Mean Value Theorem)", "f'(c) = [f(b) - f(a)] / (b - a),  c ∈ (a, b)", "स्पर्श रेखा जीवा के समानांतर होती है"),
            ],
        },
        {
            "name": "6. अवकलज के अनुप्रयोग",
            "formulas": [
                ("राशियों में परिवर्तन की दर (Rate of Change)", "dy / dt = (dy / dx) · (dx / dt)", "वृत्त का क्षेत्रफल: dA/dt = 2πr(dr/dt), आयतन: dV/dt = 4πr²(dr/dt)"),
                ("वर्धमान व ह्रासमान फलन की शर्तें", "निरंतर वर्धमान: f'(x) > 0  |  निरंतर ह्रासमान: f'(x) < 0", "वर्धमान: f'(x) ≥ 0, ह्रासमान: f'(x) ≤ 0"),
                ("स्पर्श रेखा व अभिलंब की प्रवणता (Slope)", "स्पर्श रेखा m = [dy/dx]_(x₁, y₁)  |  अभिलंब प्रवणता = -1 / m", "x-अक्ष के समानांतर: dy/dx = 0; y-अक्ष के समानांतर: dx/dy = 0"),
                ("स्पर्श रेखा (Tangent) का समीकरण", "y - y₁ = m (x - x₁)", "बिंदु (x₁, y₁) पर"),
                ("अभिलंब (Normal) का समीकरण", "y - y₁ = (-1 / m)(x - x₁)  ⇒  (x - x₁) + m(y - y₁) = 0", "स्पर्श रेखा पर लंबवत रेखा"),
                ("अवकल द्वारा सन्निकटन (Approximation)", "f(x + Δx) ≈ f(x) + f'(x) · Δx", "Δy ≈ (dy / dx) Δx"),
                ("द्वितीय अवकलज परीक्षण (Local Maxima & Minima)", "f'(c) = 0 तथा f''(c) < 0 ⇒ उच्चिष्ठ | f''(c) > 0 ⇒ निम्निष्ठ", "यदि f''(c) = 0 तो परीक्षण विफल (प्रथम अवकलज परीक्षण करें)"),
            ],
        },
        {
            "name": "7. समाकलन (Integrals)",
            "formulas": [
                ("बीजगणितीय व चरघातांकी समाकलन सूत्र", "∫ xⁿ dx = x^(n+1)/(n+1) + C (n ≠ -1),  ∫ (1/x) dx = log|x| + C", "∫ eˣ dx = eˣ + C,  ∫ aˣ dx = aˣ / log_e a + C"),
                ("त्रिकोणमितीय समाकलन (sin, cos, sec², cosec²)", "∫ sin x dx = -cos x + C,  ∫ cos x dx = sin x + C", "∫ sec²x dx = tan x + C,  ∫ cosec²x dx = -cot x + C"),
                ("त्रिकोणमितीय समाकलन (sec x tan x, cosec x cot x)", "∫ sec x tan x dx = sec x + C,  ∫ cosec x cot x dx = -cosec x + C", "सीधे अवकलन के व्युत्क्रम सूत्र"),
                ("tan x, cot x, sec x, cosec x के समाकलन", "∫ tan x dx = log|sec x| + C,  ∫ cot x dx = log|sin x| + C", "∫ sec x dx = log|sec x + tan x| + C, ∫ cosec x dx = log|cosec x - cot x| + C"),
                ("प्रतिलोम त्रिकोणमितीय रूप वाले समाकलन", "∫ dx/√(a² - x²) = sin⁻¹(x/a) + C,  ∫ dx/(a² + x²) = (1/a)tan⁻¹(x/a) + C", "∫ dx/(x√(x² - a²)) = (1/a)sec⁻¹(x/a) + C"),
                ("विशेष फलन समाकलन (1 व 2: परिमेय रूप)", "∫ dx/(x² - a²) = (1/2a)log|(x-a)/(x+a)| + C | ∫ dx/(a² - x²) = (1/2a)log|(a+x)/(a-x)| + C", "अत्यंत महत्वपूर्ण बोर्ड सूत्र"),
                ("विशेष फलन समाकलन (3 व 4: वर्गमूल रूप)", "∫ dx/√(x² - a²) = log|x + √(x² - a²)| + C | ∫ dx/√(x² + a²) = log|x + √(x² + a²)| + C", "दोनों लघुगणकीय रूप में होते हैं"),
                ("मानक वर्गमूल समाकलन-1: ∫ √(a² - x²) dx", "∫ √(a² - x²) dx = (x/2)√(a² - x²) + (a²/2)sin⁻¹(x/a) + C", "वृत्त व दीर्घवृत्त क्षेत्रफल में उपयोगी"),
                ("मानक वर्गमूल समाकलन-2 व 3", "∫ √(x² ± a²) dx = (x/2)√(x² ± a²) ± (a²/2)log|x + √(x² ± a²)| + C", "चिह्न समान रहता है (+ में +, - में -)"),
                ("खंडशः समाकलन (Integration by Parts - ILATE)", "∫ u · v dx = u ∫ v dx - ∫ [(du/dx) · ∫ v dx] dx", "ILATE क्रम: Inverse, Log, Algebraic, Trig, Exponential"),
                ("विशेष चरघातांकी समाकलन सूत्र", "∫ eˣ [f(x) + f'(x)] dx = eˣ f(x) + C", "हर साल ऑब्जेक्टिव में पूछा जाने वाला सूत्र"),
                ("निश्चित समाकलन का मूलभूत प्रमेय", "∫_a^b f(x) dx = F(b) - F(a)", "जहाँ F'(x) = f(x)"),
                ("निश्चित समाकलन गुणधर्म P₂ व P₄ (King's Property)", "∫_a^b f(x)dx = ∫_a^c f(x)dx + ∫_c^b f(x)dx | ∫_0^a f(x)dx = ∫_0^a f(a - x)dx", "सबसे अधिक प्रयुक्त निश्चित समाकलन गुणधर्म"),
                ("सम व विषम फलनों के लिए निश्चित समाकलन (P₇)", "∫_(-a)^a f(x)dx = 2∫_0^a f(x)dx (यदि f सम)  तथा  = 0 (यदि f विषम)", "सम: f(-x) = f(x); विषम: f(-x) = -f(x)"),
            ],
        },
        {
            "name": "8. समाकलनों के अनुप्रयोग",
            "formulas": [
                ("वक्र y = f(x), x-अक्ष व कोटियों x=a, x=b से घिरा क्षेत्रफल", "A = ∫_a^b |y| dx = ∫_a^b |f(x)| dx", "क्षेत्रफल सदैव धनात्मक होता है"),
                ("वक्र x = g(y), y-अक्ष व भुजों y=c, y=d से घिरा क्षेत्रफल", "A = ∫_c^d |x| dy = ∫_c^d |g(y)| dy", "y के सापेक्ष समाकलन"),
                ("दो वक्रों y = f(x) व y = g(x) के मध्य का क्षेत्रफल", "A = ∫_a^b [f(x) - g(x)] dx  (जहाँ f(x) ≥ g(x))", "ऊपरी वक्र - निचला वक्र"),
                ("वृत्त व दीर्घवृत्त का संपूर्ण क्षेत्रफल सूत्र", "वृत्त (x² + y² = a²): A = πa²  |  दीर्घवृत्त (x²/a² + y²/b² = 1): A = πab", "संपूर्ण क्षेत्रफल = 4 × प्रथम चतुर्थांश का क्षेत्रफल"),
            ],
        },
        {
            "name": "9. अवकल समीकरण",
            "formulas": [
                ("अवकल समीकरण की कोटि (Order) व घात (Degree)", "कोटि = उच्चतम अवकलज क्रम | घात = उच्चतम अवकलज की पूर्णांक घात (बहुपद रूप में)", "कोटि व घात सदैव धनात्मक पूर्णांक होते हैं"),
                ("पृथक्करणीय चर (Variable Separable) विधि", "f(y) dy = g(x) dx  ⇒  ∫ f(y) dy = ∫ g(x) dx + C", "n कोटि के व्यापक हल में n स्वेच्छ अचर होते हैं"),
                ("समघातीय अवकल समीकरण (Homogeneous DE) प्रतिस्थापन", "dy/dx = f(y/x) हेतु y = v x  ⇒  dy/dx = v + x (dv/dx)", "पृथक्करणीय चर रूप में बदल जाता है"),
                ("रैखिक अवकल समीकरण (Linear DE - रूप 1)", "dy/dx + P y = Q  ⇒  I.F. = e^(∫ P dx)", "व्यापक हल: y · (I.F.) = ∫ [Q · (I.F.)] dx + C"),
                ("रैखिक अवकल समीकरण (Linear DE - रूप 2)", "dx/dy + P₁ x = Q₁  ⇒  I.F. = e^(∫ P₁ dy)", "व्यापक हल: x · (I.F.) = ∫ [Q₁ · (I.F.)] dy + C"),
            ],
        },
        {
            "name": "10. सदिश बीजगणित",
            "formulas": [
                ("सदिश a⃗ = xî + yĵ + zk̂ का परिमाण व मात्रक सदिश", "|a⃗| = √(x² + y² + z²)  तथा  â = a⃗ / |a⃗|", "मात्रक सदिश का परिमाण सदैव 1 होता है"),
                ("दिक्-अनुपात (a, b, c) व दिक्-कोज्या (l, m, n) संबंध", "l = a/|a⃗|, m = b/|a⃗|, n = c/|a⃗|  तथा  l² + m² + n² = 1", "sin²α + sin²β + sin²γ = 2"),
                ("दो बिंदुओं P व Q को मिलाने वाला सदिश व विभाजन सूत्र", "PQ⃗ = OQ⃗ - OP⃗ | अंतः विभाजन: (m b⃗ + n a⃗)/(m + n)", "बाह्य विभाजन: (m b⃗ - n a⃗)/(m - n)"),
                ("दो सदिशों का अदिश गुणनफल (Dot Product)", "a⃗ · b⃗ = |a⃗| |b⃗| cosθ = a₁b₁ + a₂b₂ + a₃b₃", "î·î = ĵ·ĵ = k̂·k̂ = 1;  î·ĵ = ĵ·k̂ = k̂·î = 0"),
                ("दो सदिशों के बीच कोण व लंबवतता की शर्त", "cosθ = (a⃗ · b⃗) / (|a⃗| |b⃗|)  |  लंबवत (a⃗ ⊥ b⃗) ⇔ a⃗ · b⃗ = 0", "a⃗ · a⃗ = |a⃗|²"),
                ("सदिश a⃗ का सदिश b⃗ पर प्रक्षेप (Projection)", "Projection of a⃗ on b⃗ = (a⃗ · b⃗) / |b⃗| = a⃗ · b̂", "अदिश प्रक्षेप सूत्र"),
                ("दो सदिशों का सदिश गुणनफल (Cross Product)", "a⃗ × b⃗ = |a⃗| |b⃗| sinθ n̂  (a⃗ × b⃗ = -b⃗ × a⃗)", "î×ĵ = k̂, ĵ×k̂ = î, k̂×î = ĵ;  î×î = 0⃗"),
                ("दो सदिशों के समानांतर (संरेख) होने की शर्त", "a⃗ × b⃗ = 0⃗  अर्थात  a₁/b₁ = a₂/b₂ = a₃/b₃", "θ = 0° या 180°"),
                ("त्रिभुज व समांतर चतुर्भुज का क्षेत्रफल (सदिश रूप)", "त्रिभुज = ½ |a⃗ × b⃗|  |  समांतर चतुर्भुज = |a⃗ × b⃗|", "यदि विकर्ण d⃗₁, d⃗₂ दिए हों तो चतुर्भुज = ½ |d⃗₁ × d⃗₂|"),
                ("अदिश त्रिक गुणन (Scalar Triple Product) व समतलीय शर्त", "[a⃗ b⃗ c⃗] = a⃗ · (b⃗ × c⃗) = समांतर षट्फलक का आयतन", "तीन सदिश समतलीय (Coplanar) ⇔ [a⃗ b⃗ c⃗] = 0"),
            ],
        },
        {
            "name": "11. त्रि-विमीय ज्यामिति",
            "formulas": [
                ("दो बिंदुओं (x₁,y₁,z₁) व (x₂,y₂,z₂) से जाने वाली रेखा के दिक्-अनुपात", "a = x₂ - x₁,  b = y₂ - y₁,  c = z₂ - z₁", "दूरी सूत्र: d = √[(x₂-x₁)² + (y₂-y₁)² + (z₂-z₁)²]"),
                ("एक बिंदु से गुजरने वाली व b⃗ के समानांतर रेखा का समीकरण", "सदिश: r⃗ = a⃗ + λb⃗  |  कार्तीय: (x-x₁)/a = (y-y₁)/b = (z-z₁)/c", "λ एक वास्तविक प्राचल है"),
                ("दो बिंदुओं a⃗ व b⃗ से गुजरने वाली रेखा का समीकरण", "सदिश: r⃗ = a⃗ + λ(b⃗ - a⃗) | कार्तीय: (x-x₁)/(x₂-x₁) = (y-y₁)/(y₂-y₁) = (z-z₁)/(z₂-z₁)", "मानक रूप"),
                ("दो रेखाओं के बीच का कोण (θ)", "cosθ = |(b⃗₁ · b⃗₂) / (|b⃗₁| |b⃗₂|)| = |(a₁a₂ + b₁b₂ + c₁c₂) / √(a₁²+b₁²+c₁²)√(a₂²+b₂²+c₂²)|", "लंबवत: a₁a₂+b₁b₂+c₁c₂ = 0; समानांतर: a₁/a₂=b₁/b₂=c₁/c₂"),
                ("दो विषमतलीय रेखाओं (Skew Lines) के बीच न्यूनतम दूरी", "d = |[(a⃗₂ - a⃗₁) · (b⃗₁ × b⃗₂)] / |b⃗₁ × b⃗₂||", "यदि प्रतिच्छेदी हों तो न्यूनतम दूरी d = 0"),
                ("दो समानांतर रेखाओं के बीच न्यूनतम दूरी", "d = |[b⃗ × (a⃗₂ - a⃗₁)] / |b⃗||", "दोनों रेखाओं के दिशा सदिश समान (b⃗) होते हैं"),
                ("समतल का समीकरण (अभिलंब रूप व एक बिंदु रूप)", "अभिलंब: r⃗ · n̂ = d (lx + my + nz = d) | बिंदु रूप: (r⃗ - a⃗) · n⃗ = 0", "d = मूलबिंदु से समतल की लंबवत दूरी"),
                ("समतल का अंतःखंड रूप (Intercept Form)", "x / a + y / b + z / c = 1", "a, b, c क्रमशः x, y, z अक्षों पर काटे गए अंतःखंड"),
                ("दो समतलों के बीच कोण व रेखा-समतल के बीच कोण", "समतल-समतल: cosθ = |n⃗₁·n⃗₂|/(|n⃗₁||n⃗₂|) | रेखा-समतल: sinθ = |b⃗·n⃗|/(|b⃗||n⃗|)", "रेखा व समतल के कोण में sinθ आता है"),
                ("बिंदु (x₁, y₁, z₁) से समतल Ax + By + Cz + D = 0 की दूरी", "d = |(Ax₁ + By₁ + Cz₁ + D) / √(A² + B² + C²)|", "समानांतर समतलों के बीच दूरी: |D₂ - D₁| / √(A²+B²+C²)"),
            ],
        },
        {
            "name": "12. रैखिक प्रोग्रामन",
            "formulas": [
                ("उद्देश्य फलन (Objective Function)", "Z = a x + b y (अधिकतमीकरण या न्यूनतमीकरण करना)", "x, y निर्णायक चर (Decision Variables) हैं"),
                ("व्यवरोध एवं ऋणेतर व्यवरोध (Constraints)", "a₁x + b₁y ≤ c₁,  a₂x + b₂y ≥ c₂  तथा  x ≥ 0, y ≥ 0", "x ≥ 0, y ≥ 0 प्रथम चतुर्थांश को दर्शाते हैं"),
                ("कोणीय बिंदु प्रमेय (Corner Point Theorem)", "इष्टतम मान (अधिकतम/न्यूनतम) सदैव सुसंगत क्षेत्र के कोणीय बिंदु पर प्राप्त होता है", "यदि क्षेत्र परिबद्ध (Bounded) हो तो दोनों मान अवश्य विद्यमान"),
                ("अपरिबद्ध (Unbounded) सुसंगत क्षेत्र नियम", "अधिकतम M हेतु ax + by > M तथा न्यूनतम m हेतु ax + by < m का खुला अर्धतल जांचें", "उभयनिष्ठ बिंदु न होने पर वही इष्टतम मान होता है"),
            ],
        },
        {
            "name": "13. प्रायिकता",
            "formulas": [
                ("सप्रतिबंध प्रायिकता (Conditional Probability)", "P(E | F) = P(E ∩ F) / P(F)  (जहाँ P(F) ≠ 0)", "घटना F घटित होने पर E की प्रायिकता"),
                             ("सप्रतिबंध प्रायिकता (Conditional Probability)", "P(E | F) = P(E ∩ F) / P(F)  (जहाँ P(F) ≠ 0)", "घटना F घटित होने पर E की प्रायिकता"),
                ("प्रायिकता का गुणन नियम (Multiplication Theorem)", "P(E ∩ F) = P(E) · P(F | E) = P(F) · P(E | F)", "संयुक्त घटना की प्रायिकता"),
                ("स्वतंत्र घटनाओं (Independent Events) की शर्त", "P(E ∩ F) = P(E) · P(F)", "यदि E व F स्वतंत्र हैं तो E' व F' भी स्वतंत्र होंगी"),
                ("संपूर्ण प्रायिकता प्रमेय (Total Probability Theorem)", "P(A) = Σ P(E_i) · P(A | E_i)", "E₁, E₂... प्रतिदर्श समष्टि का विभाजन हैं"),
                ("बेज़ प्रमेय (Bayes' Theorem)", "P(E_i | A) = [P(E_i) · P(A | E_i)] / [Σ P(E_j) · P(A | E_j)]", "पश्च प्रायिकता (Posterior Probability) सूत्र"),
                ("यादृच्छिक चर (Random Variable) का माध्य व प्रसरण", "माध्य μ = E(X) = Σ x_i p_i  |  प्रसरण Var(X) = E(X²) - [E(X)]²", "मानक विचलन σ = √Var(X)"),
                ("बर्नौली परीक्षण व द्विपद बंटन (Binomial Distribution)", "P(X = r) = ⁿC_r · pʳ · q^(n - r)  (जहाँ p + q = 1)", "माध्य = n p,  प्रसरण = n p q (माध्य > प्रसरण सदैव)"),
            ],
        },
    ],
}

# ================= COMPLETE BIOLOGY DATA (ALL 16 CHAPTERS + TEXT DIAGRAM GUIDES) =================
BIOLOGY_DATA = {
    "title": "🧬 Biology (जीव विज्ञान)",
    "chapters": [
        {
            "name": "1. जीवों में जनन",
            "formulas": [
                ("अलैंगिक जनन की मुख्य विधियाँ", "द्विखंडन (अमीबा), मुकुलन (यीस्ट/हाइड्रा), कोनिडिया (पेनिसिलियम), चलबीजाणु (क्लैमाइडोमोनास)", "जेम्यूल निर्माण: स्पंज में"),
                ("कायिक प्रवर्धन संरचनाएँ", "प्रकंद (अदरक/हल्दी), कंद (आलू - आँख), शल्ककंद (प्याज), भूस्तारी (दूबघास), पत्ती कलिका (ब्रायोफिलम)", "बंगाल का आतंक: जलकुंभी (Eichhornia)"),
                ("गुणसूत्र संख्या (2n बनाम n)", "मनुष्य: 2n=46 (n=23) | मटर: 2n=14 | धान: 2n=24 | मक्का: 2n=20 | फलमक्खी: 2n=8", "ऑफियोग्लॉसम (फर्न): 2n = 1260 (सर्वाधिक)"),
                ("निषेचन व भ्रूणोद्भव नियम", "युग्मक सदैव अगुणित (n) व युग्मनज (Zygote) सदैव द्विगुणित (2n) होता है", "अनिषेकजनन (Parthenogenesis): मधुमक्खी, रोटीफर, टर्की"),
            ],
            "diagrams_text": (
                "1. अमीबा में द्विखंडन (Binary Fission):\n"
                "   • चरण-1: जनक अमीबा कोशिका → चरण-2: केंद्रक लंबा होकर दो भागों में विभाजित (कैरियोकाइनेसिस) → चरण-3: कोशिकाद्रव्य विभाजन (साइटोकाइनेसिस) → चरण-4: दो पुत्री अमीबा।\n\n"
                "2. यीस्ट में मुकुलन (Budding in Yeast):\n"
                "   • जनक कोशिका पर छोटा उभार (मुकुल) → केंद्रक विभाजन → मुकुल श्रृंखला निर्माण।\n\n"
                "3. अन्य प्रमुख VVI आरेख:\n"
                "   • क्लैमाइडोमोनास के चलबीजाणु (Zoospores) • पेनिसिलियम के कोनिडिया • स्पंज के जेम्यूल (Gemmules) • ब्रायोफिलम की अपस्थानिक कलिकाएँ।"
            ),
        },
        {
            "name": "2. पुष्पी पादपों में लैंगिक जनन",
            "formulas": [
                ("परागण व लघुबीजाणुजनन अनुपात", "1 लघुबीजाणु मातृ कोशिका (PMC - 2n) → अर्धसूत्री विभाजन → 4 अगुणित परागकण (n)", "टेपेटम: परागकोष की सबसे आंतरिक पोषक परत"),
                ("परागकण की बाह्य व अंतःभित्ति", "बाह्यचोल (Exine): स्पोरोपोलेनिन से निर्मित (सर्वाधिक प्रतिरोधी) | अंतःचोल: सेलुलोज व पेक्टिन", "जनन छिद्र (Germ Pore) पर स्पोरोपोलेनिन अनुपस्थित"),
                ("परिपक्व भ्रूणकोष (Embryo Sac) की संरचना", "7-कोशिकीय एवं 8-केंद्रकीय (Polygonum प्रकार)", "3 प्रतिव्यासांत + 1 केंद्रीय कोशिका (2 ध्रुवीय केंद्रक) + 2 सहायक + 1 अंड कोशिका"),
                ("द्विनिषेचन (Double Fertilization - नवाश्चिन)", "सत्य निषेचन: नर युग्मक(n) + अंड(n) → युग्मनज(2n) | त्रिसंलयन: नर युग्मक(n) + द्वितीयक केंद्रक(2n) → PEN(3n)", "आवृतबीजी पादपों का अद्वितीय लक्षण; भ्रूणपोष त्रिगुणित (3n) होता है"),
                ("असंगजनन व बहुभ्रूणता (Apomixis & Polyembryony)", "बिना निषेचन बीज निर्माण = असंगजनन | एक बीज में अनेक भ्रूण = बहुभ्रूणता (संतरा, नींबू)", "खोजकर्ता: ल्यूवेनहॉक (सिट्रस में)"),
            ],
            "diagrams_text": (
                "1. प्रतीप बीजांड (Anatropous Ovule) के मुख्य भाग:\n"
                "   • बीजांडवृंत (Funicle) • नाभिका (Hilum) • अध्यावरण (Integuments) • बीजांडद्वार (Micropyle) • निभाग (Chalaza) • बीजांडकाय (Nucellus) • भ्रूणकोष (Embryo Sac)।\n\n"
                "2. परिपक्व भ्रूणकोष (7-कोशिकीय, 8-केंद्रकीय):\n"
                "   • निभाग सिरा: 3 प्रतिव्यासांत कोशिकाएँ (Antipodals)\n"
                "   • मध्य भाग: 1 बड़ी केंद्रीय कोशिका जिसमें 2 ध्रुवीय केंद्रक (Polar Nuclei)\n"
                "   • बीजांडद्वार सिरा: अंड उपकरण (1 अंड कोशिका + 2 सहायक कोशिकाएँ + तंतुरूप समुच्चय)।"
            ),
        },
        {
            "name": "3. मानव जनन",
            "formulas": [
                ("शुक्राणुजनन व अंडजनन अनुपात", "1 प्राथमिक शुक्राणुकोशिका (2n) → 4 शुक्राणु (n) | 1 प्राथमिक अंडक (2n) → 1 अंडाणु (n) + ध्रुवीय पिंड", "शुक्राणु निर्माण ताप: शरीर से 2–2.5°C कम (वृषणकोष में)"),
                ("प्रमुख जनन हार्मोन व कार्य", "LH: लेडिग कोशिकाओं से टेस्टोस्टेरोन स्राव व अंडोत्सर्ग | FSH: सरटोली कोशिकाओं व पुटक वृद्धि को प्रेरित", "ऑक्सीटोसिन: प्रसव (Parturition) संकुचन हार्मोन"),
                ("आर्तव चक्र (Menstrual Cycle - 28 दिन)", "अंडोत्सर्ग (Ovulation): 14वें दिन (LH सर्ज के कारण) | कॉर्पस ल्यूटियम: प्रोजेस्टेरोन का स्राव", "गर्भधारण न होने पर कॉर्पस ल्यूटियम नष्ट हो जाता है"),
                ("गर्भकलाएँ व अपरा (Placenta) हार्मोन", "अपरा द्वारा स्रावित हार्मोन: hCG, hPL, एस्ट्रोजन, प्रोजेस्टेरोन व रिलैक्सिन", "मानव में सगर्भता अवधि (Gestation): लगभग 9 माह (266–280 दिन)"),
            ],
            "diagrams_text": (
                "1. मानव शुक्राणु की संरचना (Structure of Human Sperm):\n"
                "   • सिर (Head): अगुणित केंद्रक + आगे टोपीनुमा एक्रोसोम (हायल्यूरोनिडेज एंजाइम युक्त)।\n"
                "   • ग्रीवा (Neck): समीपस्थ व दूरस्थ तारककेंद्र।\n"
                "   • मध्य भाग (Middle Piece): सर्पिलाकार माइटोकॉन्ड्रिया (ऊर्जा स्रोत)।\n"
                "   • पूँछ (Tail): गतिशीलता प्रदान करती है।\n\n"
                "2. ग्राफी पुटक (Graafian Follicle) व अंडाणु की संरचना:\n"
                "   • द्वितीयक अंडक • जोना पेलुसिडा (Zona Pellucida) • कोरोना रेडिएटा • एंट्रम गुहा।"
            ),
        },
        {
            "name": "4. जनन स्वास्थ्य",
            "formulas": [
                ("प्रमुख फुल फॉर्म्स (Full Forms - Part 1)", "RCH: Reproductive and Child Health | MMR: Maternal Mortality Rate | IMR: Infant Mortality Rate", "उल्वबेधन (Amniocentesis): भ्रूणीय गुणसूत्र विकार जाँच (लिंग परीक्षण प्रतिबंधित)"),
                ("गर्भनिरोधक विधियाँ व उदाहरण", "रोध (Barrier): कंडोम, डायाफ्राम | IUDs: कॉपर-T, Cu7, मल्टीलोड-375 (Cu²⁺ शुक्राणु गतिशीलता घटाता है)", "सहेली (Saheli): गैर-स्टेरॉइडल साप्ताहिक गोली (CDRI लखनऊ द्वारा निर्मित)"),
                ("स्थायी बंध्यकरण (Sterilization)", "पुरुष नसबंदी: वैसेक्टॉमी (Vasectomy - शुक्रवाहक काटना) | महिला नसबंदी: ट्यूबेक्टॉमी (Tubectomy)", "शल्य क्रिया द्वारा स्थायी गर्भनिरोधन"),
                ("सहायक जनन प्रौद्योगिकियाँ (ART Full Forms)", "IVF-ET (टेस्ट ट्यूब बेबी), ZIFT (युग्मनज अंतः डिंबवाहिनी स्थानांतरण - 8 ब्लास्टोमियर तक), GIFT, ICSI, AI", "ZIFT: 8 ब्लास्टोमियर तक; IUT: 8 से अधिक ब्लास्टोमियर"),
            ],
            "diagrams_text": (
                "1. शल्य क्रियात्मक गर्भनिरोधन आरेख:\n"
                "   • वैसेक्टॉमी (पुरुष): शुक्रवाहक (Vas deferens) को काटकर बाँधना।\n"
                "   • ट्यूबेक्टॉमी (महिला): अंडवाहिनी (Fallopian tube) को काटकर बाँधना।\n\n"
                "2. IUDs प्रकार:\n"
                "   • औषधिरहित: लिप्पेस लूप | ताँबा मोचक: CuT, Cu7, Multiload-375 | हार्मोन मोचक: Progestasert, LNG-20।"
            ),
        },
        {
            "name": "5. वंशागति तथा विविधता के सिद्धांत",
            "formulas": [
                ("मेंडल का एकसंकर संकरण (Monohybrid Cross) अनुपात", "फीनोटाइप अनुपात = 3 : 1  |  जीनोटाइप अनुपात = 1 : 2 : 1", "प्रभाविता व विसंयोजन का नियम"),
                ("मेंडल का द्विसंकर संकरण (Dihybrid Cross) अनुपात", "फीनोटाइप अनुपात = 9 : 3 : 3 : 1  |  जीनोटाइप = 1:2:1:2:4:2:1:2:1", "स्वतंत्र अपव्यूहन का नियम (Law of Independent Assortment)"),
                ("परीक्षार्थ संकरण (Test Cross) अनुपात", "एकसंकर Test Cross = 1 : 1  |  द्विसंकर Test Cross = 1 : 1 : 1 : 1", "अज्ञात जीनोटाइप का अप्रभावी जनक (tt) से संकरण"),
                ("अपूर्ण प्रभाविता (Incomplete Dominance) अनुपात", "फीनोटाइप = 1 : 2 : 1  तथा  जीनोटाइप = 1 : 2 : 1 (दोनों समान)", "उदाहरण: श्वान पुष्प (Mirabilis jalapa / Antirrhinum)"),
                ("सह-प्रभाविता व बहु-एलील (ABO रक्त समूह)", "ABO जीनोटाइप = 6, फीनोटाइप = 4 (A, B, AB, O) | Iᴬ व Iᴮ सह-प्रभावी हैं", "सूत्र: जीनोटाइप = n(n+1)/2 (जहाँ n = 3 एलील)"),
                ("लिंग निर्धारण प्रकार (Sex Determination)", "मनुष्य व ड्रोसोफिला: XX-XY (नर विषमयुग्मकी) | पक्षी: ZZ-ZW (मादा विषमयुग्मकी)", "मधुमक्खी: अगुणित-द्विगुणित (नर n=16, मादा 2n=32)"),
                ("गुणसूत्रीय विकार (Chromosomal Disorders)", "डाउन सिंड्रोम: 21वीं त्रिगुणसूत्रता (47) | क्लाइनफेल्टर: 44+XXY (47, बंध्य नर) | टर्नर: 44+XO (45, बंध्य मादा)", "मेंडेलियन विकार: हीमोफीलिया, वर्णांधता, सिकल सेल एनीमिया"),
            ],
            "diagrams_text": (
                "1. पुनेट वर्ग (Punnett Square) — द्विसंकर संकरण:\n"
                "   • जनक: RRYY (गोल-पीला) × rryy (झुर्रीदार-हरा) → F₁: RrYy (सभी गोल-पीले)।\n"
                "   • F₂ अनुपात: 9 गोल-पीले : 3 गोल-हरे : 3 झुर्रीदार-पीले : 1 झुर्रीदार-हरा।\n\n"
                "2. वंशावली विश्लेषण (Pedigree Symbols):\n"
                "   • वर्ग (□) = नर | वृत्त (○) = मादा | भरा हुआ (■/●) = रोगग्रस्त व्यक्ति | क्षैतिज रेखा = विवाह।"
            ),
        },
        {
            "name": "6. वंशागति के आणविक आधार",
            "formulas": [
                ("DNA द्विकुंडली की माप (वाटसन एवं क्रिक मॉडल)", "पिच (1 घुमाव) = 3.4 nm (34 Å),  10 क्षार युग्म/घुमाव,  दो क्षारों की दूरी = 0.34 nm (3.4 Å)", "कुंडली का व्यास = 2.0 nm (20 Å)"),
                ("चारगाफ नियम एवं न्यूक्लियोसोम संरचना", "[A] = [T], [G] = [C]  ⇒  (A + G) / (T + C) = 1 | 1 न्यूक्लियोसोम = 200 bp DNA + हिस्टोन अष्टक", "हिस्टोन प्रोटीन धनावेशित (लाइसिन व आर्जिनिन युक्त) होता है"),
                ("रूपांतरण व आनुवंशिक पदार्थ सिद्ध करने वाले प्रयोग", "ग्रिफिथ (1928 - रूपांतरण सिद्धांत) → एवरी, मैक्लिओड व मैकार्टी → हर्षे व चेज (1952 - ³²P व ³⁵S)", "हर्षे-चेज प्रयोग ने सिद्ध किया कि DNA ही आनुवंशिक पदार्थ है"),
                ("सेंट्रल डोग्मा (Central Dogma - फ्रांसिस क्रिक)", "DNA ──(प्रतिकृति)──▶ DNA ──(अनुलेखन)──▶ mRNA ──(रूपांतरण)──▶ प्रोटीन", "रेट्रोवायरस में रिवर्स ट्रांसक्रिप्शन (RNA → DNA) होता है"),
                ("आनुवंशिक कूट (Genetic Code) की विशेषताएँ", "कुल कोडॉन = 64 (61 ऐमीनो अम्ल हेतु + 3 समापन कोडॉन: UAA, UAG, UGA)", "प्रारंभन कोडॉन: AUG (मेथिओनिन) — सर्वव्यापी व अपहासित (Degenerate)"),
                ("लैक ओपेरॉन (Lac Operon - जैकब एवं मोनाड)", "z-जीन: β-गैलेक्टोसिडेज | y-जीन: परमिएज | a-जीन: ट्रांसऐसीटाइलेज | प्रेरक (Inducer): लैक्टोज", "DNA फिंगरप्रिंटिंग जनक: एलेक जेफ्रीज (VNTR पर आधारित)"),
            ],
            "diagrams_text": (
                "1. DNA द्विकुंडली (Double Helix) लेबलिंग:\n"
                "   • प्रतिसमानांतर शृंखलाएँ (5'→3' व 3'→5') • शर्करा-फॉस्फेट रीढ़ • क्षार युग्म: A=T (2 H-बंध), G≡C (3 H-बंध)।\n\n"
                "2. लैक ओपेरॉन (Lac Operon) घटक:\n"
                "   • नियामक जीन (i) → दमनकारी प्रोटीन (Repressor) • उन्नायक (P) • प्रचालक (O) • संरचनात्मक जीन (z, y, a)।\n\n"
                "3. tRNA (क्लोवर लीफ मॉडल):\n"
                "   • 3' सिरे पर ऐमीनो अम्ल बंधन स्थल (CCA) • निचले लूप पर प्रतिप्रकूट (Anticodon) लूप।"
            ),
        },
        {
            "name": "7. विकास (Evolution)",
            "formulas": [
                ("हार्डी-वेनबर्ग समीकरण (Hardy-Weinberg Principle)", "p + q = 1  तथा  p² + 2pq + q² = 1", "p = प्रभावी एलील आवृत्ति, q = अप्रभावी एलील आवृत्ति"),
                ("समजात व समवृत्ति अंग (Homologous vs Analogous)", "समजात (उत्पत्ति समान, कार्य भिन्न - अपसारी विकास) | समवृत्ति (उत्पत्ति भिन्न, कार्य समान - अभिसारी विकास)", "समजात: मानव हाथ, चमगादड़ पंख; समवृत्ति: कीट व पक्षी के पंख"),
                ("प्रमुख विकास सिद्धांत व वैज्ञानिक", "मिलर-यूरी प्रयोग (1953): CH₄, NH₃, H₂ व जलवाष्प (800°C) से ऐमीनो अम्ल निर्माण", "डार्विन: प्राकृतिक वरण; लैमार्क: उपार्जित लक्षणों की वंशागति; ह्यूगो डी व्रीज: उत्परिवर्तन"),
                ("मानव विकास का सही क्रम एवं कपाल क्षमता", "ड्रायोपिथेकस → रामापिथेकस → ऑस्ट्रेलोपिथेकस → होमो हैबिलिस (650–800 cc) → होमो इरेक्टस (900 cc) → निएंडरथल (1400 cc) → होमो सेपियंस", "अत्यंत महत्वपूर्ण ऑब्जेक्टिव क्रम"),
            ],
            "diagrams_text": (
                "1. स्टेनली मिलर का चिंगारी विसर्जन उपकरण (1953):\n"
                "   • बड़ा फ्लास्क: CH₄ : NH₃ : H₂ (2:1:2 अनुपात) + टंगस्टन इलेक्ट्रोड (800°C विद्युत चिंगारी) → संघनित्र → ऐमीनो अम्ल (ग्लाइसीन, ऐलानीन) प्राप्त।\n\n"
                "2. अनुकूली विकिरण (Adaptive Radiation):\n"
                "   • गैलापैगोस द्वीप की डार्विन फिंचें व ऑस्ट्रेलियाई मार्सुपियल।"
            ),
        },
        {
            "name": "8. मानव स्वास्थ्य तथा रोग",
            "formulas": [
                ("प्रमुख जीवाणुजनित रोग व रोगकारक (Bacterial Diseases)", "टाइफॉइड: साल्मोनेला टाइफी (विडाल टेस्ट) | निमोनिया: स्ट्रेप्टोकोकस निमोनी | हैजा: विब्रियो कोलेरी", "क्षय रोग (TB): माइकोबैक्टीरियम ट्यूबरकुलोसिस"),
                ("प्रमुख विषाणु व प्रोटोजोआ जनित रोग", "मलेरिया: प्लाज्मोडियम (वाहक: मादा एनोफिलीज, हीमोजोइन विष) | अमीबता: एंटअमीबा हिस्टोलिटिका", "हाथीपाँव (फाइलेरिया): वुचेरेरिया बैनक्रॉफ्टी (वाहक: क्यूलेक्स मच्छर)"),
                ("प्रतिरक्षी अणु (Antibody) की संरचना", "H₂L₂ संरचना (2 भारी शृंखलाएँ + 2 हल्की शृंखलाएँ, डाइसल्फाइड बंध द्वारा जुड़ी)", "प्रमुख प्रकार: IgG, IgA (कोलोस्ट्रम में), IgM, IgE (एलर्जी में), IgD"),
                ("AIDS एवं कैंसर (AIDS & Cancer)", "HIV (रेट्रोवायरस) सहायक T-लिम्फोसाइट्स (TH) को नष्ट करता है | परीक्षण: ELISA", "कैंसर जाँच: बायोप्सी, CT स्कैन, MRI; ऑन्कोजीन सक्रियण से अनियंत्रित कोशिका विभाजन"),
            ],
            "diagrams_text": (
                "1. प्रतिरक्षी अणु (Antibody - H₂L₂) संरचना:\n"
                "   • Y-आकार अणु • 2 लंबी भारी (Heavy) व 2 छोटी हल्की (Light) पॉलिपेप्टाइड शृंखलाएँ • डाइसल्फाइड बंध (-S-S-) • ऊपरी सिरे पर प्रतिजन बंधन स्थल (Antigen Binding Site)।\n\n"
                "2. प्लाज्मोडियम जीवन चक्र:\n"
                "   • संक्रामक अवस्था: स्पोरोजोइट (Sporozoite) → मानव यकृत कोशिका → RBC विखंडन (हीमोजोइन स्राव) → मादा एनोफिलीज मच्छर में युग्मकजनन।"
            ),
        },
        {
            "name": "9. खाद्य उत्पादन में वृद्धि की कार्यनीति",
            "formulas": [
                ("प्रमुख संकर फसल किस्में (Hybrid Varieties)", "गेहूँ: सोनालिका व कल्याण सोना | धान: जया व रत्ना | भिंडी (पीत मोजेक प्रतिरोधी): परभणी क्रांति", "हरित क्रांति के जनक: नॉर्मन बोरलॉग (भारत में एम. एस. स्वामीनाथन)"),
                ("पशुपालन व मधुमक्खी पालन", "अंतःप्रजनन अवसादन दूर करने हेतु बहिःसंकरण (Out-crossing) करते हैं | MOET तकनीक: पशु प्रजनन", "भारतीय मधुमक्खी: एपिस इंडिका (Apis indica)"),
                ("ऊतक संवर्धन व एकल कोशिका प्रोटीन (SCP)", "पूर्णशक्तता (Totipotency): एक पादप कोशिका से पूर्ण पौधा बनना | विषाणु मुक्त पौधे: विभज्योतक (Meristem) संवर्धन से", "SCP उदाहरण: स्पिरुलिना (Spirulina) व मिथाइलोफिलस मिथाइलोट्रोपस"),
            ],
            "diagrams_text": (
                "1. पादप ऊतक संवर्धन (Tissue Culture) चरण:\n"
                "   • कर्तोतक (Explant) चयन → निर्जीवीकरण → पोषक माध्यम में संवर्धन → कैलस (Callus) निर्माण → ऑक्सिन व साइटोकाइनिन हार्मोन द्वारा जड़-तना विभेदन → सूक्ष्मप्रवर्धन (Micropropagation)।"
            ),
        },
        {
            "name": "10. मानव कल्याण में सूक्ष्मजीव",
            "formulas": [
                ("औद्योगिक किण्वन उत्पाद व सूक्ष्मजीव", "सिट्रिक अम्ल: एस्परजिलस नाइगर | ऐसीटिक अम्ल: ऐसीटोबैक्टर ऐसीटी | ब्यूटिरिक अम्ल: क्लॉस्ट्रिडियम ब्यूटाइलिकम", "लैक्टिक अम्ल: लैक्टोबैसिलस; एथेनॉल: सैकेरोमाइसीज सेरेविसी (यीस्ट)"),
                ("जैव-सक्रिय अणु (Bioactive Molecules)", "साइक्लोस्पोरिन-A (प्रतिरक्षा निरोधक): ट्राइकोडर्मा पॉलीस्पोरम | स्टैटिन (कोलेस्ट्रॉल घटाने वाला): मोनॉस्कस परप्यूरियस", "स्ट्रेप्टोकाइनेज (थक्का स्फोटक / Clot Buster): स्ट्रेप्टोकोकस"),
                ("वाहितमल उपचार व BOD नियम", "BOD (Biochemical Oxygen Demand) ∝ जल प्रदूषण (BOD जितना अधिक, जल उतना ही अधिक प्रदूषित)", "बायोगैस का मुख्य घटक: मीथेन (CH₄, 50–70%) — मीथेनोजेन जीवाणु द्वारा"),
                ("जैव-नियंत्रक व जैव-उर्वरक (Biocontrol & Biofertilizers)", "Bt कपास: बैसिलस थुरिनजिएंसिस | बैकुलोवायरस (न्यूक्लिओपॉलीहेड्रोवायरस): प्रजाति-विशिष्ट कीट नियंत्रण", "जैव-उर्वरक: राइजोबियम, एजोटोबैक्टर, एनाबीना, नॉस्टॉक, माइकोराइजा (Glomus)"),
            ],
            "diagrams_text": (
                "1. प्रारूपी बायोगैस संयंत्र (Biogas Plant):\n"
                "   • गोबर + जल (कर्दम/Slurry) टैंक → भूमिगत अवायवीय पाचक (10-15 फीट गहरा) → गैस होल्डर (CH₄ + CO₂ + H₂S) → गैस निकास पाइप व खाद निकासी टैंक।"
            ),
        },
        {
            "name": "11. जैव प्रौद्योगिकी - सिद्धांत व प्रक्रम",
            "formulas": [
                ("आणविक कैंची व आणविक गोंद", "आणविक कैंची: प्रतिबंधन एंडोन्यूक्लिएज (प्रथम खोजा गया: Hind II) | आणविक गोंद: DNA लाइगेज", "EcoRI: Escherichia coli RY13 से प्राप्त (पैलिंड्रोमिक क्रम: 5'-GAATTC-3')"),
                ("जेल वैद्युतकणसंचलन (Gel Electrophoresis)", "ऋणावेशित DNA खंड एगारोज जेल में एनोड (+) की ओर आकार के अनुसार गति करते हैं (छोटे खंड तेज चलते हैं)", "अभिरंजन: इथिडियम ब्रोमाइड (EtBr) + UV प्रकाश में नारंगी पट्टियाँ"),
                ("क्लोनिंग संवाहक pBR322 की विशेषताएँ", "ori (प्रतिकृति की उत्पत्ति), rop, तथा दो प्रतिजैविक प्रतिरोधी जीन: ampᴿ (ऐम्पिसिलिन) व tetᴿ (टेट्रासाइक्लिन)", "Ti प्लाज्मिड: एग्रोबैक्टीरियम ट्यूमीफेशियंस (पादपों का प्राकृतिक आनुवंशिक इंजीनियर)"),
                ("PCR (Polymerase Chain Reaction) के 3 चरण", "1. निष्क्रियकरण (Denaturation - 94°C) → 2. तापानुशीलन (Annealing - 55°C) → 3. प्रसार (Extension - 72°C)", "एंजाइम: Taq पॉलिमरेज (Thermus aquaticus जीवाणु से प्राप्त तापस्थायी एंजाइम)"),
            ],
            "diagrams_text": (
                "1. क्लोनिंग संवाहक pBR322 का मानचित्र:\n"
                "   • प्रतिबंधन स्थल: EcoRI, ClaI, HindIII, BamHI, SalI (tetᴿ में), PvuI, PstI (ampᴿ में) • ori स्थल • rop स्थल।\n\n"
                "2. साधारण विलोडित हौज बायो-रिएक्टर (Stirred-Tank Bioreactor):\n"
                "   • मोटर व विलोडक शाफ्ट • चपटे ब्लेड वाले इम्पेलर • झाग नियंत्रक • pH व ताप नियंत्रक • निर्जीवाणु वायु आपूर्ति।"
            ),
        },
        {
            "name": "12. जैव प्रौद्योगिकी एवं उसके उपयोग",
            "formulas": [
                ("Bt फसलों में Cry जीन के प्रकार", "cryIAc व cryIIAb: कपास के मुकुल कृमि (Bollworm) को नियंत्रित करते हैं | cryIAb: मक्का छेदक (Corn Borer) को रोकता है", "Bt विष क्षारीय pH (कीट की आंत्र) में सक्रिय होता है"),
                ("RNA अंतरक्षेप (RNA Interference - RNAi)", "तंबाकू की जड़ों को सूत्रकृमि Meloidogyne incognita से बचाने हेतु पूरक dsRNA द्वारा mRNA का मौनीकरण", "कोशिकीय सुरक्षा तंत्र"),
                ("आनुवंशिक रूप से निर्मित इंसुलिन (Humulin)", "A-पेप्टाइड (21 ऐमीनो अम्ल) + B-पेप्टाइड (30 ऐमीनो अम्ल) डाइसल्फाइड बंध से जुड़े; परिपक्व इंसुलिन में C-पेप्टाइड नहीं होता", "1983 में Eli Lilly कंपनी द्वारा E. coli से निर्मित"),
                ("प्रथम जीन चिकित्सा (Gene Therapy - 1990)", "4 वर्षीय बालिका में ADA (एडीनोसीन डिएमिनेज) एंजाइम की कमी को दूर करने हेतु रेट्रोवायरस संवाहक द्वारा किया गया", "GEAC: Genetic Engineering Approval Committee"),
            ],
            "diagrams_text": (
                "1. प्राक्-इंसुलिन का परिपक्व इंसुलिन में रूपांतरण:\n"
                "   • प्राक्-इंसुलिन (Pro-insulin): A-पेप्टाइड + B-पेप्टाइड + मुक्त C-पेप्टाइड।\n"
                "   • परिपक्व इंसुलिन: C-पेप्टाइड हट जाता है और A व B शृंखलाएँ दो डाइसल्फाइड बंधों से जुड़ जाती हैं।"
            ),
        },
        {
            "name": "13. जीव और समष्टियाँ",
            "formulas": [
                ("समष्टि घनत्व (Population Density) समीकरण", "N_(t+1) = N_t + [(B + I) - (D + E)]", "B = जन्मदर, I = आप्रवासन (बढ़ाते हैं); D = मृत्युदर, E = उत्प्रवासन (घटाते हैं)"),
                ("चरघातांकी वृद्धि समीकरण (Exponential Growth - J-आकार)", "dN / dt = r N  ⇒  N_t = N₀ e^(rt)", "असीमित संसाधनों की स्थिति में (r = प्राकृतिक वृद्धि की आंतरिक दर)"),
                ("संभार-तंत्र वृद्धि समीकरण (Logistic Growth - S-आकार / सिग्मॉइड)", "dN / dt = r N [(K - N) / K]", "K = पोषण क्षमता (Carrying Capacity); सीमित संसाधन हेतु यथार्थवादी मॉडल"),
                ("समष्टि पारस्परिक क्रियाएँ (Population Interactions)", "सहोपकारिता (+, +) | परभक्षण (+, -) | परजीविता (+, -) | स्पर्धा (-, -) | सहभोजिता (+, 0) | अंतरजातीय परजीविता (-, 0)", "सहोपकारिता उदाहरण: लाइकेन, माइकोराइजा, अंजीर व बर्र"),
            ],
            "diagrams_text": (
                "1. समष्टि वृद्धि वक्र (Population Growth Curves):\n"
                "   • वक्र-a (J-आकार): चरघातांकी वृद्धि (dN/dt = rN) — असीमित संसाधन।\n"
                "   • वक्र-b (S-आकार / सिग्मॉइड): लॉजिस्टिक वृद्धि (dN/dt = rN[(K-N)/K]) — पोषण क्षमता (K) रेखा पर स्थिर।"
            ),
        },
        {
            "name": "14. पारितंत्र",
            "formulas": [
                ("सकल व शुद्ध प्राथमिक उत्पादकता सूत्र", "NPP = GPP - R", "GPP = सकल प्राथमिक उत्पादकता, R = श्वसन हानि, NPP = शुद्ध प्राथमिक उत्पादकता"),
                ("लिंडमैन का 10% ऊर्जा प्रवाह नियम (10% Law)", "एक पोषी स्तर से अगले पोषी स्तर में केवल 10% ऊर्जा स्थानांतरित होती है (शेष 90% ऊष्मा में क्षय)", "ऊर्जा प्रवाह सदैव एकदिशीय (Unidirectional) होता है"),
                ("पारिस्थितिक पिरामिड के नियम", "ऊर्जा का पिरामिड सदैव सीधा (Always Upright) होता है", "समुद्र में जैवभार (Biomass) का पिरामिड उल्टा (Inverted) होता है"),
                ("अपघटन के 5 प्रमुख चरण", "खंडन (Fragmentation) → निक्षालन (Leaching) → अपचयन (Catabolism) → ह्यूमीफिकेशन → खनिजीकरण", "केंचुआ: अपरदाहारी (Detritivore)"),
            ],
            "diagrams_text": (
                "1. ऊर्जा का पारिस्थितिक पिरामिड (10% नियम):\n"
                "   • आधार: उत्पादक (10,000 J) → प्राथमिक उपभोक्ता (1,000 J) → द्वितीयक (100 J) → तृतीयक (10 J)।\n"
                "   • नोट: ऊर्जा का पिरामिड सदैव सीधा (Upright) होता है।"
            ),
        },
        {
            "name": "15. जैव-विविधता एवं संरक्षण",
            "formulas": [
                ("जाति-क्षेत्र संबंध (Species-Area Relationship - हम्बोल्ट)", "S = C Aᶻ  ⇒  log S = log C + Z log A", "Z = समाश्रयण ढाल (सामान्यतः 0.1–0.2; बड़े महाद्वीपों में 0.6–1.2)"),
                ("रिवेट पॉपर परिकल्पना (Rivet Popper Hypothesis)", "प्रतिपादक: पॉल एहरलिच (Paul Ehrlich)", "हवाई जहाज = पारितंत्र, रिवेट = प्रजातियाँ, पंख के रिवेट = मुख्य (Keystone) प्रजातियाँ"),
                ("जैव-विविधता क्षति के चार मुख्य कारण (The Evil Quartet)", "1. आवासीय क्षति एवं विखंडन (प्रमुख कारण) | 2. अति-दोहन | 3. विदेशी जातियों का आक्रमण | 4. सह-विलुप्तता", "पृथ्वी का फेफड़ा: अमेज़न वर्षा वन"),
                ("स्व-स्थाने (In-situ) बनाम बाह्य-स्थाने (Ex-situ) संरक्षण", "In-situ: राष्ट्रीय उद्यान, अभयारण्य, बायोस्फीयर रिजर्व, हॉटस्पॉट (विश्व में 34, भारत में 3) | Ex-situ: चिड़ियाघर, वानस्पतिक उद्यान, बीज बैंक, निम्नताप परिरक्षण (-196°C द्रव N₂)", "रियो पृथ्वी सम्मेलन: 1992"),
            ],
            "diagrams_text": (
                "1. जाति-क्षेत्र संबंध ग्राफ (Species-Area Curve):\n"
                "   • X-अक्ष: क्षेत्रफल (Area) | Y-अक्ष: जाति समृद्धि (Species Richness)।\n"
                "   • सामान्य पैमाने पर आयताकार अतिपरवलय (S = CAᶻ) तथा लघुगणकीय पैमाने पर सीधी रेखा (log S = log C + Z log A)।"
            ),
        },
        {
            "name": "16. पर्यावरण के मुद्दे",
            "formulas": [
                ("वायु प्रदूषण नियंत्रण उपकरण व मानक", "स्थिरवैद्युत अवक्षेपक (Electrostatic Precipitator): 99% कणिकीय पदार्थ हटाता है | स्क्रबर (Scrubber): SO₂ गैस हटाता है", "CPCB के अनुसार 2.5 μm या कम व्यास (PM 2.5) के कण मानव स्वास्थ्य हेतु सर्वाधिक हानिकारक"),
                ("उत्प्रेरक परिवर्तक (Catalytic Converter) धातुएँ", "प्लैटिनम (Pt), पैलेडियम (Pd) तथा रोडियम (Rh) — जहरीली गैसों को CO₂, N₂ व H₂O में बदलते हैं", "सीसा रहित पेट्रोल (Unleaded Petrol) का उपयोग अनिवार्य"),
                ("जैव-आवर्धन (Biomagnification) व सुपोषण (Eutrophication)", "जैव-आवर्धन: खाद्य शृंखला में DDT / पारा की सांद्रता बढ़ना (जल 0.003 ppb → मछलीभक्षी पक्षी 25 ppm)", "सुपोषण: नाइट्रेट व फॉस्फेट से झील की प्राकृतिक काल-प्रभावन (शैवाल प्रस्फुटन)"),
                ("ग्रीनहाउस गैसें व ओजोन क्षरण (मॉन्ट्रियल प्रोटोकॉल)", "ग्रीनहाउस योगदान: CO₂ (60%) > CH₄ (20%) > CFCs (14%) > N₂O (6%)", "ओजोन परत मापन इकाई: डॉबसन इकाई (DU); मॉन्ट्रियल प्रोटोकॉल: 1987 (कनाडा)"),
            ],
            "diagrams_text": (
                "1. ग्रीनहाउस गैसों का सापेक्ष योगदान चार्ट:\n"
                "   • CO₂ (60%) > मीथेन CH₄ (20%) > CFCs (14%) > N₂O (6%)।\n\n"
                "2. जलीय खाद्य शृंखला में DDT का जैव-आवर्धन:\n"
                "   • जल (0.003 ppb) → प्राणिप्लवक (0.04 ppm) → छोटी मछली (0.5 ppm) → बड़ी मछली (2 ppm) → मत्स्यभक्षी पक्षी (25 ppm)।"
            ),
        },
    ],
}
# ================= VERIFIED HINDI & ENGLISH SYLLABUS DATA =================
HINDI_DATA = {
    "gadya": [
        {
            "ch": "1. बातचीत",
            "genre": "निबंध (ललित निबंध)",
            "author": "बालकृष्ण भट्ट",
            "dob": "23 जून 1844",
            "dod": "20 जुलाई 1914",
            "birthplace": "इलाहाबाद (प्रयागराज), उत्तर प्रदेश",
            "mother": "पार्वती देवी",
            "father": "बेनी प्रसाद भट्ट",
            "works": "नूतन ब्रह्मचारी, सौ अजान एक सुजान (उपन्यास); रेल का विकट खेल (नाटक); 'हिंदी प्रदीप' मासिक पत्र का 33 वर्षों तक संपादन।",
            "theme": "बातचीत मनुष्य की वाक्शक्ति का उत्तम उपयोग है। बेन जॉनसन के अनुसार बोलने से ही मनुष्य के रूप का साक्षात्कार होता है, और एडीसन के अनुसार असल बातचीत सिर्फ़ दो व्यक्तियों में हो सकती है।",
        },
        {
            "ch": "2. उसने कहा था",
            "genre": "कहानी (प्रथम विश्वयुद्ध की पृष्ठभूमि पर)",
            "author": "चंद्रधर शर्मा 'गुलेरी'",
            "dob": "7 जुलाई 1883",
            "dod": "12 सितंबर 1922",
            "birthplace": "जयपुर, राजस्थान (मूल निवास: गुलेर ग्राम, काँगड़ा, हिमाचल प्रदेश)",
            "mother": "लक्ष्मी देवी",
            "father": "पं. शिवराम",
            "works": "सुखमय जीवन (1911), बुद्धू का काँटा (1911), उसने कहा था (1915 - सरस्वती पत्रिका में प्रकाशित), कछुआ धरम, मारेसि मोहिं कुठाँव।",
            "theme": "लहना सिंह के निश्छल प्रेम, कर्तव्यनिष्ठा और सर्वोच्च बलिदान की अमर गाथा, जो सूबेदारनी के वचन की रक्षा के लिए अपने प्राण न्योछावर कर देता है।",
        },
        {
            "ch": "3. संपूर्ण क्रांति",
            "genre": "ऐतिहासिक भाषण (5 जून 1974, पटना गांधी मैदान)",
            "author": "जयप्रकाश नारायण (लोकनायक)",
            "dob": "11 अक्टूबर 1902",
            "dod": "8 अक्टूबर 1979",
            "birthplace": "सिताबदियारा गाँव (उत्तर प्रदेश के बलिया और बिहार के सारण जिले में फैला)",
            "mother": "फूलरानी",
            "father": "हरसुदयाल",
            "works": "रिकंस्ट्रक्शन ऑफ इंडियन पॉलिटी; सम्मान: भारत रत्न (1999 मरणोपरांत), मैग्सेसे सम्मान (1965)।",
            "theme": "भ्रष्टाचार, बेरोज़गारी, महँगाई और कुशासन के विरुद्ध छात्र आंदोलन एवं दलविहीन लोकतंत्र की स्थापना का आह्वान।",
        },
        {
            "ch": "4. अर्धनारीश्वर",
            "genre": "निबंध",
            "author": "रामधारी सिंह 'दिनकर' (राष्ट्रकवि)",
            "dob": "23 सितंबर 1908",
            "dod": "24 अप्रैल 1974",
            "birthplace": "सिमरिया, बेगूसराय, बिहार",
            "mother": "मनरूप देवी",
            "father": "रवि सिंह",
            "works": "उर्वशी (ज्ञानपीठ पुरस्कार), संस्कृति के चार अध्याय (साहित्य अकादमी पुरस्कार), रश्मिरथी, कुरुक्षेत्र, रेणुका, हुंकार; पद्म भूषण।",
            "theme": "शिव और पार्वती के कल्पित रूप अर्धनारीश्वर के माध्यम से स्त्री और पुरुष की पूर्ण समानता तथा दोनों में एक-दूसरे के गुणों के समन्वय पर बल।",
        },
        {
            "ch": "5. रोज",
            "genre": "कहानी (पूर्व नाम: 'गैंग्रीन')",
            "author": "सच्चिदानंद हीरानंद वात्स्यायन 'अज्ञेय'",
            "dob": "7 मार्च 1911",
            "dod": "4 अप्रैल 1987",
            "birthplace": "कसया, कुशीनगर, उत्तर प्रदेश (मूल निवास: करतारपुर, पंजाब)",
            "mother": "व्यंती देवी",
            "father": "डॉ. हीरानंद शास्त्री",
            "works": "शेखर: एक जीवनी, नदी के द्वीप, कितनी नावों में कितनी बार (ज्ञानपीठ पुरस्कार), आँगन के पार द्वार (साहित्य अकादमी पुरस्कार), तार सप्तक।",
            "theme": "मध्यवर्गीय गृहिणी मालती के यांत्रिक, नीरस और घुटन भरे वैवाहिक जीवन की मनोवैज्ञानिक पीड़ा का चित्रण।",
        },
        {
            "ch": "6. एक लेख और एक पत्र",
            "genre": "ऐतिहासिक निबंध एवं पत्र (सुखदेव के नाम)",
            "author": "भगत सिंह",
            "dob": "28 सितंबर 1907",
            "dod": "23 मार्च 1931 (शहादत, लाहौर जेल)",
            "birthplace": "बंगा चक नंबर 105, गुगेरा ब्रांच, लायलपुर (वर्तमान पाकिस्तान)",
            "mother": "विद्यावती",
            "father": "सरदार किशन सिंह",
            "works": "मैं नास्तिक क्यों हूँ, युवक, अछूत समस्या, विद्यार्थी और राजनीति।",
            "theme": "विद्यार्थियों को देश की राजनीति में सक्रिय भाग लेने की प्रेरणा तथा क्रांतिकारी जीवन में आत्महत्या को कायरता बताते हुए देशहित में शहादत का महत्व।",
        },
        {
            "ch": "7. ओ सदानीरा",
            "genre": "निबंध (यात्रा-वृत्तांत आधारित)",
            "author": "जगदीशचंद्र माथुर",
            "dob": "16 जुलाई 1917",
            "dod": "14 मई 1978",
            "birthplace": "शाहजहाँपुर, उत्तर प्रदेश",
            "mother": "उर्मिला देवी",
            "father": "लक्ष्मी नारायण माथुर",
            "works": "भोर का तारा, कोणार्क, शारदीया, पहला राजा, बोलते क्षण; सम्मान: विद्यावारिधि उपाधि, कालिदास अवॉर्ड।",
            "theme": "गंडक नदी (सदानीरा) को निमित्त बनाकर चंपारण की संस्कृति, वनों की कटाई से उत्पन्न बाढ़ की समस्या, निलहे गोरों के अत्याचार और गांधीजी के रचनात्मक कार्यों का वर्णन।",
        },
        {
            "ch": "8. सिपाही की माँ",
            "genre": "एकांकी ('अंडे के छिलके तथा अन्य एकांकी' से संकलित)",
            "author": "मोहन राकेश",
            "dob": "8 जनवरी 1925",
            "dod": "3 दिसंबर 1972",
            "birthplace": "जंडीवाली गली, अमृतसर, पंजाब",
            "mother": "बच्चन कौर",
            "father": "करमचंद गुगलानी",
            "works": "आषाढ़ का एक दिन, लहरों के राजहंस, आधे-अधूरे (नाटक); अँधेरे बंद कमरे, अंतराल (उपन्यास)।",
            "theme": "द्वितीय विश्वयुद्ध (बर्मा मोर्चे) पर गए सिपाही मानक की प्रतीक्षा करती निम्न-मध्यवर्गीय माँ बिशनी और बहन मुन्नी की मार्मिक व्यथा व मानवीय करुणा।",
        },
        {
            "ch": "9. प्रगीत और समाज",
            "genre": "आलोचनात्मक निबंध ('वाद विवाद संवाद' से संकलित)",
            "author": "नामवर सिंह",
            "dob": "28 जुलाई 1927",
            "dod": "19 फरवरी 2019",
            "birthplace": "जीयनपुर, वाराणसी (चंदौली), उत्तर प्रदेश",
            "mother": "वागेश्वरी देवी",
            "father": "नागर सिंह",
            "works": "कविता के नए प्रतिमान (साहित्य अकादमी पुरस्कार - 1971), दूसरी परंपरा की खोज, वाद विवाद संवाद, छायावाद।",
            "theme": "हिंदी कविता में प्रगीतात्मकता (Lyricism) की सामाजिक सार्थकता पर विचार; सिद्ध किया गया है कि आत्मपरक प्रगीत काव्य में भी गहरा सामाजिक यथार्थ छिपा होता है।",
        },
        {
            "ch": "10. जूठन",
            "genre": "आत्मकथा (दलित आत्मकथा)",
            "author": "ओमप्रकाश वाल्मीकि",
            "dob": "30 जून 1950",
            "dod": "17 नवंबर 2013",
            "birthplace": "बरला, मुजफ्फरनगर, उत्तर प्रदेश",
            "mother": "मकुंदी देवी",
            "father": "छोटन लाल",
            "works": "जूठन (आत्मकथा), सदियों का संताप, बस्स! बहुत हो चुका (कविता संग्रह), सलाम, घुसपैठिए (कहानी संग्रह); डॉ. अंबेडकर राष्ट्रीय पुरस्कार (1993)।",
            "theme": "जातिगत भेदभाव, अस्पृश्यता और सामाजिक उत्पीड़न के विरुद्ध शिक्षा के माध्यम से आत्मसम्मान और संघर्ष की सच्ची जीवन गाथा।",
        },
        {
            "ch": "11. हँसते हुए मेरा अकेलापन",
            "genre": "डायरी",
            "author": "मलयज (मूल नाम: भरतजी श्रीवास्तव)",
            "dob": "1935 ई.",
            "dod": "26 अप्रैल 1982",
            "birthplace": "महुई, आजमगढ़, उत्तर प्रदेश",
            "mother": "प्रभावती",
            "father": "त्रिलोकीनाथ वर्मा",
            "works": "जख्म पर धूल, अपने होने को अप्रकाशित करता हुआ (कविता); कविता से साक्षात्कार, संवाद और एकालाप (आलोचना)।",
            "theme": "एक रचनाकार के आंतरिक संघर्ष, यथार्थ से जुड़ाव और जीवन के हर क्षण को ईमानदारी से जीने की संवेदनशील अभिव्यक्ति।",
        },
        {
            "ch": "12. तिरिछ",
            "genre": "कहानी (जादुई यथार्थवाद)",
            "author": "उदय प्रकाश",
            "dob": "1 जनवरी 1952",
            "dod": "जीवित रचनाकार",
            "birthplace": "सीतापुर, अनूपपुर, मध्य प्रदेश",
            "mother": "गंगा देवी",
            "father": "प्रेम कुमार सिंह",
            "works": "दरियाई घोड़ा, तिरिछ, और अंत में प्रार्थना, पाल गोमरा का स्कूटर, मोहनदास (साहित्य अकादमी पुरस्कार)।",
            "theme": "विषैले छिपकलीनुमा जीव 'तिरिछ' के प्रतीक के माध्यम से आधुनिक शहरी समाज की संवेदनहीनता, अमानवीयता और भय का चित्रण।",
        },
        {
            "ch": "13. शिक्षा",
            "genre": "संभाषण",
            "author": "जे. कृष्णमूर्ति (जिद्दू कृष्णमूर्ति)",
            "dob": "12 मई 1895",
            "dod": "17 फरवरी 1986",
            "birthplace": "मदनपल्ली, चित्तूर, आंध्र प्रदेश",
            "mother": "संजीवम्मा",
            "father": "नारायणा जिद्दू",
            "works": "द फर्स्ट एंड लास्ट फ्रीडम, द ओनली रिवोल्यूशन, कृष्णमूर्तिज नोटबुक।",
            "theme": "सच्ची शिक्षा केवल नौकरी पाना नहीं, बल्कि भयमुक्त होकर जीवन की संपूर्ण प्रक्रिया को समझना और मेधा (Intelligence) का विकास करना है।",
        },
    ],
    "kavya": [
        {
            "ch": "1. कड़बक",
            "genre": "प्रेम की पीर का काव्य ('पद्मावत' से संकलित)",
            "author": "मलिक मुहम्मद जायसी",
            "dob": "1492 ई. (अनुमानित)",
            "dod": "1548 ई. (अनुमानित)",
            "birthplace": "जायस, कब्र अमेठी, उत्तर प्रदेश",
            "mother": "उल्लेख नहीं (अल्पायु में माता-पिता का निधन)",
            "father": "मलिक शेख ममरेज (मलिक राजे अशरफ)",
            "works": "पद्मावत, अखरावट, आखिरी कलाम, चित्ररेखा, कहरानामा।",
            "theme": "रूप की अपेक्षा गुणों की श्रेष्ठता का प्रतिपादन तथा प्रेम की पीर के माध्यम से अमर कीर्ति प्राप्त करने का संदेश।",
        },
        {
            "ch": "2. पद (सूरदास)",
            "genre": "भक्ति पद / वात्सल्य रस ('सूरसागर' से संकलित)",
            "author": "सूरदास",
            "dob": "1478 ई. (अनुमानित)",
            "dod": "1583 ई.",
            "birthplace": "दिल्ली के निकट सीही नामक ग्राम में",
            "mother": "उल्लेख नहीं",
            "father": "उल्लेख नहीं (गुरु: महाप्रभु वल्लभाचार्य)",
            "works": "सूरसागर, सूरसारावली, साहित्य लहरी, राधा-रसकेलि।",
            "theme": "प्रथम पद में बालक कृष्ण को भोर होने का संकेत देकर जगाने का वात्सल्य वर्णन तथा द्वितीय पद में नंद की गोद में बैठकर कृष्ण के भोजन करने की बाल-लीला।",
        },
        {
            "ch": "3. पद (तुलसीदास)",
            "genre": "विनय के पद ('विनय पत्रिका' से संकलित)",
            "author": "तुलसीदास (मूल नाम: रामबोला)",
            "dob": "1543 ई.",
            "dod": "1623 ई.",
            "birthplace": "राजापुर, बाँदा, उत्तर प्रदेश",
            "mother": "हुलसी",
            "father": "आत्माराम दुबे (पत्नी: रत्नावली, गुरु: नरहरिदास)",
            "works": "रामचरितमानस, विनय पत्रिका, कवितावली, गीतावली, दोहावली, कृष्ण गीतावली, हनुमान बाहुक।",
            "theme": "माँ सीता से अपनी दीन-हीन अवस्था का संदेश प्रभु श्रीराम तक पहुँचाने की विनती तथा राम की भक्ति रूपी अमृत प्राप्त करने की आकांक्षा।",
        },
        {
            "ch": "4. छप्पय",
            "genre": "छंद काव्य ('भक्तमाल' से संकलित)",
            "author": "नाभादास",
            "dob": "1570 ई. (अनुमानित)",
            "dod": "अज्ञात (1600 ई. तक वर्तमान)",
            "birthplace": "दक्षिण भारत (शैशव में अकाल के कारण जयपुर/वृंदावन प्रवास)",
            "mother": "उल्लेख नहीं",
            "father": "उल्लेख नहीं (गुरु: स्वामी अग्रदास / अग्रअली)",
            "works": "भक्तमाल (1585–1596), अष्टयाम (ब्रजभाषा गद्य में)।",
            "theme": "कबीर की निष्पक्षता व निर्भीक वाणी तथा सूरदास की अद्भुत काव्य-प्रतिभा व कृष्ण-भक्ति की महिमा का गुणगान।",
        },
        {
            "ch": "5. कवित्त",
            "genre": "वीर रस काव्य",
            "author": "भूषण",
            "dob": "1613 ई.",
            "dod": "1715 ई.",
            "birthplace": "तिकवाँपुर, कानपुर, उत्तर प्रदेश",
            "mother": "उल्लेख नहीं",
            "father": "रत्नाकर त्रिपाठी (उपनाम 'भूषण' राजा रुद्रशाह द्वारा प्रदत्त)",
            "works": "शिवराजभूषण, शिवाबावनी, छत्रसाल दशक।",
            "theme": "छत्रपति शिवाजी के अदम्य शौर्य-पराक्रम तथा महाराज छत्रसाल की तलवार (बरछी) के विनाशकारी तेज का ओजस्वी वर्णन।",
        },
        {
            "ch": "6. तुमुल कोलाहल कलह में",
            "genre": "छायावादी गीत ('कामायनी' के निर्वेद सर्ग से)",
            "author": "जयशंकर प्रसाद",
            "dob": "30 जनवरी 1889",
            "dod": "15 नवंबर 1937",
            "birthplace": "वाराणसी (काशी), उत्तर प्रदेश",
            "mother": "मुन्नी देवी",
            "father": "देवी प्रसाद साहू",
            "works": "कामायनी (महाकाव्य), आँसू, झरना, लहर; स्कंदगुप्त, चंद्रगुप्त, ध्रुवस्वामिनी (नाटक); कंकाल, तितली (उपन्यास)।",
            "theme": "जीवन के घोर कोलाहल और विषाद के बीच श्रद्धा रूपी हृदय की बात को मन की शांति, आशा और संजीवनी के रूप में चित्रित किया गया है।",
        },
        {
            "ch": "7. पुत्र वियोग",
            "genre": "शोकगीत ('मुकुल' से संकलित)",
            "author": "सुभद्रा कुमारी चौहान",
            "dob": "16 अगस्त 1904",
            "dod": "15 फरवरी 1948 (कार दुर्घटना में)",
            "birthplace": "निहालपुर, इलाहाबाद (प्रयागराज), उत्तर प्रदेश",
            "mother": "श्रीमती धीराज कुँवर",
            "father": "ठाकुर रामनाथ सिंह (पति: ठाकुर लक्ष्मण सिंह चौहान)",
            "works": "मुकुल (काव्य संग्रह - सेकसरिया पुरस्कार), बिखरे मोती, त्रिधारा, झाँसी की रानी।",
            "theme": "असमय पुत्र की मृत्यु से व्याकुल माँ के असह्य शोक, वात्सल्य और सूनेपन की अत्यंत हृदयस्पर्शी अभिव्यक्ति।",
        },
        {
            "ch": "8. उषा",
            "genre": "प्रकृति चित्रण (प्रयोगवादी कविता)",
            "author": "शमशेर बहादुर सिंह ('कवियों के कवि')",
            "dob": "13 जनवरी 1911",
            "dod": "12 मई 1993",
            "birthplace": "देहरादून, उत्तराखंड",
            "mother": "प्रभुदेई",
            "father": "तारीफ सिंह",
            "works": "चुका भी हूँ नहीं मैं (साहित्य अकादमी पुरस्कार), इतने पास अपने, बात बोलेगी, काल तुझसे होड़ है मेरी।",
            "theme": "सूर्योदय से ठीक पहले के आकाश के पल-पल बदलते प्राकृतिक सौंदर्य का नवीन ग्रामीण उपमानों (राख से लीपा चौका, काली सिल, स्लेट) से चित्रण।",
        },
        {
            "ch": "9. जन-जन का चेहरा एक",
            "genre": "प्रगतिवादी कविता ('चाँद का मुँह टेढ़ा है' से)",
            "author": "गजानन माधव मुक्तिबोध",
            "dob": "13 नवंबर 1917",
            "dod": "11 सितंबर 1964",
            "birthplace": "श्योपुर, ग्वालियर, मध्य प्रदेश",
            "mother": "पार्वती बाई",
            "father": "माधवराज मुक्तिबोध",
            "works": "चाँद का मुँह टेढ़ा है, भूरी-भूरी खाक धूल (कविता); काठ का सपना (कहानी); एक साहित्यिक की डायरी।",
            "theme": "पूरी दुनिया के शोषित-पीड़ित मनुष्यों की एकता, संघर्ष और न्याय के लिए उठने वाली क्रांति की ज्वाला की समानता का उद्घोष।",
        },
        {
            "ch": "10. अधिनायक",
            "genre": "व्यंग्य कविता ('आत्महत्या के विरुद्ध' से)",
            "author": "रघुवीर सहाय",
            "dob": "9 दिसंबर 1929",
            "dod": "30 दिसंबर 1990",
            "birthplace": "लखनऊ, उत्तर प्रदेश",
            "mother": "तारा देवी",
            "father": "हरदेव सहाय",
            "works": "सीढ़ियों पर धूप में, आत्महत्या के विरुद्ध, हँसो-हँसो जल्दी हँसो, लोग भूल गए हैं (साहित्य अकादमी पुरस्कार)।",
            "theme": "आज़ादी के बाद सत्ताधारियों की तानाशाही प्रवृत्ति और आम गरीब जनता ('हरचरना') की विवशता पर तीखा राजनीतिक व्यंग्य।",
        },
        {
            "ch": "11. प्यारे नन्हे बेटे को",
            "genre": "कविता ('वह आदमी नया गरम कोट पहनकर चला गया' से)",
            "author": "विनोद कुमार शुक्ल",
            "dob": "1 जनवरी 1937",
            "dod": "जीवित रचनाकार",
            "birthplace": "राजनांदगाँव, छत्तीसगढ़ (निवास: रायपुर)",
            "mother": "उल्लेख नहीं",
            "father": "उल्लेख नहीं",
            "works": "लगभग जयहिंद, वह आदमी नया गरम कोट पहनकर चला गया विचार की तरह; नौकर की कमीज, दीवार में एक खिड़की रहती थी (साहित्य अकादमी पुरस्कार), ज्ञानपीठ पुरस्कार।",
            "theme": "श्रम और मेहनत की गरिमा का बोध—हर मेहनतकश व्यक्ति 'लोहा' है और हर वह औरत जो दबी-सताई बोझ उठाती है, 'लोहा' है।",
        },
        {
            "ch": "12. हार-जीत",
            "genre": "गद्य कविता ('कहीं नहीं वहीं' से संकलित)",
            "author": "अशोक वाजपेयी",
            "dob": "16 जनवरी 1941",
            "dod": "जीवित रचनाकार",
            "birthplace": "दुर्ग, छत्तीसगढ़ (मूल निवास: सागर, मध्य प्रदेश)",
            "mother": "निर्मला देवी",
            "father": "परमानंद वाजपेयी",
            "works": "शहर अब भी संभावना है, एक पतंग अनंत में, तत्पुरुष, कहीं नहीं वहीं (साहित्य अकादमी पुरस्कार)।",
            "theme": "युद्ध की वास्तविकता से अनजान उत्सव मनाते नागरिकों और सड़क सींचने वाले बूढ़े 'मशकवाले' के माध्यम से सत्ता के झूठे प्रचार पर प्रश्नचिह्न।",
        },
        {
            "ch": "13. गाँव का घर",
            "genre": "कविता ('संशयात्मा' से संकलित)",
            "author": "ज्ञानेंद्रपति",
            "dob": "1 जनवरी 1950",
            "dod": "जीवित रचनाकार",
            "birthplace": "पथरगामा, गोड्डा, झारखंड",
            "mother": "सरला देवी",
            "father": "देवेंद्र प्रसाद चौबे",
            "works": "आँख हाथ बनते हुए, शब्द लिखने के लिए ही यह कागज बना है, गंगातट, संशयात्मा (साहित्य अकादमी पुरस्कार - 2006)।",
            "theme": "आधुनिकीकरण और शहरीकरण की चकाचौंध में विलुप्त होती ग्रामीण संस्कृति, पंचायती राज में खोए हुए पंच परमेश्वर और लोकगीतों की पीड़ा।",
        },
    ],
}

ENGLISH_DATA = {
    "prose": [
        {
            "ch": "1. Indian Civilization and Culture",
            "genre": "Essay",
            "author": "Mahatma Gandhi (Mohandas Karamchand Gandhi)",
            "dob": "2 October 1869",
            "dod": "30 January 1948",
            "birthplace": "Porbandar, Gujarat, India",
            "father": "Karamchand Gandhi",
            "mother": "Putlibai Gandhi",
            "works": "My Experiments with Truth (Autobiography), Hind Swaraj, Young India, Harijan.",
            "theme": "Compares ancient Indian spiritual civilization with modern Western material civilization, proving that Indian civilization elevates moral values and self-control.",
        },
        {
            "ch": "2. Bharat is My Home",
            "genre": "Speech (Delivered in 1967 upon taking oath as President)",
            "author": "Dr. Zakir Husain",
            "dob": "8 February 1897",
            "dod": "3 May 1969",
            "birthplace": "Hyderabad, India",
            "father": "Fida Husain Khan",
            "mother": "Naznin Begum",
            "works": "3rd President of India (1967–1969), Governor of Bihar (1957–1962); Awarded Bharat Ratna (1963).",
            "theme": "Pledges total loyalty to India's composite culture, emphasizes education as the prime instrument of national purpose, and stresses both individual and social work.",
        },
        {
            "ch": "3. A Pinch of Snuff",
            "genre": "Comic Short Story",
            "author": "Manohar Malgonkar",
            "dob": "12 July 1913",
            "dod": "14 June 2010",
            "birthplace": "Jagalbet, Karnataka, India",
            "father": "Dattoba Malgonkar",
            "mother": "Parvati Bai",
            "works": "Distant Drum, The Princes, A Bend in the Ganges, Bombay Beware.",
            "theme": "A witty and humorous story showcasing the cleverness, bluffing skill, and presence of mind of Nanukaka in getting his work done in Delhi.",
        },
        {
            "ch": "4. I Have a Dream",
            "genre": "Speech (Lincoln Memorial, Washington D.C., 28 Aug 1963)",
            "author": "Martin Luther King, Jr.",
            "dob": "15 January 1929",
            "dod": "4 April 1968 (Assassinated in Memphis)",
            "birthplace": "Atlanta, Georgia, USA",
            "father": "Martin Luther King Sr.",
            "mother": "Alberta Williams King",
            "works": "Stride Toward Freedom, Strength to Love, Why We Can't Wait; Nobel Peace Prize (1964).",
            "theme": "Advocates non-violent struggle against racial segregation and discrimination in America, envisioning a future where people are judged by character, not skin colour.",
        },
        {
            "ch": "5. Ideas That Have Helped Mankind",
            "genre": "Essay",
            "author": "Bertrand Russell",
            "dob": "18 May 1872",
            "dod": "2 February 1970",
            "birthplace": "Trellech, Monmouthshire, Wales, UK",
            "father": "John Russell, Viscount Amberley",
            "mother": "Katharine Louisa Stanley",
            "works": "Principia Mathematica, A History of Western Philosophy, The Conquest of Happiness; Nobel Prize in Literature (1950).",
            "theme": "Traces how human intellect, language, fire, agriculture, and moral cooperation helped mankind survive and progress from savagery to civilization.",
        },
        {
            "ch": "6. The Artist",
            "genre": "Short Story",
            "author": "Shiga Naoya",
            "dob": "20 February 1883",
            "dod": "21 October 1971",
            "birthplace": "Ishinomaki, Miyagi Prefecture, Japan",
            "father": "Naoharu Shiga",
            "mother": "Gin Shiga",
            "works": "A Dark Night's Passing (An'ya Koro), At Kinosaki, Seibei's Gourds.",
            "theme": "Highlights how rigid adults and traditional teachers often crush a child's natural artistic talent—shown through 12-year-old Seibei's passion for polishing gourds.",
        },
        {
            "ch": "7. A Child is Born",
            "genre": "Essay (Excerpt from 'Sex and Destiny')",
            "author": "Germaine Greer",
            "dob": "29 January 1939",
            "dod": "Living Author",
            "birthplace": "Melbourne, Victoria, Australia",
            "father": "Eric Reginald Greer",
            "mother": "Margaret May Lafrank",
            "works": "The Female Eunuch (1970), Sex and Destiny: The Politics of Human Fertility (1984), The Change.",
            "theme": "Contrasts traditional family-supported childbirth customs in societies like Bangladesh and India with isolated, high-tech hospital births in Western countries.",
        },
        {
            "ch": "8. How Free is the Press",
            "genre": "Essay",
            "author": "Dorothy L. Sayers",
            "dob": "13 June 1893",
            "dod": "17 December 1957",
            "birthplace": "Oxford, England, UK",
            "father": "Rev. Henry Sayers",
            "mother": "Helen Mary Leigh",
            "works": "Clouds of Witness, Unnatural Death, Nine Tailors, Gaudy Night.",
            "theme": "Exposes how press freedom in democracies is secretly controlled by two major forces: wealthy advertisers and the personal interests of newspaper proprietors.",
        },
        {
            "ch": "9. The Earth",
            "genre": "Short Story",
            "author": "H. E. Bates (Herbert Ernest Bates)",
            "dob": "16 May 1905",
            "dod": "29 January 1974",
            "birthplace": "Rushden, Northamptonshire, England, UK",
            "father": "Albert Ernest Bates",
            "mother": "Lucy Elizabeth Lucas",
            "works": "The Two Sisters, Fair Stood the Wind for France, The Purple Plain, The Jacaranda Tree.",
            "theme": "Depicts blind parental indulgence turning into tragedy as simple-minded Benjy grows cunning and selfish, eventually driving his own parents out of their home.",
        },
        {
            "ch": "10. India Through a Traveller's Eyes",
            "genre": "Travelogue / Essay",
            "author": "Pearl S. Buck (Pearl Sydenstricker Buck)",
            "dob": "26 June 1892",
            "dod": "6 March 1973",
            "birthplace": "Hillsboro, West Virginia, USA",
            "father": "Absalom Sydenstricker",
            "mother": "Caroline Stulting",
            "works": "The Good Earth (Pulitzer Prize 1932), Come, My Beloved, My Several Worlds; Nobel Prize in Literature (1938).",
            "theme": "Shares her deep love for India's spiritual heritage, village life, selfless intellectuals, and reverence for sacrifice inspired by Mahatma Gandhi.",
        },
        {
            "ch": "11. A Marriage Proposal",
            "genre": "One-Act Play (Farce)",
            "author": "Anton Chekhov (Anton Pavlovich Chekhov)",
            "dob": "29 January 1860",
            "dod": "15 July 1904",
            "birthplace": "Taganrog, Russia",
            "father": "Pavel Yegorovich Chekhov",
            "mother": "Yevgeniya Yakovlevna Morozova",
            "works": "The Seagull, Uncle Vanya, Three Sisters, The Cherry Orchard.",
            "theme": "A satirical comedy mocking wealthy landowners (Chubukov, Natalya, and Lomov) who quarrel over trivial land and hunting dogs even during a marriage proposal.",
        },
    ],
    "poetry": [
        {
            "ch": "1. Sweetest Love I Do Not Goe",
            "genre": "Metaphysical Love Poem",
            "author": "John Donne",
            "dob": "22 January 1572",
            "dod": "31 March 1631",
            "birthplace": "London, England, UK",
            "father": "John Donne",
            "mother": "Elizabeth Heywood",
            "works": "Songs and Sonnets, The Progress of the Soul, Devotions upon Emergent Occasions.",
            "theme": "Consoles his beloved wife Anne More before a temporary journey, assuring her that true lovers remain united in soul even when physically apart.",
        },
        {
            "ch": "2. Song of Myself",
            "genre": "Free Verse Poem (from 'Leaves of Grass')",
            "author": "Walt Whitman ('The People's Poet')",
            "dob": "31 May 1819",
            "dod": "26 March 1892",
            "birthplace": "West Hills, Long Island, New York, USA",
            "father": "Walter Whitman Sr.",
            "mother": "Louisa Van Velsor",
            "works": "Leaves of Grass (1855), Drum-Taps (1865).",
            "theme": "Celebrates universal brotherhood, individual freedom, and harmony with nature, stating that every atom of his blood belongs equally to all human beings.",
        },
        {
            "ch": "3. Now the Leaves are Falling Fast",
            "genre": "Lyric Poem",
            "author": "W. H. Auden (Wystan Hugh Auden)",
            "dob": "21 February 1907",
            "dod": "29 September 1973",
            "birthplace": "York, England, UK",
            "father": "George Augustus Auden",
            "mother": "Constance Rosalie Bicknell",
            "works": "The Orators, Look, Stranger!, The Age of Anxiety (Pulitzer Prize 1948), The Shield of Achilles.",
            "theme": "Reflects human mortality and frustration in modern life—just as autumn leaves fall, human lives fade toward death, yet spiritual grace offers hope.",
        },
        {
            "ch": "4. Ode to Autumn",
            "genre": "Ode (Romantic Poetry)",
            "author": "John Keats",
            "dob": "31 October 1795",
            "dod": "23 February 1821 (aged 25, in Rome)",
            "birthplace": "Moorgate, London, England, UK",
            "father": "Thomas Keats",
            "mother": "Frances Jennings",
            "works": "Endymion, Lamia, Hyperion, Ode to a Nightingale, Ode on a Grecian Urn, To Autumn.",
            "theme": "Personifies Autumn as a season of ripeness, abundance, and serene beauty, showing that autumn has its own distinct music equal to spring.",
        },
        {
            "ch": "5. An Epitaph",
            "genre": "Epitaph / Short Lyric",
            "author": "Walter de la Mare",
            "dob": "25 April 1873",
            "dod": "22 June 1956",
            "birthplace": "Charlton, Kent, England, UK",
            "father": "James Edward de la Mare",
            "mother": "Lucy Sophia Browning",
            "works": "Songs for Children, Peacock Pie, The Listeners; Order of Merit (1953).",
            "theme": "Mourns a beautiful lady of the West Country lying in her grave, reminding us that physical beauty vanishes with time and only love keeps memory alive.",
        },
        {
            "ch": "6. The Soldier",
            "genre": "War Sonnet (Patriotic Petrarchan Sonnet)",
            "author": "Rupert Brooke",
            "dob": "3 August 1887",
            "dod": "23 April 1915 (during World War I)",
            "birthplace": "Rugby, Warwickshire, England, UK",
            "father": "William Parker Brooke",
            "mother": "Mary Ruth Cotterill",
            "works": "1914 and Other Poems (Five War Sonnets including 'The Soldier').",
            "theme": "Expresses selfless patriotism: if the soldier dies on foreign soil, that corner of land will forever remain England because his body was shaped by England.",
        },
        {
            "ch": "7. Macavity: The Mystery Cat",
            "genre": "Humorous / Mock-Heroic Poem",
            "author": "T. S. Eliot (Thomas Stearns Eliot)",
            "dob": "26 September 1888",
            "dod": "4 January 1965",
            "birthplace": "St. Louis, Missouri, USA",
            "father": "Henry Ware Eliot",
            "mother": "Charlotte Champe Stearns",
            "works": "The Waste Land, The Love Song of J. Alfred Prufrock, Four Quartets, Old Possum's Book of Practical Cats; Nobel Prize in Literature (1948).",
            "theme": "A playful description of Macavity, a master criminal cat ('The Hidden Paw') who defies every law and always vanishes before Scotland Yard arrives.",
        },
        {
            "ch": "8. Fire-Hymn",
            "genre": "Poem",
            "author": "Keki N. Daruwalla",
            "dob": "24 January 1937",
            "dod": "26 September 2024",
            "birthplace": "Lahore (British India; Parsi family)",
            "father": "N. C. Daruwalla",
            "mother": "Not specified in BSEB text",
            "works": "Under Orion, Apparition in April, Crossing of Rivers, The Keeper of the Dead (Sahitya Akademi Award 1984); Padma Shri.",
            "theme": "Recalls witnessing half-burnt bodies at a burning ghat and his guilt as a Parsi for consigning his own first-born child to fire when the Tower of Silence was far away.",
        },
        {
            "ch": "9. Snake",
            "genre": "Free Verse Poem",
            "author": "D. H. Lawrence (David Herbert Lawrence)",
            "dob": "11 September 1885",
            "dod": "2 March 1930",
            "birthplace": "Eastwood, Nottinghamshire, England, UK",
            "father": "Arthur John Lawrence",
            "mother": "Lydia Beardsall",
            "works": "Sons and Lovers, The Rainbow, Women in Love, Birds, Beasts and Flowers.",
            "theme": "Depicts the conflict between natural human respect for a golden snake drinking water in Sicily and conditioned social fear that makes the poet throw a log and regret it.",
        },
        {
            "ch": "10. My Grandmother's House",
            "genre": "Autobiographical Poem",
            "author": "Kamala Das (Kamala Surayya)",
            "dob": "31 March 1934",
            "dod": "31 May 2009",
            "birthplace": "Punnayurkulam, Malabar (Kerala), India",
            "father": "V. M. Nair",
            "mother": "Balamani Amma (Renowned Malayalam Poetess)",
            "works": "Summer in Calcutta, Sirens, The Descendants, The Old Playhouse and Other Poems, My Story.",
            "theme": "Nostalgic longing for the unconditional love and warmth she experienced in her grandmother's house in Malabar, which fell silent after her grandmother's death.",
        },
    ],
}

REVISION_SUBJECTS = {
    "phy": PHYSICS_DATA,
    "chem": CHEMISTRY_DATA,
    "math": MATH_DATA,
    "bio": BIOLOGY_DATA,
}


def get_chapter_list_for_subject(subj_code: str) -> tuple[str, list[str]]:
    if subj_code in REVISION_SUBJECTS:
        d = REVISION_SUBJECTS[subj_code]
        return d["title"], [c["name"] for c in d["chapters"]]
    if subj_code == "hin":
        chs = [f"गद्य - {x['ch']}" for x in HINDI_DATA["gadya"]] + [f"काव्य - {x['ch']}" for x in HINDI_DATA["kavya"]]
        return "📖 Hindi (दिगंत भाग-2)", chs
    if subj_code == "eng":
        chs = [f"Prose - {x['ch']}" for x in ENGLISH_DATA["prose"]] + [f"Poetry - {x['ch']}" for x in ENGLISH_DATA["poetry"]]
        return "🔤 English (Rainbow Part-2)", chs
    return "Subject", []


# ================= GEMINI API HELPER (ONLY FOR AI DOUBT SOLVER) =================
async def call_gemini_doubt(prompt: str, image_bytes: bytes | None = None) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    parts = [{"text": prompt}]
    if image_bytes:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })
    payload = {"contents": [{"parts": parts}]}
    async with httpx.AsyncClient(timeout=65.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# ================= KEYBOARD MENUS =================
def root_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Study / Viewer Mode (पढ़ाई व लाइव टेस्ट)", callback_data="mode_study")],
        [InlineKeyboardButton("🛠️ Admin / Manage Mode (Add • Edit • Delete)", callback_data="mode_admin")],
    ])


def study_features_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ 📁 My Study Vault (2h Auto-Delete)", callback_data="s_vault")],
        [InlineKeyboardButton("2️⃣ 🧠 AI Doubt Solver (Fast Text)", callback_data="s_doubt")],
        [InlineKeyboardButton("3️⃣ 📚 Bihar Board Revision Bank (100% Offline)", callback_data="s_rev")],
        [InlineKeyboardButton("4️⃣ ⚠️ Mistake Bank (मेरी गलतियाँ)", callback_data="s_mis")],
        [InlineKeyboardButton("5️⃣ 📝 HTML Live MCQ Test (Timer + Score)", callback_data="s_html_test")],
        [InlineKeyboardButton("🔄 Switch Mode (Study / Admin)", callback_data="root_home")],
    ])


def admin_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Manage Study Vault (Subjects & Files)", callback_data="adm_vault")],
        [InlineKeyboardButton("⚠️ Manage Mistake Bank (Chapter Mistakes)", callback_data="adm_mis")],
        [InlineKeyboardButton("📝 Manage HTML Tests (Upload .html Test)", callback_data="adm_html_test")],
        [InlineKeyboardButton("📖 Switch to Study / Viewer Mode", callback_data="mode_study")],
        [InlineKeyboardButton("🏠 Start Screen", callback_data="root_home")],
    ])


def html_tests_kb(is_admin: bool) -> InlineKeyboardMarkup:
    kb = []
    for tid, tinfo in vault_data.get("tests", {}).items():
        q_cnt = len(tinfo.get("questions", []))
        if is_admin:
            kb.append([
                InlineKeyboardButton(f"📝 {tinfo['name'][:26]} ({q_cnt} Qs)", callback_data=f"ht_run:{tid}"),
                InlineKeyboardButton("✏️", callback_data=f"ht_ren:{tid}"),
                InlineKeyboardButton("🗑️", callback_data=f"ht_del:{tid}"),
            ])
        else:
            kb.append([InlineKeyboardButton(f"▶️ {tinfo['name']} ({q_cnt} MCQs)", callback_data=f"ht_run:{tid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("📤 नया HTML टेस्ट अपलोड करें (.html)", callback_data="ht_upload")])
        kb.append([InlineKeyboardButton("⬅️ Admin Menu", callback_data="mode_admin")])
    else:
        kb.append([InlineKeyboardButton("⬅️ Study Menu", callback_data="mode_study")])
    return InlineKeyboardMarkup(kb)


def vault_subjects_kb(is_admin: bool) -> InlineKeyboardMarkup:
    kb = []
    for sid, sinfo in vault_data.get("subjects", {}).items():
        t_cnt = len(sinfo.get("topics", {}))
        kb.append([InlineKeyboardButton(f"📘 {sinfo['name']} ({t_cnt})", callback_data=f"v_sub:{sid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("➕ नया Subject जोड़ें", callback_data="v_add_sub")])
        kb.append([InlineKeyboardButton("⬅️ Admin Menu", callback_data="mode_admin")])
    else:
        kb.append([InlineKeyboardButton("⬅️ Study Menu", callback_data="mode_study")])
    return InlineKeyboardMarkup(kb)


def vault_topics_kb(sid: str, is_admin: bool) -> InlineKeyboardMarkup:
    sinfo = vault_data["subjects"][sid]
    kb = []
    for tid, tinfo in sinfo.get("topics", {}).items():
        st_cnt = len(tinfo.get("subtopics", {}))
        kb.append([InlineKeyboardButton(f"📂 {tinfo['name']} ({st_cnt})", callback_data=f"v_top:{sid}:{tid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("➕ नया Topic जोड़ें", callback_data=f"v_add_top:{sid}")])
        kb.append([
            InlineKeyboardButton("✏️ Rename Subject", callback_data=f"v_ren_sub:{sid}"),
            InlineKeyboardButton("🗑️ Delete Subject", callback_data=f"v_del_sub:{sid}"),
        ])
    kb.append([InlineKeyboardButton("⬅️ Subjects", callback_data="adm_vault" if is_admin else "s_vault")])
    return InlineKeyboardMarkup(kb)


def vault_subtopics_kb(sid: str, tid: str, is_admin: bool) -> InlineKeyboardMarkup:
    tinfo = vault_data["subjects"][sid]["topics"][tid]
    kb = []
    for subid, subinfo in tinfo.get("subtopics", {}).items():
        f_cnt = len(subinfo.get("files", []))
        kb.append([InlineKeyboardButton(f"📁 {subinfo['name']} ({f_cnt})", callback_data=f"v_st:{sid}:{tid}:{subid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("➕ नया Sub-Topic जोड़ें", callback_data=f"v_add_st:{sid}:{tid}")])
        kb.append([
            InlineKeyboardButton("✏️ Rename Topic", callback_data=f"v_ren_top:{sid}:{tid}"),
            InlineKeyboardButton("🗑️ Delete Topic", callback_data=f"v_del_top:{sid}:{tid}"),
        ])
    kb.append([InlineKeyboardButton("⬅️ Topics", callback_data=f"v_sub:{sid}")])
    return InlineKeyboardMarkup(kb)


def vault_files_kb(sid: str, tid: str, subid: str, is_admin: bool) -> InlineKeyboardMarkup:
    subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
    kb = []
    for fitem in subinfo.get("files", []):
        if is_admin:
            kb.append([
                InlineKeyboardButton(f"{fitem.get('icon', '📄')} {fitem['name']}", callback_data=f"v_get:{sid}:{tid}:{subid}:{fitem['id']}"),
                InlineKeyboardButton("✏️", callback_data=f"v_ren_f:{sid}:{tid}:{subid}:{fitem['id']}"),
                InlineKeyboardButton("❌", callback_data=f"v_del_f:{sid}:{tid}:{subid}:{fitem['id']}"),
            ])
        else:
            kb.append([InlineKeyboardButton(f"{fitem.get('icon', '📄')} {fitem['name']}", callback_data=f"v_get:{sid}:{tid}:{subid}:{fitem['id']}")])
    if is_admin:
        kb.append([InlineKeyboardButton("📤 यहाँ फाइल अपलोड करें", callback_data=f"v_up:{sid}:{tid}:{subid}")])
        kb.append([
            InlineKeyboardButton("✏️ Rename Folder", callback_data=f"v_ren_st:{sid}:{tid}:{subid}"),
            InlineKeyboardButton("🗑️ Delete Folder", callback_data=f"v_del_st:{sid}:{tid}:{subid}"),
        ])
    kb.append([InlineKeyboardButton("⬅️ Sub-Topics", callback_data=f"v_top:{sid}:{tid}")])
    return InlineKeyboardMarkup(kb)


def revision_subjects_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚛️ Physics (भौतिकी — 14 Chapters)", callback_data="r_subj:phy")],
        [InlineKeyboardButton("🧪 Chemistry (रसायन शास्त्र — 16 Chapters)", callback_data="r_subj:chem")],
        [InlineKeyboardButton("📐 Mathematics (गणित — 13 Chapters)", callback_data="r_subj:math")],
        [InlineKeyboardButton("🧬 Biology (जीव विज्ञान — 16 Chapters)", callback_data="r_subj:bio")],
        [InlineKeyboardButton("📖 Hindi (दिगंत भाग-2 — 26 पाठ)", callback_data="r_hin")],
        [InlineKeyboardButton("🔤 English (Rainbow Part-2 — 21 Chapters)", callback_data="r_eng")],
        [InlineKeyboardButton("⬅️ Study Menu", callback_data="mode_study")],
    ])


def mistake_subjects_kb(is_admin: bool) -> InlineKeyboardMarkup:
    prefix = "ma_sub" if is_admin else "ms_sub"
    back_cb = "mode_admin" if is_admin else "mode_study"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚛️ Physics (भौतिकी)", callback_data=f"{prefix}:phy")],
        [InlineKeyboardButton("🧪 Chemistry (रसायन शास्त्र)", callback_data=f"{prefix}:chem")],
        [InlineKeyboardButton("📐 Mathematics (गणित)", callback_data=f"{prefix}:math")],
        [InlineKeyboardButton("🧬 Biology (जीव विज्ञान)", callback_data=f"{prefix}:bio")],
        [InlineKeyboardButton("📖 Hindi (दिगंत भाग-2)", callback_data=f"{prefix}:hin")],
        [InlineKeyboardButton("🔤 English (Rainbow Part-2)", callback_data=f"{prefix}:eng")],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
    ])


def mistake_chapters_kb(subj: str, is_admin: bool) -> InlineKeyboardMarkup:
    _, ch_list = get_chapter_list_for_subject(subj)
    prefix = "ma_ch" if is_admin else "ms_ch"
    back_cb = "adm_mis" if is_admin else "s_mis"
    kb = []
    for idx, ch_name in enumerate(ch_list):
        cnt = len(vault_data.get("mistakes", {}).get(f"{subj}_{idx}", []))
        badge = f" ({cnt})" if cnt > 0 else ""
        kb.append([InlineKeyboardButton(f"{ch_name}{badge}", callback_data=f"{prefix}:{subj}:{idx}")])
    kb.append([InlineKeyboardButton("⬅️ Subjects", callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)


def admin_mistake_items_kb(subj: str, idx: int) -> InlineKeyboardMarkup:
    key = f"{subj}_{idx}"
    items = vault_data.get("mistakes", {}).get(key, [])
    kb = []
    for m in items:
        icon = "🖼️" if m.get("type") == "photo" else "📝"
        kb.append([
            InlineKeyboardButton(f"{icon} {m['title'][:28]}", callback_data=f"ma_view:{subj}:{idx}:{m['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"ma_ren:{subj}:{idx}:{m['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"ma_del:{subj}:{idx}:{m['id']}"),
        ])
    kb.append([InlineKeyboardButton("➕ नई गलती / नोट जोड़ें (Text या Photo)", callback_data=f"ma_add:{subj}:{idx}")])
    kb.append([InlineKeyboardButton("⬅️ Chapters", callback_data=f"ma_sub:{subj}")])
    return InlineKeyboardMarkup(kb)


# ================= 24-HOUR STEALTH AUTH HANDLERS =================
def is_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID and time.time() < login_expiry_ts


async def cmd_suraj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return  # बाहरी यूज़र के लिए पूरी तरह Silent

    if is_authorized(user_id):
        rem_hrs = round((login_expiry_ts - time.time()) / 3600, 1)
        user_states.pop(user_id, None)
        await update.message.reply_text(
            f"👋 स्वागत है सूरज! आपका 24h सेशन एक्टिव है (शेष ~{rem_hrs} घंटे)।\n<b>Sanskari Study</b> का मोड चुनें:",
            parse_mode="HTML",
            reply_markup=root_mode_kb(),
        )
        return

    user_states[user_id] = {"step": "AUTH_WAIT_PASS"}
    await update.message.reply_text("🔐 पासवर्ड डालें (एक बार डालने पर 24 घंटे वैध रहेगा):")


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global login_expiry_ts
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    login_expiry_ts = 0.0
    user_states.pop(user_id, None)
    await update.message.reply_text("🔒 लॉगआउट हो गया। दोबारा खोलने के लिए /suraj भेजें।")


# ================= INTERACTIVE MCQ QUIZ ENGINE (WITH LIVE TIMER & MISTAKE REPORT) =================
async def send_quiz_question(target_msg, user_id: int, context: ContextTypes.DEFAULT_TYPE, edit=False):
    st = user_states.get(user_id, {})
    quiz = st.get("quiz", {})
    questions = quiz.get("questions", [])
    q_idx = quiz.get("idx", 0)
    start_ts = quiz.get("start_ts", time.time())

    if q_idx >= len(questions):
        score = quiz.get("score", 0)
        total = len(questions)
        wrong_list = quiz.get("wrongs", [])
        elapsed_str = format_elapsed_time(time.time() - start_ts)
        acc = round((score / total) * 100, 1) if total > 0 else 0
        dur_sec = quiz.get("dur_sec", 600)
        dur_label = quiz.get("dur_label", "10 मिनट")

        header_report = (
            f"🏆 <b>लाइव टेस्ट रिजल्ट (Live Test Report)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📘 <b>टेस्ट:</b> {html.escape(quiz.get('title', ''))}\n"
            f"🎯 <b>फाइनल स्कोर:</b> {score} / {total} ({acc}%)\n"
            f"✅ <b>सही:</b> {score}  |  ❌ <b>गलत:</b> {len(wrong_list)}\n"
            f"⏱️ <b>कुल लगा समय:</b> {elapsed_str}\n"
            f"⏳ <i>यह रिपोर्ट {dur_label} में ऑटो-डिलीट हो जाएगी।</i>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")]])
        sent_ids = []
        first_msg = (
            await target_msg.edit_text(header_report, parse_mode="HTML", reply_markup=kb if not wrong_list else None)
            if edit
            else await target_msg.reply_text(header_report, parse_mode="HTML", reply_markup=kb if not wrong_list else None)
        )
        sent_ids.append(first_msg.message_id)

        if wrong_list:
            labels = ["A", "B", "C", "D"]
            w_lines = ["❌ <b>गलत हुए प्रश्नों की पूरी सूची (Review Mistakes):</b>\n━━━━━━━━━━━━━━━━━━━━"]
            for w in wrong_list:
                w_lines.append(
                    f"\n<b>Q{w['q_num']}. {html.escape(w['q'])}</b>\n"
                    f"   ❌ आपका उत्तर: {labels[w['chosen']]}) {html.escape(w['chosen_txt'])}\n"
                    f"   ✅ सही उत्तर: <b>{labels[w['correct']]}) {html.escape(w['correct_txt'])}</b>"
                )
            chunks = split_long_text("\n".join(w_lines), 3700)
            for c_i, ch_txt in enumerate(chunks):
                m = await context.bot.send_message(
                    chat_id=user_id,
                    text=ch_txt,
                    parse_mode="HTML",
                    reply_markup=kb if c_i == len(chunks) - 1 else None,
                )
                sent_ids.append(m.message_id)

        schedule_custom_delete(context, user_id, sent_ids, dur_sec)
        user_states.pop(user_id, None)
        return

    item = questions[q_idx]
    opts = item["options"]
    labels = ["A", "B", "C", "D"]
    elapsed_now = format_elapsed_time(time.time() - start_ts)
    opt_lines = "\n".join([f"<b>{labels[i]})</b> {html.escape(opts[i])}" for i in range(4)])

    text = (
        f"🎯 <b>प्रश्न {q_idx + 1} / {len(questions)}</b>  |  ⏱️ <i>{elapsed_now}</i>\n"
        f"📊 लाइव स्कोर: <b>{quiz.get('score', 0)}/{q_idx}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(item['q'])}</b>\n\n"
        f"{opt_lines}\n\n"
        f"<i>सही विकल्प पर क्लिक करें:</i>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("A", callback_data="qz_ans:0"),
            InlineKeyboardButton("B", callback_data="qz_ans:1"),
            InlineKeyboardButton("C", callback_data="qz_ans:2"),
            InlineKeyboardButton("D", callback_data="qz_ans:3"),
        ],
        [InlineKeyboardButton("🏁 टेस्ट अभी सबमिट करें (Result देखें)", callback_data="qz_finish_now")],
    ])
    if edit:
        await target_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target_msg.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ================= TIMED ACTION EXECUTOR (100% FAST TEXT — NO PHOTO GENERATION) =================
async def execute_timed_action(msg_obj, user_id: int, pending: dict, dur_sec: int, dur_label: str, context: ContextTypes.DEFAULT_TYPE, from_button=False):
    action = pending.get("action")

    # 1. AI Doubt Hint (Fast Text)
    if action == "DOUBT_HINT":
        wait = await msg_obj.edit_text("💡 हिंट तैयार हो रहा है...") if from_button else await msg_obj.reply_text("💡 हिंट तैयार हो रहा है...")
        prompt = (
            "आप बिहार बोर्ड कक्षा 12वीं के शिक्षक हैं। छात्र को इस प्रश्न का पूरा उत्तर न बताकर "
            "केवल 1-2 लाइन का सटीक Hint (मुख्य सूत्र या पहला स्टेप) सरल हिंदी में दें।\n"
            f"प्रश्न: {pending.get('q_text', '')}"
        )
        try:
            ans = await call_gemini_doubt(prompt, pending.get("q_img"))
            out = await wait.edit_text(
                f"💡 <b>Hint:</b>\n{html.escape(ans)}\n\n⏳ <i>{dur_label} में ऑटो-डिलीट होगा।</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")]]),
            )
            schedule_custom_delete(context, user_id, [out.message_id], dur_sec)
        except Exception as e:
            await wait.edit_text(f"⚠️ त्रुटि: {e}")

    # 2. AI Doubt Solution (Fast Text — No Image Delay)
    elif action == "DOUBT_SOL":
        wait = (
            await msg_obj.edit_text("📝 स्टेप-बाय-स्टेप सॉल्यूशन तैयार हो रहा है...")
            if from_button
            else await msg_obj.reply_text("📝 स्टेप-बाय-स्टेप सॉल्यूशन तैयार हो रहा है...")
        )
        prompt = (
            "आप बिहार बोर्ड (BSEB) कक्षा 12वीं के अनुभवी शिक्षक हैं। नीचे दिए गए प्रश्न का "
            "100% सटीक, स्टेप-बाय-स्टेप हल सरल हिंदी में लिखें।\n"
            "क्रम: 1) दिया गया है  2) सूत्र / नियम  3) चरणबद्ध हल  4) अंतिम उत्तर (SI मात्रक सहित)।\n"
            "यूनिकोड गणित चिह्नों (×, ÷, √, π, θ, Δ, →, ², ³) का प्रयोग करें।\n"
            f"प्रश्न: {pending.get('q_text', '')}"
        )
        try:
            sol_text = await call_gemini_doubt(prompt, pending.get("q_img"))
            full_msg = (
                f"📝 <b>Bihar Board Step-by-Step Solution</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{html.escape(sol_text)}\n\n"
                f"⏳ <i>{dur_label} में ऑटो-डिलीट होगा।</i>"
            )
            chunks = split_long_text(full_msg, 3800)
            await wait.delete()
            sent_ids = []
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")]])
            for i, ch_txt in enumerate(chunks):
                m = await context.bot.send_message(
                    chat_id=user_id,
                    text=ch_txt,
                    parse_mode="HTML",
                    reply_markup=kb if i == len(chunks) - 1 else None,
                )
                sent_ids.append(m.message_id)
            schedule_custom_delete(context, user_id, sent_ids, dur_sec)
        except Exception as e:
            await wait.edit_text(f"⚠️ सॉल्यूशन त्रुटि: {e}")

    # 3. Revision Bank: Complete Formulas in Fast Text (100% Offline)
    elif action == "REV_FORMULA_SHEET":
        subj, idx = pending["subj"], pending["idx"]
        sdata = REVISION_SUBJECTS[subj]
        ch_obj = sdata["chapters"][idx]
        ch_name = ch_obj["name"]
        formulas = ch_obj["formulas"]

        if from_button:
            try:
                await msg_obj.delete()
            except Exception:
                pass

        lines = [
            f"📘 <b>{html.escape(ch_name)}</b>",
            f"<i>{html.escape(sdata['title'])} • कुल {len(formulas)} संपूर्ण सूत्र</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for i, (fname, fexpr, funit) in enumerate(formulas, 1):
            unit_part = f"\n   📌 <i>{html.escape(funit)}</i>" if funit else ""
            lines.append(f"\n<b>{i}. {html.escape(fname)}:</b>\n   🔹 <code>{html.escape(fexpr)}</code>{unit_part}")
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━\n⏳ <i>यह मैसेज {dur_label} में ऑटो-डिलीट हो जाएगा।</i>")

        chunks = split_long_text("\n".join(lines), 3800)
        sent_ids = []
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🎯 इन्हीं {len(formulas)} फॉर्मूलों का MCQ टेस्ट दें", callback_data=f"r_mcq_direct:{subj}:{idx}")],
            [InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")],
        ])
        for i, ch_txt in enumerate(chunks):
            m = await context.bot.send_message(
                chat_id=user_id,
                text=ch_txt,
                parse_mode="HTML",
                reply_markup=kb if i == len(chunks) - 1 else None,
            )
            sent_ids.append(m.message_id)
        schedule_custom_delete(context, user_id, sent_ids, dur_sec)

    # 4. Revision Bank: Biology Chapter Diagrams Labeling in Fast Text (100% Offline)
    elif action == "REV_BIO_DIAGRAMS":
        idx = pending["idx"]
        ch_obj = BIOLOGY_DATA["chapters"][idx]
        ch_name = ch_obj["name"]
        diag_txt = ch_obj.get("diagrams_text", "इस अध्याय के मुख्य नामांकित चित्र पाठ्यपुस्तक के अनुसार तैयार करें।")

        full_txt = (
            f"🧬 <b>{html.escape(ch_name)} — महत्वपूर्ण डायग्राम एवं नामांकन</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{html.escape(diag_txt)}\n\n"
            f"⏳ <i>{dur_label} में ऑटो-डिलीट हो जाएगा।</i>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")]])
        sent = (
            await msg_obj.edit_text(full_txt, parse_mode="HTML", reply_markup=kb)
            if from_button
            else await msg_obj.reply_text(full_txt, parse_mode="HTML", reply_markup=kb)
        )
        schedule_custom_delete(context, user_id, [sent.message_id], dur_sec)

    # 5. Revision Bank: Offline 1-to-1 Formula MCQs
    elif action == "REV_OFFLINE_MCQ":
        subj, idx = pending["subj"], pending["idx"]
        ch_obj = REVISION_SUBJECTS[subj]["chapters"][idx]
        qs = build_formula_mcqs(ch_obj["formulas"])
        user_states[user_id] = {
            "quiz": {
                "title": ch_obj["name"],
                "questions": qs,
                "idx": 0,
                "score": 0,
                "wrongs": [],
                "start_ts": time.time(),
                "dur_sec": dur_sec,
                "dur_label": dur_label,
            }
        }
        await send_quiz_question(msg_obj, user_id, context, edit=from_button)

    # 6. Revision Bank: Hindi / English Verified Author Notes
    elif action == "REV_LANG_NOTES":
        lang, sec, idx = pending["lang"], pending["sec"], pending["idx"]
        if lang == "hin":
            item = HINDI_DATA[sec][idx]
            card = (
                f"📖 <b>{html.escape(item['ch'])}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>विधा:</b> {html.escape(item['genre'])}\n"
                f"✍️ <b>लेखक / कवि:</b> {html.escape(item['author'])}\n"
                f"🎂 <b>जन्म तिथि:</b> {html.escape(item['dob'])}\n"
                f"🕯️ <b>निधन तिथि:</b> {html.escape(item['dod'])}\n"
                f"📍 <b>जन्म स्थान:</b> {html.escape(item['birthplace'])}\n"
                f"👩 <b>माता का नाम:</b> {html.escape(item['mother'])}\n"
                f"👨 <b>पिता का नाम:</b> {html.escape(item['father'])}\n"
                f"📚 <b>प्रमुख रचनाएँ व सम्मान:</b> {html.escape(item['works'])}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <b>मुख्य भाव (VVI):</b> {html.escape(item['theme'])}\n\n"
                f"⏳ <i>यह नोट {dur_label} में ऑटो-डिलीट हो जाएगा।</i>"
            )
        else:
            item = ENGLISH_DATA[sec][idx]
            card = (
                f"🔤 <b>{html.escape(item['ch'])}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Genre:</b> {html.escape(item['genre'])}\n"
                f"✍️ <b>Writer / Poet:</b> {html.escape(item['author'])}\n"
                f"🎂 <b>Date of Birth:</b> {html.escape(item['dob'])}\n"
                f"🕯️ <b>Date of Death:</b> {html.escape(item['dod'])}\n"
                f"📍 <b>Birthplace:</b> {html.escape(item['birthplace'])}\n"
                f"👨 <b>Father's Name:</b> {html.escape(item['father'])}\n"
                f"👩 <b>Mother's Name:</b> {html.escape(item['mother'])}\n"
                f"📚 <b>Notable Works & Honours:</b> {html.escape(item['works'])}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <b>Core Theme (VVI):</b> {html.escape(item['theme'])}\n\n"
                f"⏳ <i>Auto-deletes in {dur_label}.</i>"
            )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 इस पाठ के सारे संभावित MCQ हल करें", callback_data=f"r_lang_mcq_direct:{lang}:{sec}:{idx}")],
            [InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")],
        ])
        sent = await msg_obj.edit_text(card, parse_mode="HTML", reply_markup=kb) if from_button else await msg_obj.reply_text(card, parse_mode="HTML", reply_markup=kb)
        schedule_custom_delete(context, user_id, [sent.message_id], dur_sec)

    # 7. Revision Bank: Hindi / English All Possible Offline MCQs
    elif action == "REV_LANG_MCQ":
        lang, sec, idx = pending["lang"], pending["sec"], pending["idx"]
        all_list = HINDI_DATA[sec] if lang == "hin" else ENGLISH_DATA[sec]
        item = all_list[idx]
        qs = build_lang_mcqs(item, all_list, is_hindi=(lang == "hin"))
        user_states[user_id] = {
            "quiz": {
                "title": item["ch"],
                "questions": qs,
                "idx": 0,
                "score": 0,
                "wrongs": [],
                "start_ts": time.time(),
                "dur_sec": dur_sec,
                "dur_label": dur_label,
            }
        }
        await send_quiz_question(msg_obj, user_id, context, edit=from_button)

    # 8. Mistake Bank: View Saved Chapter Mistakes
    elif action == "VIEW_MISTAKES":
        subj, idx = pending["subj"], pending["idx"]
        _, ch_list = get_chapter_list_for_subject(subj)
        ch_name = ch_list[idx]
        key = f"{subj}_{idx}"
        items = vault_data.get("mistakes", {}).get(key, [])

        if from_button:
            try:
                await msg_obj.delete()
            except Exception:
                pass

        if not items:
            m = await context.bot.send_message(
                chat_id=user_id,
                text=f"ℹ️ <b>{html.escape(ch_name)}</b> में अभी कोई गलती सेव नहीं है।",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")]]),
            )
            schedule_custom_delete(context, user_id, [m.message_id], dur_sec)
            return

        sent_ids = []
        hdr = await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ <b>{html.escape(ch_name)} — मेरी सेव की गई गलतियाँ ({len(items)})</b>\n⏳ <i>{dur_label} में ऑटो-डिलीट होंगी:</i>",
            parse_mode="HTML",
        )
        sent_ids.append(hdr.message_id)

        for i, mitem in enumerate(items, 1):
            if mitem.get("type") == "photo" and mitem.get("msg_id"):
                cm = await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=STORAGE_CHANNEL_ID,
                    message_id=mitem["msg_id"],
                    caption=f"#{i} • {mitem['title']}",
                )
                sent_ids.append(cm.message_id)
            else:
                tm = await context.bot.send_message(
                    chat_id=user_id,
                    text=f"<b>#{i}. {html.escape(mitem['title'])}</b>\n{html.escape(mitem.get('text', ''))}",
                    parse_mode="HTML",
                )
                sent_ids.append(tm.message_id)

        nav = await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ सभी गलतियाँ भेज दी गईं (⏳ {dur_label} बाद ऑटो-डिलीट होंगी)।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 Study Menu", callback_data="mode_study")]]),
        )
        sent_ids.append(nav.message_id)
        schedule_custom_delete(context, user_id, sent_ids, dur_sec)

    # 9. Feature 5: Start Uploaded HTML Live MCQ Test
    elif action == "RUN_HTML_TEST":
        tid = pending["tid"]
        tinfo = vault_data.get("tests", {}).get(tid)
        if not tinfo or not tinfo.get("questions"):
            await msg_obj.edit_text("⚠️ यह टेस्ट उपलब्ध नहीं है।", reply_markup=study_features_kb())
            return
        user_states[user_id] = {
            "quiz": {
                "title": tinfo["name"],
                "questions": tinfo["questions"],
                "idx": 0,
                "score": 0,
                "wrongs": [],
                "start_ts": time.time(),
                "dur_sec": dur_sec,
                "dur_label": dur_label,
            }
        }
        await send_quiz_question(msg_obj, user_id, context, edit=from_button)


async def prompt_for_duration(target, user_id: int, pending_action: dict, edit=True):
    user_states[user_id] = {"step": "WAIT_DURATION", "pending": pending_action}
    msg = (
        "⏳ <b>आप इसे कितने मिनट या घंटे तक देखना चाहते हैं?</b>\n\n"
        "नीचे बटन चुनें या सीधे टाइप करें (जैसे: <code>5 मिनट</code>, <code>20 min</code>, <code>1 घंटा</code>):"
    )
    if edit:
        await target.edit_text(msg, parse_mode="HTML", reply_markup=duration_prompt_kb())
    else:
        await target.reply_text(msg, parse_mode="HTML", reply_markup=duration_prompt_kb())


# ================= CALLBACK QUERY HANDLER =================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != OWNER_ID:
        return  # बाहरी यूज़र के लिए पूरी तरह Silent

    await query.answer()

    if not is_authorized(user_id):
        user_states[user_id] = {"step": "AUTH_WAIT_PASS"}
        await query.message.reply_text("🔐 आपका 24 घंटे का सेशन समाप्त हो गया है। कृपया पासवर्ड डालें:")
        return

    cb = query.data
    parts = cb.split(":")

    # ---------- MODE SWITCHING ----------
    if cb == "root_home":
        user_states.pop(user_id, None)
        await query.edit_message_text("🏠 <b>Sanskari Study</b>\nअपना मोड चुनें:", parse_mode="HTML", reply_markup=root_mode_kb())

    elif cb == "mode_study":
        user_states[user_id] = {"mode": "STUDY"}
        await query.edit_message_text("📖 <b>Study / Viewer Mode</b>\nपढ़ाई या लाइव टेस्ट के लिए विकल्प चुनें:", parse_mode="HTML", reply_markup=study_features_kb())

    elif cb == "mode_admin":
        user_states[user_id] = {"mode": "ADMIN"}
        await query.edit_message_text("🛠️ <b>Admin / Manage Mode</b>\nयहाँ से आप सब कुछ Add, Edit और Delete कर सकते हैं:", parse_mode="HTML", reply_markup=admin_home_kb())

    # ---------- DURATION PICKER ----------
    elif parts[0] == "dur_pick":
        dur_sec = int(parts[1])
        dur_label = parts[2]
        st = user_states.get(user_id, {})
        pending = st.get("pending")
        if not pending:
            await query.edit_message_text("⚠️ सेशन समाप्त हो गया। कृपया पुनः चुनें।", reply_markup=study_features_kb())
            return
        user_states.pop(user_id, None)
        await execute_timed_action(query.message, user_id, pending, dur_sec, dur_label, context, from_button=True)

    # ---------- FEATURE 1: MY STUDY VAULT ----------
    elif cb in ("s_vault", "adm_vault"):
        is_adm = cb == "adm_vault"
        user_states[user_id] = {"mode": "ADMIN" if is_adm else "STUDY"}
        title = "🛠️ <b>Manage Study Vault</b>\nSubject चुनें या नया जोड़ें:" if is_adm else "📁 <b>My Study Vault</b>\nअपना Subject चुनें (फाइलें 2 घंटे में ऑटो-डिलीट होंगी):"
        await query.edit_message_text(title, parse_mode="HTML", reply_markup=vault_subjects_kb(is_adm))

    elif parts[0] == "v_sub":
        sid = parts[1]
        is_adm = user_states.get(user_id, {}).get("mode") == "ADMIN"
        sinfo = vault_data["subjects"].get(sid)
        if not sinfo:
            await query.edit_message_text("⚠️ Subject नहीं मिला।", reply_markup=vault_subjects_kb(is_adm))
            return
        await query.edit_message_text(f"📘 <b>{html.escape(sinfo['name'])}</b> → Topics:", parse_mode="HTML", reply_markup=vault_topics_kb(sid, is_adm))

    elif parts[0] == "v_top":
        sid, tid = parts[1], parts[2]
        is_adm = user_states.get(user_id, {}).get("mode") == "ADMIN"
        tinfo = vault_data["subjects"].get(sid, {}).get("topics", {}).get(tid)
        if not tinfo:
            await query.edit_message_text("⚠️ Topic नहीं मिला।", reply_markup=vault_subjects_kb(is_adm))
            return
        await query.edit_message_text(f"📂 <b>{html.escape(tinfo['name'])}</b> → Sub-Topics:", parse_mode="HTML", reply_markup=vault_subtopics_kb(sid, tid, is_adm))

    elif parts[0] == "v_st":
        sid, tid, subid = parts[1], parts[2], parts[3]
        is_adm = user_states.get(user_id, {}).get("mode") == "ADMIN"
        subinfo = vault_data["subjects"].get(sid, {}).get("topics", {}).get(tid, {}).get("subtopics", {}).get(subid)
        if not subinfo:
            await query.edit_message_text("⚠️ Sub-Topic नहीं मिला।", reply_markup=vault_subjects_kb(is_adm))
            return
        await query.edit_message_text(
            f"📁 <b>{html.escape(subinfo['name'])}</b>\nफाइल पर क्लिक करें (2 घंटे बाद ऑटो-डिलीट होगी):",
            parse_mode="HTML",
            reply_markup=vault_files_kb(sid, tid, subid, is_adm),
        )

    elif parts[0] == "v_get":
        sid, tid, subid, fid = parts[1], parts[2], parts[3], parts[4]
        subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
        fitem = next((f for f in subinfo["files"] if f["id"] == fid), None)
        if not fitem:
            await query.message.reply_text("⚠️ फाइल नहीं मिली।")
            return
        sent = await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=fitem["msg_id"],
            caption=f"📁 {subinfo['name']} • {fitem['name']}\n⏳ 2 घंटे में ऑटो-डिलीट हो जाएगी।",
        )
        schedule_custom_delete(context, user_id, [sent.message_id], VAULT_AUTO_DELETE_SECONDS)

    elif cb == "v_add_sub":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_ADD_SUB"}
        await query.edit_message_text("✍️ नए <b>Subject</b> का नाम लिखकर भेजें:", parse_mode="HTML")

    elif parts[0] == "v_add_top":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_ADD_TOP", "sid": parts[1]}
        await query.edit_message_text("✍️ नए <b>Topic</b> का नाम लिखकर भेजें:", parse_mode="HTML")

    elif parts[0] == "v_add_st":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_ADD_ST", "sid": parts[1], "tid": parts[2]}
        await query.edit_message_text("✍️ नए <b>Sub-Topic</b> का नाम लिखकर भेजें:", parse_mode="HTML")

    elif parts[0] == "v_ren_sub":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_SUB", "sid": parts[1]}
        await query.edit_message_text("✏️ नया नाम लिखकर भेजें:")

    elif parts[0] == "v_ren_top":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_TOP", "sid": parts[1], "tid": parts[2]}
        await query.edit_message_text("✏️ नया नाम लिखकर भेजें:")

    elif parts[0] == "v_ren_st":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_ST", "sid": parts[1], "tid": parts[2], "subid": parts[3]}
        await query.edit_message_text("✏️ नया नाम लिखकर भेजें:")

    elif parts[0] == "v_ren_f":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_F", "sid": parts[1], "tid": parts[2], "subid": parts[3], "fid": parts[4]}
        await query.edit_message_text("✏️ फाइल का नया नाम लिखकर भेजें:")

    elif parts[0] == "v_del_sub":
        vault_data["subjects"].pop(parts[1], None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ Subject डिलीट हो गया।", reply_markup=vault_subjects_kb(True))

    elif parts[0] == "v_del_top":
        sid, tid = parts[1], parts[2]
        vault_data["subjects"][sid]["topics"].pop(tid, None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ Topic डिलीट हो गया।", reply_markup=vault_topics_kb(sid, True))

    elif parts[0] == "v_del_st":
        sid, tid, subid = parts[1], parts[2], parts[3]
        vault_data["subjects"][sid]["topics"][tid]["subtopics"].pop(subid, None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ Sub-Topic डिलीट हो गया।", reply_markup=vault_subtopics_kb(sid, tid, True))

    elif parts[0] == "v_del_f":
        sid, tid, subid, fid = parts[1], parts[2], parts[3], parts[4]
        flist = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["files"]
        vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["files"] = [f for f in flist if f["id"] != fid]
        await save_and_pin_backup(context.bot)
        await query.edit_message_reply_markup(reply_markup=vault_files_kb(sid, tid, subid, True))

    elif parts[0] == "v_up":
        sid, tid, subid = parts[1], parts[2], parts[3]
        user_states[user_id] = {"mode": "ADMIN", "step": "V_BATCH_UPLOAD", "sid": sid, "tid": tid, "subid": subid}
        await query.edit_message_text(
            "📤 अब जितनी चाहें उतनी <b>PDF, Video, Photo या Audio</b> भेजें—सब सीधे इसी फोल्डर में सेव होती जाएंगी।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ अपलोड पूरा हुआ", callback_data=f"v_st:{sid}:{tid}:{subid}")]]),
        )

    # ---------- FEATURE 2: AI DOUBT SOLVER ----------
    elif cb == "s_doubt":
        user_states[user_id] = {"mode": "STUDY", "step": "DOUBT_WAIT_Q"}
        await query.edit_message_text(
            "🧠 <b>Bihar Board AI Doubt Solver</b>\n\nकिसी भी प्रश्न की <b>फोटो भेजें</b> या <b>प्रश्न लिखकर भेजें</b>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Study Menu", callback_data="mode_study")]]),
        )

    elif cb in ("d_hint", "d_sol"):
        st = user_states.get(user_id, {})
        pending = {
            "action": "DOUBT_HINT" if cb == "d_hint" else "DOUBT_SOL",
            "q_text": st.get("q_text", "इस प्रश्न को हल करें"),
            "q_img": st.get("q_img"),
        }
        await prompt_for_duration(query.message, user_id, pending, edit=True)

    # ---------- FEATURE 3: BIHAR BOARD REVISION BANK ----------
    elif cb == "s_rev":
        user_states[user_id] = {"mode": "STUDY"}
        await query.edit_message_text("📚 <b>Bihar Board Revision Bank</b>\nअपना विषय चुनें:", parse_mode="HTML", reply_markup=revision_subjects_kb())

    elif parts[0] == "r_subj":
        subj = parts[1]
        sdata = REVISION_SUBJECTS[subj]
        kb = []
        for i, ch in enumerate(sdata["chapters"]):
            fcnt = len(ch["formulas"])
            kb.append([InlineKeyboardButton(f"{ch['name']} ({fcnt} सूत्र)", callback_data=f"r_ch_opt:{subj}:{i}")])
        kb.append([InlineKeyboardButton("⬅️ Subjects", callback_data="s_rev")])
        await query.edit_message_text(f"<b>{html.escape(sdata['title'])}</b>\nचैप्टर चुनें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif parts[0] == "r_ch_opt":
        subj, idx = parts[1], int(parts[2])
        ch_obj = REVISION_SUBJECTS[subj]["chapters"][idx]
        fcnt = len(ch_obj["formulas"])
        btns = [
            [InlineKeyboardButton(f"📝 All {fcnt} Formulas (Fast Text Sheet)", callback_data=f"r_sheet_ask:{subj}:{idx}")],
            [InlineKeyboardButton(f"🎯 All {fcnt} Formulas Live MCQ Test", callback_data=f"r_mcq_ask:{subj}:{idx}")],
        ]
        if subj == "bio":
            btns.append([InlineKeyboardButton("🧬 Chapter Diagrams & Labeling", callback_data=f"r_diag_ask:{idx}")])
        btns.append([InlineKeyboardButton("⬅️ Chapters", callback_data=f"r_subj:{subj}")])
        await query.edit_message_text(f"📘 <b>{html.escape(ch_obj['name'])}</b>\nविकल्प चुनें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

    elif parts[0] == "r_sheet_ask":
        await prompt_for_duration(query.message, user_id, {"action": "REV_FORMULA_SHEET", "subj": parts[1], "idx": int(parts[2])}, edit=True)

    elif parts[0] == "r_diag_ask":
        await prompt_for_duration(query.message, user_id, {"action": "REV_BIO_DIAGRAMS", "idx": int(parts[1])}, edit=True)

    elif parts[0] in ("r_mcq_ask", "r_mcq_direct"):
        await prompt_for_duration(query.message, user_id, {"action": "REV_OFFLINE_MCQ", "subj": parts[1], "idx": int(parts[2])}, edit=True)

    elif cb == "r_hin":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ गद्य खंड (13 पाठ)", callback_data="r_hin_sec:gadya")],
            [InlineKeyboardButton("📜 काव्य खंड (13 पाठ)", callback_data="r_hin_sec:kavya")],
            [InlineKeyboardButton("⬅️ Subjects", callback_data="s_rev")],
        ])
        await query.edit_message_text("📖 <b>Hindi (दिगंत भाग-2)</b>\nखंड चुनें:", parse_mode="HTML", reply_markup=kb)

    elif parts[0] == "r_hin_sec":
        sec = parts[1]
        kb = [[InlineKeyboardButton(x["ch"], callback_data=f"r_lang_opt:hin:{sec}:{i}")] for i, x in enumerate(HINDI_DATA[sec])]
        kb.append([InlineKeyboardButton("⬅️ हिंदी खंड", callback_data="r_hin")])
        await query.edit_message_text("पाठ चुनें:", reply_markup=InlineKeyboardMarkup(kb))

    elif cb == "r_eng":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📘 Prose (11 Chapters)", callback_data="r_eng_sec:prose")],
            [InlineKeyboardButton("🖋️ Poetry (10 Chapters)", callback_data="r_eng_sec:poetry")],
            [InlineKeyboardButton("⬅️ Subjects", callback_data="s_rev")],
        ])
        await query.edit_message_text("🔤 <b>English (Rainbow Part-2)</b>\nSelect Section:", parse_mode="HTML", reply_markup=kb)

    elif parts[0] == "r_eng_sec":
        sec = parts[1]
        kb = [[InlineKeyboardButton(x["ch"], callback_data=f"r_lang_opt:eng:{sec}:{i}")] for i, x in enumerate(ENGLISH_DATA[sec])]
        kb.append([InlineKeyboardButton("⬅️ English Sections", callback_data="r_eng")])
        await query.edit_message_text("Select Chapter:", reply_markup=InlineKeyboardMarkup(kb))

    elif parts[0] == "r_lang_opt":
        lang, sec, idx = parts[1], parts[2], int(parts[3])
        item = HINDI_DATA[sec][idx] if lang == "hin" else ENGLISH_DATA[sec][idx]
        back_cb = f"r_hin_sec:{sec}" if lang == "hin" else f"r_eng_sec:{sec}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 लेखक / कवि परिचय (Notes)", callback_data=f"r_ln_ask:{lang}:{sec}:{idx}")],
            [InlineKeyboardButton("🎯 सारे संभावित Live MCQ Test", callback_data=f"r_lm_ask:{lang}:{sec}:{idx}")],
            [InlineKeyboardButton("⬅️ Chapters", callback_data=back_cb)],
        ])
        await query.edit_text(f"📘 <b>{html.escape(item['ch'])}</b>\nविकल्प चुनें:", parse_mode="HTML", reply_markup=kb)

    elif parts[0] == "r_ln_ask":
        await prompt_for_duration(query.message, user_id, {"action": "REV_LANG_NOTES", "lang": parts[1], "sec": parts[2], "idx": int(parts[3])}, edit=True)

    elif parts[0] in ("r_lm_ask", "r_lang_mcq_direct"):
        await prompt_for_duration(query.message, user_id, {"action": "REV_LANG_MCQ", "lang": parts[1], "sec": parts[2], "idx": int(parts[3])}, edit=True)

    # Interactive MCQ Answer Checking (Records Wrong Answers + Live Timer)
    elif parts[0] == "qz_ans":
        chosen = int(parts[1])
        st = user_states.get(user_id, {})
        quiz = st.get("quiz")
        if not quiz:
            return
        questions = quiz["questions"]
        q_idx = quiz["idx"]
        item = questions[q_idx]
        correct = item["ans"]
        labels = ["A", "B", "C", "D"]

        if chosen == correct:
            quiz["score"] = quiz.get("score", 0) + 1
            verdict = f"✅ <b>सही जवाब! ({labels[correct]})</b>"
        else:
            quiz.setdefault("wrongs", []).append({
                "q_num": q_idx + 1,
                "q": item["q"],
                "chosen": chosen,
                "chosen_txt": item["options"][chosen],
                "correct": correct,
                "correct_txt": item["options"][correct],
            })
            verdict = (
                f"❌ <b>गलत जवाब!</b> आपने <b>{labels[chosen]}</b> चुना।\n"
                f"✅ <b>सही उत्तर: {labels[correct]}) {html.escape(item['options'][correct])}</b>"
            )

        exp_txt = f"\n💡 <i>{html.escape(item['exp'])}</i>" if item.get("exp") else ""
        quiz["idx"] = q_idx + 1

        next_label = "➡️ अगला प्रश्न" if quiz["idx"] < len(questions) else "🏆 फाइनल रिजल्ट व गलतियाँ देखें"
        res_text = f"<b>प्रश्न {q_idx + 1}/{len(questions)}:</b> {html.escape(item['q'])}\n\n{verdict}{exp_txt}"
        await query.edit_message_text(
            res_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(next_label, callback_data="qz_next")]]),
        )

    elif cb == "qz_next":
        await send_quiz_question(query.message, user_id, context, edit=True)

    elif cb == "qz_finish_now":
        st = user_states.get(user_id, {})
        if st.get("quiz"):
            st["quiz"]["idx"] = len(st["quiz"]["questions"])
            await send_quiz_question(query.message, user_id, context, edit=True)

    # ---------- FEATURE 4: MISTAKE BANK ----------
    elif cb in ("s_mis", "adm_mis"):
        is_adm = cb == "adm_mis"
        user_states[user_id] = {"mode": "ADMIN" if is_adm else "STUDY"}
        title = "🛠️ <b>Manage Mistake Bank</b>\nविषय चुनें:" if is_adm else "⚠️ <b>Mistake Bank (मेरी गलतियाँ)</b>\nकिस विषय की गलतियाँ देखनी हैं?"
        await query.edit_message_text(title, parse_mode="HTML", reply_markup=mistake_subjects_kb(is_adm))

    elif parts[0] in ("ma_sub", "ms_sub"):
        is_adm = parts[0] == "ma_sub"
        subj = parts[1]
        stitle, _ = get_chapter_list_for_subject(subj)
        await query.edit_message_text(f"<b>{html.escape(stitle)}</b>\nचैप्टर चुनें:", parse_mode="HTML", reply_markup=mistake_chapters_kb(subj, is_adm))

    elif parts[0] == "ma_ch":
        subj, idx = parts[1], int(parts[2])
        _, ch_list = get_chapter_list_for_subject(subj)
        await query.edit_message_text(
            f"🛠️ <b>{html.escape(ch_list[idx])}</b>\nयहाँ अपनी भूली हुई चीज़ें/गलतियाँ Add, Rename या Delete करें:",
            parse_mode="HTML",
            reply_markup=admin_mistake_items_kb(subj, idx),
        )

    elif parts[0] == "ms_ch":
        await prompt_for_duration(query.message, user_id, {"action": "VIEW_MISTAKES", "subj": parts[1], "idx": int(parts[2])}, edit=True)

    elif parts[0] == "ma_add":
        user_states[user_id] = {"mode": "ADMIN", "step": "M_ADD_ITEM", "subj": parts[1], "idx": int(parts[2])}
        await query.edit_message_text("✍️ वह प्रश्न, फॉर्मूला या गलती <b>लिखकर भेजें</b> या उसकी <b>फोटो भेजें</b>:", parse_mode="HTML")

    elif parts[0] == "ma_ren":
        user_states[user_id] = {"mode": "ADMIN", "step": "M_REN_ITEM", "subj": parts[1], "idx": int(parts[2]), "mid": parts[3]}
        await query.edit_message_text("✏️ इस नोट/गलती का नया नाम या टेक्स्ट लिखकर भेजें:")

    elif parts[0] == "ma_del":
        subj, idx, mid = parts[1], int(parts[2]), parts[3]
        key = f"{subj}_{idx}"
        vault_data["mistakes"][key] = [m for m in vault_data.get("mistakes", {}).get(key, []) if m["id"] != mid]
        await save_and_pin_backup(context.bot)
        await query.edit_message_reply_markup(reply_markup=admin_mistake_items_kb(subj, idx))

    elif parts[0] == "ma_view":
        subj, idx, mid = parts[1], int(parts[2]), parts[3]
        key = f"{subj}_{idx}"
        mitem = next((m for m in vault_data.get("mistakes", {}).get(key, []) if m["id"] == mid), None)
        if not mitem:
            return
        if mitem.get("type") == "photo" and mitem.get("msg_id"):
            await context.bot.copy_message(chat_id=user_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=mitem["msg_id"], caption=mitem["title"])
        else:
            await query.message.reply_text(f"<b>{html.escape(mitem['title'])}</b>\n\n{html.escape(mitem.get('text', ''))}", parse_mode="HTML")

    # ---------- FEATURE 5: HTML LIVE MCQ TESTS ----------
    elif cb in ("s_html_test", "adm_html_test"):
        is_adm = cb == "adm_html_test"
        user_states[user_id] = {"mode": "ADMIN" if is_adm else "STUDY"}
        t_count = len(vault_data.get("tests", {}))
        title = (
            f"🛠️ <b>Manage HTML Tests ({t_count})</b>\nअपलोड किए गए टेस्ट या नया <code>.html</code> टेस्ट अपलोड करें:"
            if is_adm
            else f"📝 <b>HTML Live MCQ Tests ({t_count})</b>\nलाइव टेस्ट शुरू करने के लिए किसी भी टेस्ट पर क्लिक करें:"
        )
        await query.edit_message_text(title, parse_mode="HTML", reply_markup=html_tests_kb(is_adm))

    elif cb == "ht_upload":
        user_states[user_id] = {"mode": "ADMIN", "step": "HT_WAIT_FILE"}
        await query.edit_message_text(
            "📤 अपनी <b>HTML टेस्ट फाइल (.html)</b> यहाँ भेजें—बॉट उसमें से सारे MCQs निकालकर लाइव टेस्ट तैयार कर देगा!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_html_test")]]),
        )

    elif parts[0] == "ht_ren":
        user_states[user_id] = {"mode": "ADMIN", "step": "HT_REN", "tid": parts[1]}
        await query.edit_message_text("✏️ इस टेस्ट का नया नाम लिखकर भेजें:")

    elif parts[0] == "ht_del":
        vault_data.get("tests", {}).pop(parts[1], None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ HTML टेस्ट डिलीट हो गया।", reply_markup=html_tests_kb(True))

    elif parts[0] == "ht_run":
        await prompt_for_duration(query.message, user_id, {"action": "RUN_HTML_TEST", "tid": parts[1]}, edit=True)


# ================= TEXT & MEDIA HANDLERS =================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global login_expiry_ts
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return  # बाहरी यूज़र के लिए पूरी तरह Silent

    text = update.message.text.strip()
    st = user_states.get(user_id, {})
    step = st.get("step")

    if not is_authorized(user_id):
        if step == "AUTH_WAIT_PASS" and text == BOT_PASSWORD:
            login_expiry_ts = time.time() + LOGIN_VALIDITY_SECONDS
            user_states.pop(user_id, None)
            try:
                await update.message.delete()
            except Exception:
                pass
            await update.message.reply_text(
                "✅ <b>Sanskari Study में लॉगिन सफल!</b>\n🔓 यह सेशन अगले <b>24 घंटे</b> तक सक्रिय रहेगा। अपना मोड चुनें:",
                parse_mode="HTML",
                reply_markup=root_mode_kb(),
            )
        return

    if step == "WAIT_DURATION":
        pending = st.get("pending")
        dur_sec, dur_label = parse_duration_seconds(text)
        user_states.pop(user_id, None)
        await execute_timed_action(update.message, user_id, pending, dur_sec, dur_label, context, from_button=False)
        return

    if step == "DOUBT_WAIT_Q":
        user_states[user_id] = {"mode": "STUDY", "q_text": text, "q_img": None}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💡 Hint (1-2 लाइन क्लू)", callback_data="d_hint")],
            [InlineKeyboardButton("📝 Step-by-Step Board Solution", callback_data="d_sol")],
            [InlineKeyboardButton("⬅️ Study Menu", callback_data="mode_study")],
        ])
        await update.message.reply_text("✅ प्रश्न मिल गया! अब चुनें:", reply_markup=kb)
        return

    if step == "V_ADD_SUB":
        sid = next_id("s")
        vault_data["subjects"][sid] = {"name": text, "topics": {}}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text(f"✅ Subject <b>{html.escape(text)}</b> बन गया!", parse_mode="HTML", reply_markup=vault_topics_kb(sid, True))
        return

    if step == "V_ADD_TOP":
        sid = st["sid"]
        tid = next_id("t")
        vault_data["subjects"][sid]["topics"][tid] = {"name": text, "subtopics": {}}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text(f"✅ Topic <b>{html.escape(text)}</b> बन गया!", parse_mode="HTML", reply_markup=vault_subtopics_kb(sid, tid, True))
        return

    if step == "V_ADD_ST":
        sid, tid = st["sid"], st["tid"]
        subid = next_id("st")
        vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid] = {"name": text, "files": []}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text(f"✅ Sub-Topic <b>{html.escape(text)}</b> बन गया!", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, True))
        return

    if step == "V_REN_SUB":
        sid = st["sid"]
        vault_data["subjects"][sid]["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ नाम अपडेट हो गया।", reply_markup=vault_topics_kb(sid, True))
        return

    if step == "V_REN_TOP":
        sid, tid = st["sid"], st["tid"]
        vault_data["subjects"][sid]["topics"][tid]["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ नाम अपडेट हो गया।", reply_markup=vault_subtopics_kb(sid, tid, True))
        return

    if step == "V_REN_ST":
        sid, tid, subid = st["sid"], st["tid"], st["subid"]
        vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ नाम अपडेट हो गया।", reply_markup=vault_files_kb(sid, tid, subid, True))
        return

    if step == "V_REN_F":
        sid, tid, subid, fid = st["sid"], st["tid"], st["subid"], st["fid"]
        for fitem in vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["files"]:
            if fitem["id"] == fid:
                fitem["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ फाइल का नाम अपडेट हो गया।", reply_markup=vault_files_kb(sid, tid, subid, True))
        return

    if step == "M_ADD_ITEM":
        subj, idx = st["subj"], st["idx"]
        key = f"{subj}_{idx}"
        vault_data.setdefault("mistakes", {}).setdefault(key, [])
        mid = next_id("m")
        title = text[:35] + ("..." if len(text) > 35 else "")
        vault_data["mistakes"][key].append({"id": mid, "title": title, "text": text, "type": "text", "msg_id": None})
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✅ गलती सेव हो गई!", reply_markup=admin_mistake_items_kb(subj, idx))
        return

    if step == "M_REN_ITEM":
        subj, idx, mid = st["subj"], st["idx"], st["mid"]
        key = f"{subj}_{idx}"
        for mitem in vault_data.get("mistakes", {}).get(key, []):
            if mitem["id"] == mid:
                mitem["title"] = text[:35] + ("..." if len(text) > 35 else "")
                if mitem.get("type") == "text":
                    mitem["text"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ अपडेट हो गया!", reply_markup=admin_mistake_items_kb(subj, idx))
        return

    if step == "HT_REN":
        tid = st["tid"]
        if tid in vault_data.get("tests", {}):
            vault_data["tests"][tid]["name"] = text
            await save_and_pin_backup(context.bot)
        user_states[user_id] = {"mode": "ADMIN"}
        await update.message.reply_text("✏️ टेस्ट का नाम अपडेट हो गया!", reply_markup=html_tests_kb(True))
        return

    await update.message.reply_text("🏠 अपना मोड चुनें:", reply_markup=root_mode_kb())


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return  # बाहरी यूज़र के लिए पूरी तरह Silent

    msg = update.message
    st = user_states.get(user_id, {})
    step = st.get("step")

    # Feature 5: HTML Test File Upload
    if step == "HT_WAIT_FILE" and msg.document:
        fname = msg.document.file_name or "Live_Test.html"
        if not fname.lower().endswith((".html", ".htm")):
            await msg.reply_text("⚠️ कृपया केवल <code>.html</code> या <code>.htm</code> टेस्ट फाइल भेजें।", parse_mode="HTML")
            return
        doc_file = await msg.document.get_file()
        bio = io.BytesIO()
        await doc_file.download_to_memory(bio)
        parsed_qs = parse_html_mcq_file(bio.getvalue())
        if not parsed_qs:
            await msg.reply_text(
                "⚠️ इस HTML फाइल से प्रश्न नहीं निकल पाए। सुनिश्चित करें कि फाइल में प्रश्न, 4 विकल्प (A, B, C, D) और उत्तर मौजूद हैं।",
                reply_markup=html_tests_kb(True),
            )
            return
        tid = next_id("ht")
        clean_name = re.sub(r"\.(html|htm)$", "", fname, flags=re.I)
        vault_data.setdefault("tests", {})[tid] = {"name": clean_name, "questions": parsed_qs}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await msg.reply_text(
            f"✅ <b>HTML लाइव टेस्ट तैयार हो गया!</b>\n📘 <b>नाम:</b> {html.escape(clean_name)}\n🎯 <b>कुल प्रश्न:</b> {len(parsed_qs)} MCQs\n\nअब आप <b>Study / Viewer Mode → HTML Live MCQ Test</b> में जाकर लाइव टेस्ट दे सकते हैं!",
            parse_mode="HTML",
            reply_markup=html_tests_kb(True),
        )
        return

    if step == "DOUBT_WAIT_Q" and msg.photo:
        pfile = await msg.photo[-1].get_file()
        bio = io.BytesIO()
        await pfile.download_to_memory(bio)
        user_states[user_id] = {
            "mode": "STUDY",
            "q_text": msg.caption or "इस फोटो में दिए गए प्रश्न को हल करें।",
            "q_img": bio.getvalue(),
        }
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💡 Hint (1-2 लाइन क्लू)", callback_data="d_hint")],
            [InlineKeyboardButton("📝 Step-by-Step Board Solution", callback_data="d_sol")],
            [InlineKeyboardButton("⬅️ Study Menu", callback_data="mode_study")],
        ])
        await msg.reply_text("✅ प्रश्न की फोटो मिल गई! अब चुनें:", reply_markup=kb)
        return

    if step == "M_ADD_ITEM" and msg.photo:
        subj, idx = st["subj"], st["idx"]
        copied = await context.bot.copy_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=user_id, message_id=msg.message_id)
        key = f"{subj}_{idx}"
        vault_data.setdefault("mistakes", {}).setdefault(key, [])
        mid = next_id("m")
        title = msg.caption[:35] if msg.caption else f"Photo Mistake #{len(vault_data['mistakes'][key]) + 1}"
        vault_data["mistakes"][key].append({"id": mid, "title": title, "text": msg.caption or "", "type": "photo", "msg_id": copied.message_id})
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await msg.reply_text("✅ फोटो वाली गलती सेव हो गई!", reply_markup=admin_mistake_items_kb(subj, idx))
        return

    if step == "V_BATCH_UPLOAD":
        sid, tid, subid = st["sid"], st["tid"], st["subid"]
        if msg.document:
            icon, default_name = "📄", msg.document.file_name or "Document.pdf"
        elif msg.video:
            icon, default_name = "🎥", msg.video.file_name or (msg.caption[:40] if msg.caption else "Video Lecture")
        elif msg.photo:
            icon, default_name = "🖼️", msg.caption[:40] if msg.caption else "Handwritten Note"
        elif msg.audio or msg.voice:
            icon, default_name = "🎧", msg.caption[:40] if msg.caption else "Audio Note"
        else:
            return

        copied = await context.bot.copy_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=user_id, message_id=msg.message_id)
        fid = next_id("f")
        vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["files"].append({
            "id": fid,
            "name": default_name,
            "msg_id": copied.message_id,
            "icon": icon,
        })
        await save_and_pin_backup(context.bot)
        await msg.reply_text(f"✅ सेव हो गया: <b>{html.escape(default_name)}</b>", parse_mode="HTML")
        return

    await msg.reply_text(
        "ℹ️ फाइल अपलोड करने के लिए <b>Admin Mode</b> में जाकर संबंधित फोल्डर या <b>Manage HTML Tests</b> खोलें।",
        parse_mode="HTML",
    )


# ================= RENDER HEALTH SERVER =================
class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


def start_health_server():
    port = int(os.environ.get("PORT", 8080))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Sanskari Study Bot Active")

        def log_message(self, format, *args):
            pass

    with ReusableTCPServer(("", port), Handler) as httpd:
        httpd.serve_forever()


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(restore_from_pinned_backup).build()

    app.add_handler(CommandHandler("suraj", cmd_suraj))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.VIDEO | filters.PHOTO | filters.AUDIO | filters.VOICE,
            handle_media,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Sanskari Study Bot (Fast Text + 24h Login + HTML Live Test) running...")
    app.run_polling()


if __name__ == "__main__":
    main()
