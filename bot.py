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
VAULT_AUTO_DELETE_SECONDS = 7200
LOGIN_VALIDITY_SECONDS = 86400
DATA_FILE = "sanskari_study_data.json"
# =================================================

login_expiry_ts = 0.0
user_states = {}

def default_data():
    return {"counter": 1, "subjects": {}, "mistakes": {}, "tests": {}}

def load_local_data():
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

def next_id(prefix):
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
                sent = await bot.send_document(chat_id=STORAGE_CHANNEL_ID, document=f, filename=DATA_FILE, caption="🔄 <b>Sanskari Study Auto-Backup</b> (Pinned - डिलीट न करें)")
            await bot.pin_chat_message(chat_id=STORAGE_CHANNEL_ID, message_id=sent.message_id, disable_notification=True)
            if old_pinned_id and old_pinned_id != sent.message_id:
                try: await bot.delete_message(chat_id=STORAGE_CHANNEL_ID, message_id=old_pinned_id)
                except Exception: pass
        except Exception:
            pass

async def restore_from_pinned_backup(app):
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

def split_long_text(text, max_len=3800):
    if len(text) <= max_len: return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current: chunks.append(current)
    return chunks

def parse_duration_seconds(raw_text):
    txt = raw_text.strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)", txt)
    if not m: return 300, "5 मिनट"
    val = float(m.group(1))
    if any(k in txt for k in ["घंट", "hour", "hr", "h"]):
        return int(val * 3600), f"{int(val) if val.is_integer() else val} घंटा"
    elif any(k in txt for k in ["सेक", "sec", "s"]):
        return int(val), f"{int(val)} सेकंड"
    else:
        return int(val * 60), f"{int(val) if val.is_integer() else val} मिनट"

def format_elapsed_time(seconds):
    sec = max(1, int(seconds))
    mins, rem_s = divmod(sec, 60)
    hrs, rem_m = divmod(mins, 60)
    if hrs > 0: return f"{hrs} घंटा {rem_m} मिनट {rem_s} सेकंड"
    if mins > 0: return f"{mins} मिनट {rem_s} सेकंड"
    return f"{rem_s} सेकंड"

async def auto_delete_job(context):
    jd = context.job.data
    for mid in jd.get("message_ids", []):
        try: await context.bot.delete_message(chat_id=jd["chat_id"], message_id=mid)
        except Exception: pass

def schedule_custom_delete(context, chat_id, message_ids, seconds):
    if message_ids and context.job_queue:
        context.job_queue.run_once(auto_delete_job, when=seconds, data={"chat_id": chat_id, "message_ids": message_ids})

def duration_prompt_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱️ 5 मिनट", callback_data="dur_pick:300:5 मिनट"), InlineKeyboardButton("⏱️ 15 मिनट", callback_data="dur_pick:900:15 मिनट")],
        [InlineKeyboardButton("⏱️ 30 मिनट", callback_data="dur_pick:1800:30 मिनट"), InlineKeyboardButton("⏱️ 1 घंटा", callback_data="dur_pick:3600:1 घंटा")],
        [InlineKeyboardButton("📖 मुख्य मेनू (Study Menu)", callback_data="mode_study")]
    ])

def strip_html_tags(raw):
    clean = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    clean = re.sub(r"<[^>]+>", " ", clean)
    return re.sub(r"\s+", " ", html.unescape(clean)).strip()

def parse_html_mcq_file(html_bytes):
    raw = html_bytes.decode("utf-8", errors="ignore")
    questions = []
    js_blocks = re.findall(r"\{[^{}]*?(?:q|question|title)\s*:\s*(['\"`])(.*?)\1[^{}]*?(?:options|choices)\s*:\s*\[(.*?)\][^{}]*?(?:ans|answer|correct|right)\s*:\s*(['\"`]?)(.*?)\4[^{}]*?\}", raw, flags=re.S | re.I)
    for _, q_raw, opts_raw, _, ans_raw in js_blocks:
        q_txt = strip_html_tags(q_raw)
        opt_items = re.findall(r"(['\"`])(.*?)\1", opts_raw, flags=re.S)
        opts = [strip_html_tags(o[1]) for o in opt_items if strip_html_tags(o[1])]
        if not q_txt or len(opts) < 2: continue
        while len(opts) < 4: opts.append("उपरोक्त में से कोई नहीं")
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
                    ans_idx = idx_o; break
        questions.append({"q": q_txt, "options": opts, "ans": ans_idx, "exp": ""})
    if questions: return questions
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
            if letter_m: cur_ans = ["A", "B", "C", "D"].index(letter_m.group(1).upper())
            else:
                for idx_o, o_txt in enumerate(cur_opts):
                    if val.lower() in o_txt.lower(): cur_ans = idx_o; break
        elif exp_m: cur_exp = exp_m.group(1).strip()
        elif opt_m and cur_q and len(cur_opts) < 4: cur_opts.append(opt_m.group(2).strip())
        elif q_m:
            if cur_q and len(cur_opts) >= 2:
                while len(cur_opts) < 4: cur_opts.append("इनमें से कोई नहीं")
                questions.append({"q": cur_q, "options": cur_opts[:4], "ans": cur_ans if cur_ans is not None else 0, "exp": cur_exp})
            cur_q = q_m.group(1).strip()
            cur_opts, cur_ans, cur_exp = [], None, ""
    if cur_q and len(cur_opts) >= 2:
        while len(cur_opts) < 4: cur_opts.append("इनमें से कोई नहीं")
        questions.append({"q": cur_q, "options": cur_opts[:4], "ans": cur_ans if cur_ans is not None else 0, "exp": cur_exp})
    return questions

def build_formula_mcqs(formulas):
    if not formulas: return []
    mcqs, all_exprs, all_names = [], [f[1] for f in formulas], [f[0] for f in formulas]
    for i, (fname, fexpr, funit) in enumerate(formulas):
        if i % 2 == 0 and len(set(all_exprs)) >= 4:
            q_text, correct_val, pool = f"'{fname}' का सही सूत्र / व्यंजक क्या है?", fexpr, list({x for x in all_exprs if x != fexpr})
        else:
            q_text, correct_val, pool = f"सूत्र '{fexpr}' किसे व्यक्त करता है?", fname, list({x for x in all_names if x != fname})
        distractors = random.sample(pool, min(3, len(pool)))
        while len(distractors) < 3: distractors.append(f"इनमें से कोई नहीं ({len(distractors)+1})")
        options = distractors + [correct_val]
        random.shuffle(options)
        mcqs.append({"q": q_text, "options": options, "ans": options.index(correct_val), "exp": f"{fname}: {fexpr} ({funit})" if funit else f"{fname}: {fexpr}"})
    return mcqs

def build_lang_mcqs(item, all_items, is_hindi):
    mcqs, ch = [], item["ch"]
    def make_q(q_str, correct, field_key, fallback_pool):
        if not correct or "उल्लेख नहीं" in correct or "Not specified" in correct or "अज्ञात" in correct: return
        pool = list({x[field_key] for x in all_items if x.get(field_key) and x[field_key] != correct and "उल्लेख नहीं" not in x[field_key]})
        for fb in fallback_pool: 
            if fb != correct and fb not in pool: pool.append(fb)
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
    else:
        make_q(f"Who is the writer/poet of '{ch}'?", item["author"], "author", ["William Wordsworth", "Rabindranath Tagore", "R. K. Narayan"])
        make_q(f"What is the literary genre of '{ch}'?", item["genre"], "genre", ["Novel", "Biography", "Epic Poem"])
        make_q(f"When was {item['author']} born?", item["dob"], "dob", ["14 November 1889", "15 August 1901", "25 December 1875"])
    return mcqs

PHYSICS_DATA = {"title": "⚛️ Physics", "chapters": []}
CHEMISTRY_DATA = {"title": "🧪 Chemistry", "chapters": []}
MATH_DATA = {"title": "📐 Mathematics", "chapters": []}
BIOLOGY_DATA = {"title": "🧬 Biology", "chapters": []}
HINDI_DATA = {"gadya": [], "kavya": []}
ENGLISH_DATA = {"prose": [], "poetry": []}
REVISION_SUBJECTS = {"phy": PHYSICS_DATA, "chem": CHEMISTRY_DATA, "math": MATH_DATA, "bio": BIOLOGY_DATA}

def get_chapter_list_for_subject(subj_code):
    if subj_code in REVISION_SUBJECTS:
        d = REVISION_SUBJECTS[subj_code]
        return d["title"], [c["name"] for c in d["chapters"]]
    if subj_code == "hin":
        return "📖 Hindi (दिगंत भाग-2)", [f"गद्य - {x['ch']}" for x in HINDI_DATA["gadya"]] + [f"काव्य - {x['ch']}" for x in HINDI_DATA["kavya"]]
    if subj_code == "eng":
        return "🔤 English (Rainbow Part-2)", [f"Prose - {x['ch']}" for x in ENGLISH_DATA["prose"]] + [f"Poetry - {x['ch']}" for x in ENGLISH_DATA["poetry"]]
    return "Subject", []

async def call_gemini_doubt(prompt, image_bytes=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    parts = [{"text": prompt}]
    if image_bytes: parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode("utf-8")}})
    payload = {"contents": [{"parts": parts}]}
    async with httpx.AsyncClient(timeout=65.0) as client:
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            return f"⚠️ AI से संपर्क करने में त्रुटि: {str(e)}"

def root_mode_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 पढ़ाई मोड (Study)", callback_data="mode_study"), InlineKeyboardButton("🛠️ एडमिन मोड (Admin)", callback_data="mode_admin")]
    ])

def study_features_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 स्टडी वॉल्ट (Vault)", callback_data="s_vault"), InlineKeyboardButton("🧠 AI डाउट सॉल्वर", callback_data="s_doubt")],
        [InlineKeyboardButton("📚 रिवीजन बैंक (100% Offline)", callback_data="s_rev"), InlineKeyboardButton("⚠️ मेरी गलतियाँ (Mistake)", callback_data="s_mis")],
        [InlineKeyboardButton("📝 HTML लाइव टेस्ट (MCQ)", callback_data="s_html_test"), InlineKeyboardButton("🔄 मोड बदलें (Switch)", callback_data="root_home")]
    ])

def admin_home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 स्टडी वॉल्ट मैनेज करें", callback_data="adm_vault"), InlineKeyboardButton("⚠️ गलतियाँ मैनेज करें", callback_data="adm_mis")],
        [InlineKeyboardButton("📝 HTML टेस्ट अपलोड करें", callback_data="adm_html_test"), InlineKeyboardButton("📖 पढ़ाई मोड पर जाएं", callback_data="mode_study")],
        [InlineKeyboardButton("🏠 होम स्क्रीन", callback_data="root_home")]
    ])

def html_tests_kb(is_admin):
    kb = []
    for tid, tinfo in vault_data.get("tests", {}).items():
        q_cnt = len(tinfo.get("questions", []))
        if is_admin:
            kb.append([InlineKeyboardButton(f"📝 {tinfo['name'][:22]} ({q_cnt}Q)", callback_data=f"ht_run:{tid}"), InlineKeyboardButton("✏️ नाम बदलें", callback_data=f"ht_ren:{tid}"), InlineKeyboardButton("🗑️ डिलीट", callback_data=f"ht_del:{tid}")])
        else:
            kb.append([InlineKeyboardButton(f"▶️ {tinfo['name']} ({q_cnt} MCQs)", callback_data=f"ht_run:{tid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("📤 नया HTML टेस्ट (.html) अपलोड करें", callback_data="ht_upload")])
        kb.append([InlineKeyboardButton("⬅️ एडमिन मेनू", callback_data="mode_admin")])
    else:
        kb.append([InlineKeyboardButton("⬅️ स्टडी मेनू", callback_data="mode_study")])
    return InlineKeyboardMarkup(kb)

def vault_subjects_kb(is_admin):
    kb = []
    for sid, sinfo in vault_data.get("subjects", {}).items():
        t_cnt = len(sinfo.get("topics", {}))
        kb.append([InlineKeyboardButton(f"📘 {sinfo['name']} ({t_cnt} चैप्टर्स)", callback_data=f"v_sub:{sid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("➕ नया विषय (Subject) जोड़ें", callback_data="v_add_sub")])
        kb.append([InlineKeyboardButton("⬅️ एडमिन मेनू", callback_data="mode_admin")])
    else:
        kb.append([InlineKeyboardButton("⬅️ स्टडी मेनू", callback_data="mode_study")])
    return InlineKeyboardMarkup(kb)

def vault_topics_kb(sid, is_admin):
    sinfo = vault_data["subjects"][sid]
    kb = []
    for tid, tinfo in sinfo.get("topics", {}).items():
        st_cnt = len(tinfo.get("subtopics", {}))
        kb.append([InlineKeyboardButton(f"📂 {tinfo['name']} ({st_cnt} टॉपिक्स)", callback_data=f"v_top:{sid}:{tid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("➕ नया चैप्टर (Topic) जोड़ें", callback_data=f"v_add_top:{sid}")])
        kb.append([InlineKeyboardButton("✏️ विषय का नाम बदलें", callback_data=f"v_ren_sub:{sid}"), InlineKeyboardButton("🗑️ विषय डिलीट करें", callback_data=f"v_del_sub:{sid}")])
    kb.append([InlineKeyboardButton("⬅️ विषय सूची (Subjects)", callback_data="adm_vault" if is_admin else "s_vault")])
    return InlineKeyboardMarkup(kb)

def vault_subtopics_kb(sid, tid, is_admin):
    tinfo = vault_data["subjects"][sid]["topics"][tid]
    kb = []
    for subid, subinfo in tinfo.get("subtopics", {}).items():
        f_cnt = len(subinfo.get("files", []))
        kb.append([InlineKeyboardButton(f"📁 {subinfo['name']} ({f_cnt} फाइलें)", callback_data=f"v_st:{sid}:{tid}:{subid}")])
    if is_admin:
        kb.append([InlineKeyboardButton("➕ नया टॉपिक (Sub-Topic) जोड़ें", callback_data=f"v_add_st:{sid}:{tid}")])
        kb.append([InlineKeyboardButton("✏️ चैप्टर का नाम बदलें", callback_data=f"v_ren_top:{sid}:{tid}"), InlineKeyboardButton("🗑️ चैप्टर डिलीट करें", callback_data=f"v_del_top:{sid}:{tid}")])
    kb.append([InlineKeyboardButton("⬅️ चैप्टर्स (Topics)", callback_data=f"v_sub:{sid}")])
    return InlineKeyboardMarkup(kb)

def vault_files_kb(sid, tid, subid, is_admin):
    subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
    kb = []
    for fitem in subinfo.get("files", []):
        if is_admin:
            kb.append([InlineKeyboardButton(f"{fitem.get('icon', '📄')} {fitem['name']}", callback_data=f"v_get:{sid}:{tid}:{subid}:{fitem['id']}"), InlineKeyboardButton("✏️ नाम बदलें", callback_data=f"v_ren_f:{sid}:{tid}:{subid}:{fitem['id']}"), InlineKeyboardButton("❌ डिलीट", callback_data=f"v_del_f:{sid}:{tid}:{subid}:{fitem['id']}")])
        else:
            kb.append([InlineKeyboardButton(f"{fitem.get('icon', '📄')} {fitem['name']}", callback_data=f"v_get:{sid}:{tid}:{subid}:{fitem['id']}")])
    if is_admin:
        kb.append([InlineKeyboardButton("📤 अपनी फाइल डालें", callback_data=f"v_up:{sid}:{tid}:{subid}"), InlineKeyboardButton("📂 फाइल देखें", callback_data=f"v_st:{sid}:{tid}:{subid}")])
        kb.append([InlineKeyboardButton("✏️ नाम बदलें", callback_data=f"v_ren_st:{sid}:{tid}:{subid}"), InlineKeyboardButton("🗑️ डिलीट करें", callback_data=f"v_del_st:{sid}:{tid}:{subid}")])
    kb.append([InlineKeyboardButton("⬅️ टॉपिक्स सूची", callback_data=f"v_top:{sid}:{tid}")])
    return InlineKeyboardMarkup(kb)

def revision_subjects_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚛️ भौतिकी (Physics — 14)", callback_data="r_subj:phy"), InlineKeyboardButton("🧪 रसायन (Chemistry — 16)", callback_data="r_subj:chem")],
        [InlineKeyboardButton("📐 गणित (Math — 13)", callback_data="r_subj:math"), InlineKeyboardButton("🧬 जीव विज्ञान (Biology — 16)", callback_data="r_subj:bio")],
        [InlineKeyboardButton("📖 हिंदी (दिगंत भाग-2)", callback_data="r_hin"), InlineKeyboardButton("🔤 अंग्रेजी (Rainbow Part-2)", callback_data="r_eng")],
        [InlineKeyboardButton("⬅️ स्टडी मेनू", callback_data="mode_study")]
    ])

def mistake_subjects_kb(is_admin):
    prefix, back_cb = ("ma_sub" if is_admin else "ms_sub"), ("mode_admin" if is_admin else "mode_study")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚛️ Physics (भौतिकी)", callback_data=f"{prefix}:phy"), InlineKeyboardButton("🧪 Chemistry (रसायन)", callback_data=f"{prefix}:chem")],
        [InlineKeyboardButton("📐 Mathematics (गणित)", callback_data=f"{prefix}:math"), InlineKeyboardButton("🧬 Biology (जीव विज्ञान)", callback_data=f"{prefix}:bio")],
        [InlineKeyboardButton("📖 Hindi (हिंदी)", callback_data=f"{prefix}:hin"), InlineKeyboardButton("🔤 English (अंग्रेजी)", callback_data=f"{prefix}:eng")],
        [InlineKeyboardButton("⬅️ वापस जाएं", callback_data=back_cb)]
    ])

def mistake_chapters_kb(subj, is_admin):
    _, ch_list = get_chapter_list_for_subject(subj)
    prefix, back_cb = ("ma_ch" if is_admin else "ms_ch"), ("adm_mis" if is_admin else "s_mis")
    kb = []
    for idx, ch_name in enumerate(ch_list):
        cnt = len(vault_data.get("mistakes", {}).get(f"{subj}_{idx}", []))
        badge = f" ({cnt})" if cnt > 0 else ""
        kb.append([InlineKeyboardButton(f"{ch_name}{badge}", callback_data=f"{prefix}:{subj}:{idx}")])
    kb.append([InlineKeyboardButton("⬅️ विषय सूची", callback_data=back_cb)])
    return InlineKeyboardMarkup(kb)

def admin_mistake_items_kb(subj, idx):
    key = f"{subj}_{idx}"
    items = vault_data.get("mistakes", {}).get(key, [])
    kb = []
    for m in items:
        icon = "🖼️" if m.get("type") == "photo" else "📝"
        kb.append([InlineKeyboardButton(f"{icon} {m['title'][:25]}", callback_data=f"ma_view:{subj}:{idx}:{m['id']}"), InlineKeyboardButton("✏️", callback_data=f"ma_ren:{subj}:{idx}:{m['id']}"), InlineKeyboardButton("🗑️", callback_data=f"ma_del:{subj}:{idx}:{m['id']}")])
    kb.append([InlineKeyboardButton("➕ नई गलती / नोट जोड़ें", callback_data=f"ma_add:{subj}:{idx}")])
    kb.append([InlineKeyboardButton("⬅️ चैप्टर्स सूची", callback_data=f"ma_sub:{subj}")])
    return InlineKeyboardMarkup(kb)

def is_authorized(user_id):
    return user_id == OWNER_ID and time.time() < login_expiry_ts

async def cmd_suraj(update, context):
    global login_expiry_ts
    user_id = update.effective_user.id
    if user_id != OWNER_ID: return
    if is_authorized(user_id):
        rem_hrs = round((login_expiry_ts - time.time()) / 3600, 1)
        user_states.pop(user_id, None)
        await update.message.reply_text("👋 स्वागत है सूरज!
✨ आपका 24h सेशन एक्टिव है (शेष ~" + str(rem_hrs) + " घंटे)।

<b>Sanskari Study</b> का मोड चुनें:", parse_mode="HTML", reply_markup=root_mode_kb())
        return
    user_states[user_id] = {"step": "AUTH_WAIT_PASS"}
    await update.message.reply_text("🔐 पासवर्ड डालें (एक बार डालने पर 24 घंटे वैध रहेगा):")

async def cmd_logout(update, context):
    global login_expiry_ts
    if update.effective_user.id != OWNER_ID: return
    login_expiry_ts = 0.0
    user_states.pop(update.effective_user.id, None)
    await update.message.reply_text("🔒 सुरक्षित रूप से लॉगआउट हो गया। दोबारा खोलने के लिए /suraj भेजें।")

async def send_quiz_question(target_msg, user_id, context, edit=False):
    st = user_states.get(user_id, {})
    quiz = st.get("quiz", {})
    questions, q_idx, start_ts = quiz.get("questions", []), quiz.get("idx", 0), quiz.get("start_ts", time.time())
    if q_idx >= len(questions):
        score, total = quiz.get("score", 0), len(questions)
        wrong_list, elapsed_str = quiz.get("wrongs", []), format_elapsed_time(time.time() - start_ts)
        acc = round((score / total) * 100, 1) if total > 0 else 0
        dur_sec, dur_label = quiz.get("dur_sec", 600), quiz.get("dur_label", "10 मिनट")
        correct_list_str = ", ".join([str(x) for x in quiz.get("correct_indices", [])]) if quiz.get("correct_indices") else ("सभी सही" if score == total else "कोई नहीं")
        wrong_list_str = ", ".join([str(w["q_num"]) for w in wrong_list]) if wrong_list else "कोई नहीं 🌟"
        header_report = f"🏆 <b>लाइव टेस्ट स्कोर कार्ड (Live Test Scorecard)</b>
━━━━━━━━━━━━━━━━━━━━
📘 <b>टेस्ट:</b> {html.escape(quiz.get('title', ''))}
🎯 <b>फाइनल स्कोर:</b> {score} / {total} ({acc}%)
✅ <b>सही प्रश्न:</b> {score} ({correct_list_str} ✅)
❌ <b>गलत प्रश्न:</b> {len(wrong_list)} ({wrong_list_str} ❌)
⏱️ <b>कुल लगा समय:</b> {elapsed_str}
⏳ <i>यह रिपोर्ट {dur_label} में ऑटो-डिलीट हो जाएगी।</i>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू (Study Menu)", callback_data="mode_study")]])
        sent_ids = []
        first_msg = await target_msg.edit_text(header_report, parse_mode="HTML", reply_markup=kb if not wrong_list else None) if edit else await target_msg.reply_text(header_report, parse_mode="HTML", reply_markup=kb if not wrong_list else None)
        sent_ids.append(first_msg.message_id)
        if wrong_list:
            labels = ["A", "B", "C", "D"]
            w_lines = ["❌ <b>गलत हुए प्रश्नों की पूरी सूची (Review Mistakes):</b>
━━━━━━━━━━━━━━━━━━━━"]
            for w in wrong_list:
                w_lines.append(f"
<b>Q{w['q_num']}. {html.escape(w['q'])}</b>
   ❌ आपका उत्तर: {labels[w['chosen']]}) {html.escape(w['chosen_txt'])}
   ✅ सही उत्तर: <b>{labels[w['correct']]}) {html.escape(w['correct_txt'])}</b>")
            for c_i, ch_txt in enumerate(split_long_text("
".join(w_lines), 3700)):
                m = await context.bot.send_message(chat_id=user_id, text=ch_txt, parse_mode="HTML", reply_markup=kb if c_i == len(split_long_text("
".join(w_lines), 3700)) - 1 else None)
                sent_ids.append(m.message_id)
        schedule_custom_delete(context, user_id, sent_ids, dur_sec)
        user_states.pop(user_id, None)
        return
    item = questions[q_idx]
    opts, labels = item["options"], ["A", "B", "C", "D"]
    elapsed_now = format_elapsed_time(time.time() - start_ts)
    opt_lines = "
".join([f"<b>{labels[i]})</b> {html.escape(opts[i])}" for i in range(4)])
    text = f"🎯 <b>प्रश्न {q_idx + 1} / {len(questions)}</b>  |  ⏱️ <i>{elapsed_now}</i>
📊 लाइव स्कोर: <b>{quiz.get('score', 0)}/{q_idx}</b>
━━━━━━━━━━━━━━━━━━━━
<b>{html.escape(item['q'])}</b>

{opt_lines}

<i>सही विकल्प पर क्लिक करें:</i>"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("A", callback_data="qz_ans:0"), InlineKeyboardButton("B", callback_data="qz_ans:1"), InlineKeyboardButton("C", callback_data="qz_ans:2"), InlineKeyboardButton("D", callback_data="qz_ans:3")], [InlineKeyboardButton("🏁 टेस्ट सबमिट करें (Result)", callback_data="qz_finish_now")]])
    if edit: await target_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else: await target_msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def execute_timed_action(msg_obj, user_id, pending, dur_sec, dur_label, context, from_button=False):
    action = pending.get("action")
    if action == "DOUBT_HINT":
        wait = await msg_obj.edit_text("💡 हिंट तैयार हो रहा है...") if from_button else await msg_obj.reply_text("💡 हिंट तैयार हो रहा है...")
        prompt = f"आप बिहार बोर्ड कक्षा 12वीं के शिक्षक हैं। छात्र को इस प्रश्न का पूरा उत्तर न बताकर केवल 1-2 लाइन का सटीक Hint (मुख्य सूत्र या पहला स्टेप) सरल हिंदी में दें。
प्रश्न: {pending.get('q_text', '')}"
        try:
            ans = await call_gemini_doubt(prompt, pending.get("q_img"))
            out = await wait.edit_text(f"💡 <b>सटीक Hint:</b>
{html.escape(ans)}

⏳ <i>{dur_label} में ऑटो-डिलीट होगा।</i>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]]))
            schedule_custom_delete(context, user_id, [out.message_id], dur_sec)
        except Exception as e: await wait.edit_text(f"⚠️ त्रुटि: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]]))
    elif action == "DOUBT_SOL":
        wait = await msg_obj.edit_text("📝 स्टेप-बाय-स्टेप सॉल्यूशन तैयार हो रहा है...") if from_button else await msg_obj.reply_text("📝 स्टेप-बाय-स्टेप सॉल्यूशन तैयार हो रहा है...")
        prompt = f"आप बिहार बोर्ड (BSEB) कक्षा 12वीं के अनुभवी शिक्षक हैं। नीचे दिए गए प्रश्न का 100% सटीक, स्टेप-बाय-स्टेप हल सरल हिंदी में लिखें。
क्रम: 1) दिया गया है  2) सूत्र / नियम  3) चरणबद्ध हल  4) अंतिम उत्तर (SI मात्रक सहित)।
यूनिकोड गणित चिह्नों (×, ÷, √, π, θ, Δ, →, ², ³) का प्रयोग करें。
प्रश्न: {pending.get('q_text', '')}"
        try:
            sol_text = await call_gemini_doubt(prompt, pending.get("q_img"))
            full_msg = f"📝 <b>Bihar Board Step-by-Step Solution</b>
━━━━━━━━━━━━━━━━━━━━
{html.escape(sol_text)}

⏳ <i>{dur_label} में ऑटो-डिलीट होगा।</i>"
            try: await wait.delete()
            except Exception: pass
            sent_ids, kb = [], InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]])
            for i, ch_txt in enumerate(split_long_text(full_msg, 3800)):
                m = await context.bot.send_message(chat_id=user_id, text=ch_txt, parse_mode="HTML", reply_markup=kb if i == len(split_long_text(full_msg, 3800)) - 1 else None)
                sent_ids.append(m.message_id)
            schedule_custom_delete(context, user_id, sent_ids, dur_sec)
        except Exception as e: await msg_obj.reply_text(f"⚠️ सॉल्यूशन त्रुटि: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]]))
    elif action == "REV_FORMULA_SHEET":
        subj, idx = pending["subj"], pending["idx"]
        sdata, ch_obj = REVISION_SUBJECTS[subj], REVISION_SUBJECTS[subj]["chapters"][idx]
        if from_button: 
            try: await msg_obj.delete()
            except Exception: pass
        lines = [f"📘 <b>{html.escape(ch_obj['name'])}</b>", f"<i>{html.escape(sdata['title'])} • कुल {len(ch_obj['formulas'])} संपूर्ण सूत्र</i>", "━━━━━━━━━━━━━━━━━━━━"]
        for i, (fname, fexpr, funit) in enumerate(ch_obj["formulas"], 1):
            unit_part = f"
   📌 <i>{html.escape(funit)}</i>" if funit else ""
            lines.append(f"
<b>{i}. {html.escape(fname)}:</b>
   🔹 <code>{html.escape(fexpr)}</code>{unit_part}")
        lines.append(f"
━━━━━━━━━━━━━━━━━━━━
⏳ <i>यह मैसेज {dur_label} में ऑटो-डिलीट हो जाएगा।</i>")
        sent_ids, kb = [], InlineKeyboardMarkup([[InlineKeyboardButton(f"🎯 इन्हीं {len(ch_obj['formulas'])} फॉर्मूलों का MCQ टेस्ट दें", callback_data=f"r_mcq_direct:{subj}:{idx}")], [InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]])
        for i, ch_txt in enumerate(split_long_text("
".join(lines), 3800)):
            m = await context.bot.send_message(chat_id=user_id, text=ch_txt, parse_mode="HTML", reply_markup=kb if i == len(split_long_text("
".join(lines), 3800)) - 1 else None)
            sent_ids.append(m.message_id)
        schedule_custom_delete(context, user_id, sent_ids, dur_sec)
    elif action == "REV_BIO_DIAGRAMS":
        idx = pending["idx"]
        ch_obj = BIOLOGY_DATA["chapters"][idx]
        full_txt = f"🧬 <b>{html.escape(ch_obj['name'])} — महत्वपूर्ण डायग्राम एवं नामांकन</b>
━━━━━━━━━━━━━━━━━━━━
{html.escape(ch_obj.get('diagrams_text', ''))}

⏳ <i>{dur_label} में ऑटो-डिलीट हो जाएगा।</i>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]])
        sent = await msg_obj.edit_text(full_txt, parse_mode="HTML", reply_markup=kb) if from_button else await msg_obj.reply_text(full_txt, parse_mode="HTML", reply_markup=kb)
        schedule_custom_delete(context, user_id, [sent.message_id], dur_sec)
    elif action == "REV_OFFLINE_MCQ":
        subj, idx = pending["subj"], pending["idx"]
        ch_obj = REVISION_SUBJECTS[subj]["chapters"][idx]
        user_states[user_id] = {"quiz": {"title": ch_obj["name"], "questions": build_formula_mcqs(ch_obj["formulas"]), "idx": 0, "score": 0, "wrongs": [], "correct_indices": [], "start_ts": time.time(), "dur_sec": dur_sec, "dur_label": dur_label}}
        await send_quiz_question(msg_obj, user_id, context, edit=from_button)
    elif action == "REV_LANG_NOTES":
        lang, sec, idx = pending["lang"], pending["sec"], pending["idx"]
        item = HINDI_DATA[sec][idx] if lang == "hin" else ENGLISH_DATA[sec][idx]
        card = f"📖 <b>{html.escape(item['ch'])}</b>
━━━━━━━━━━━━━━━━━━━━
📌 <b>विधा:</b> {html.escape(item['genre'])}
✍️ <b>लेखक / कवि:</b> {html.escape(item['author'])}
🎂 <b>जन्म:</b> {html.escape(item['dob'])}
🕯️ <b>निधन:</b> {html.escape(item['dod'])}
📍 <b>जन्म स्थान:</b> {html.escape(item['birthplace'])}
👩 <b>माता:</b> {html.escape(item['mother'])}
👨 <b>पिता:</b> {html.escape(item['father'])}
📚 <b>प्रमुख रचनाएँ:</b> {html.escape(item['works'])}
━━━━━━━━━━━━━━━━━━━━
💡 <b>मुख्य भाव:</b> {html.escape(item['theme'])}

⏳ <i>यह नोट {dur_label} में ऑटो-डिलीट हो जाएगा।</i>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎯 इस पाठ के सारे संभावित MCQ हल करें", callback_data=f"r_lang_mcq_direct:{lang}:{sec}:{idx}")], [InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]])
        sent = await msg_obj.edit_text(card, parse_mode="HTML", reply_markup=kb) if from_button else await msg_obj.reply_text(card, parse_mode="HTML", reply_markup=kb)
        schedule_custom_delete(context, user_id, [sent.message_id], dur_sec)
    elif action == "REV_LANG_MCQ":
        lang, sec, idx = pending["lang"], pending["sec"], pending["idx"]
        all_list = HINDI_DATA[sec] if lang == "hin" else ENGLISH_DATA[sec]
        item = all_list[idx]
        user_states[user_id] = {"quiz": {"title": item["ch"], "questions": build_lang_mcqs(item, all_list, is_hindi=(lang == "hin")), "idx": 0, "score": 0, "wrongs": [], "correct_indices": [], "start_ts": time.time(), "dur_sec": dur_sec, "dur_label": dur_label}}
        await send_quiz_question(msg_obj, user_id, context, edit=from_button)
    elif action == "VIEW_MISTAKES":
        subj, idx = pending["subj"], pending["idx"]
        _, ch_list = get_chapter_list_for_subject(subj)
        ch_name, key = ch_list[idx], f"{subj}_{idx}"
        items = vault_data.get("mistakes", {}).get(key, [])
        if from_button: 
            try: await msg_obj.delete()
            except Exception: pass
        if not items:
            m = await context.bot.send_message(chat_id=user_id, text=f"ℹ️ <b>{html.escape(ch_name)}</b> में अभी कोई गलती सेव नहीं है।", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]]))
            schedule_custom_delete(context, user_id, [m.message_id], dur_sec); return
        sent_ids = [ (await context.bot.send_message(chat_id=user_id, text=f"⚠️ <b>{html.escape(ch_name)} — मेरी सेव की गई गलतियाँ ({len(items)})</b>
⏳ <i>{dur_label} में ऑटो-डिलीट होंगी:</i>", parse_mode='HTML')).message_id ]
        for i, mitem in enumerate(items, 1):
            if mitem.get("type") == "photo" and mitem.get("msg_id"):
                cm = await context.bot.copy_message(chat_id=user_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=mitem["msg_id"], caption=f"#{i} • {mitem['title']}")
                sent_ids.append(cm.message_id)
            else:
                tm = await context.bot.send_message(chat_id=user_id, text=f"<b>#{i}. {html.escape(mitem['title'])}</b>
{html.escape(mitem.get('text', ''))}", parse_mode="HTML")
                sent_ids.append(tm.message_id)
        nav = await context.bot.send_message(chat_id=user_id, text=f"✅ सभी गलतियाँ भेज दी गईं (⏳ {dur_label} बाद ऑटो-डिलीट होंगी)।", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📖 स्टडी मेनू", callback_data="mode_study")]]))
        sent_ids.append(nav.message_id)
        schedule_custom_delete(context, user_id, sent_ids, dur_sec)
    elif action == "RUN_HTML_TEST":
        tinfo = vault_data.get("tests", {}).get(pending["tid"])
        if not tinfo or not tinfo.get("questions"):
            await msg_obj.edit_text("⚠️ यह टेस्ट उपलब्ध नहीं है।", reply_markup=study_features_kb()); return
        user_states[user_id] = {"quiz": {"title": tinfo["name"], "questions": tinfo["questions"], "idx": 0, "score": 0, "wrongs": [], "correct_indices": [], "start_ts": time.time(), "dur_sec": dur_sec, "dur_label": dur_label}}
        await send_quiz_question(msg_obj, user_id, context, edit=from_button)

async def prompt_for_duration(target, user_id, pending_action, edit=True):
    user_states[user_id] = {"step": "WAIT_DURATION", "pending": pending_action}
    msg = "⏳ <b>आप इसे कितने मिनट या घंटे तक देखना चाहते हैं?</b>

नीचे बटन चुनें या सीधे टाइप करें (जैसे: <code>5 मिनट</code>, <code>20 min</code>, <code>1 घंटा</code>):"
    if edit: await target.edit_text(msg, parse_mode="HTML", reply_markup=duration_prompt_kb())
    else: await target.reply_text(msg, parse_mode="HTML", reply_markup=duration_prompt_kb())

async def handle_callbacks(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != OWNER_ID: return
    await query.answer()
    if not is_authorized(user_id):
        user_states[user_id] = {"step": "AUTH_WAIT_PASS"}
        await query.message.reply_text("🔐 आपका 24 घंटे का सेशन समाप्त हो गया है। कृपया पासवर्ड डालें:")
        return
    cb, parts = query.data, query.data.split(":")
    if cb == "root_home":
        user_states.pop(user_id, None)
        await query.edit_message_text("🏠 <b>Sanskari Study</b>
अपना मोड चुनें:", parse_mode="HTML", reply_markup=root_mode_kb())
    elif cb == "mode_study":
        user_states[user_id] = {"mode": "STUDY"}
        await query.edit_message_text("📖 <b>Study / Viewer Mode</b>
पढ़ाई या लाइव टेस्ट के लिए विकल्प चुनें:", parse_mode="HTML", reply_markup=study_features_kb())
    elif cb == "mode_admin":
        user_states[user_id] = {"mode": "ADMIN"}
        await query.edit_message_text("🛠️ <b>Admin / Manage Mode</b>
यहाँ से आप सब कुछ Add, Edit और Delete कर सकते हैं:", parse_mode="HTML", reply_markup=admin_home_kb())
    elif parts[0] == "dur_pick":
        st = user_states.get(user_id, {})
        pending = st.get("pending")
        if not pending:
            await query.edit_message_text("⚠️ सेशन समाप्त हो गया। कृपया पुनः चुनें।", reply_markup=study_features_kb()); return
        user_states.pop(user_id, None)
        await execute_timed_action(query.message, user_id, pending, int(parts[1]), parts[2], context, from_button=True)
    elif cb in ("s_vault", "adm_vault"):
        is_adm = (cb == "adm_vault")
        user_states[user_id] = {"mode": "ADMIN" if is_adm else "STUDY"}
        title = "🛠️ <b>Manage Study Vault</b>
विषय चुनें या नया जोड़ें:" if is_adm else "📁 <b>My Study Vault</b>
अपना Subject चुनें (फाइलें 2 घंटे में ऑटो-डिलीट होंगी):"
        await query.edit_message_text(title, parse_mode="HTML", reply_markup=vault_subjects_kb(is_adm))
    elif parts[0] == "v_sub":
        sid, is_adm = parts[1], (user_states.get(user_id, {}).get("mode") == "ADMIN")
        sinfo = vault_data["subjects"].get(sid)
        if not sinfo: await query.edit_message_text("⚠️ Subject नहीं मिला।", reply_markup=vault_subjects_kb(is_adm)); return
        await query.edit_message_text(f"📘 <b>{html.escape(sinfo['name'])}</b> → चैप्टर्स चुनें:", parse_mode="HTML", reply_markup=vault_topics_kb(sid, is_adm))
    elif parts[0] == "v_top":
        sid, tid, is_adm = parts[1], parts[2], (user_states.get(user_id, {}).get("mode") == "ADMIN")
        tinfo = vault_data["subjects"].get(sid, {}).get("topics", {}).get(tid)
        if not tinfo: await query.edit_message_text("⚠️ Topic नहीं मिला।", reply_markup=vault_subjects_kb(is_adm)); return
        await query.edit_message_text(f"📂 <b>{html.escape(tinfo['name'])}</b> → टॉपिक्स चुनें:", parse_mode="HTML", reply_markup=vault_subtopics_kb(sid, tid, is_adm))
    elif parts[0] == "v_st":
        sid, tid, subid, is_adm = parts[1], parts[2], parts[3], (user_states.get(user_id, {}).get("mode") == "ADMIN")
        subinfo = vault_data["subjects"].get(sid, {}).get("topics", {}).get(tid, {}).get("subtopics", {}).get(subid)
        if not subinfo: await query.edit_message_text("⚠️ Sub-Topic नहीं मिला।", reply_markup=vault_subjects_kb(is_adm)); return
        await query.edit_message_text(f"📁 <b>{html.escape(subinfo['name'])}</b>
फाइल पर क्लिक करें (2 घंटे बाद ऑटो-डिलीट होगी):", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, is_adm))
    elif parts[0] == "v_get":
        sid, tid, subid, fid = parts[1], parts[2], parts[3], parts[4]
        subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
        fitem = next((f for f in subinfo["files"] if f["id"] == fid), None)
        if not fitem: await query.message.reply_text("⚠️ फाइल नहीं मिली।"); return
        sent = await context.bot.copy_message(chat_id=user_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=fitem["msg_id"], caption=f"📁 {subinfo['name']} • {fitem['name']}
⏳ 2 घंटे में ऑटो-डिलीट हो जाएगी।")
        schedule_custom_delete(context, user_id, [sent.message_id], VAULT_AUTO_DELETE_SECONDS)
    elif cb == "v_add_sub":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_ADD_SUB"}
        await query.edit_message_text("✍️ नए <b>Subject (विषय)</b> का नाम लिखकर भेजें:", parse_mode="HTML")
    elif parts[0] == "v_add_top":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_ADD_TOP", "sid": parts[1]}
        await query.edit_message_text("✍️ नए <b>Topic (चैप्टर)</b> का नाम लिखकर भेजें:", parse_mode="HTML")
    elif parts[0] == "v_add_st":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_ADD_ST", "sid": parts[1], "tid": parts[2]}
        await query.edit_message_text("✍️ नए <b>Sub-Topic (टॉपिक)</b> का नाम लिखकर भेजें:", parse_mode="HTML")
    elif parts[0] == "v_ren_sub":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_SUB", "sid": parts[1]}
        await query.edit_message_text("✏️ नया विषय नाम लिखकर भेजें:")
    elif parts[0] == "v_ren_top":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_TOP", "sid": parts[1], "tid": parts[2]}
        await query.edit_message_text("✏️ नया चैप्टर नाम लिखकर भेजें:")
    elif parts[0] == "v_ren_st":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_ST", "sid": parts[1], "tid": parts[2], "subid": parts[3]}
        await query.edit_message_text("✏️ नया टॉपिक नाम लिखकर भेजें:")
    elif parts[0] == "v_ren_f":
        user_states[user_id] = {"mode": "ADMIN", "step": "V_REN_F", "sid": parts[1], "tid": parts[2], "subid": parts[3], "fid": parts[4]}
        await query.edit_message_text("✏️ फाइल का नया नाम लिखकर भेजें:")
    elif parts[0] == "v_del_sub":
        vault_data["subjects"].pop(parts[1], None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ विषय डिलीट हो गया।", reply_markup=vault_subjects_kb(True))
    elif parts[0] == "v_del_top":
        sid, tid = parts[1], parts[2]
        vault_data["subjects"][sid]["topics"].pop(tid, None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ चैप्टर डिलीट हो गया।", reply_markup=vault_topics_kb(sid, True))
    elif parts[0] == "v_del_st":
        sid, tid, subid = parts[1], parts[2], parts[3]
        vault_data["subjects"][sid]["topics"][tid]["subtopics"].pop(subid, None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ टॉपिक डिलीट हो गया।", reply_markup=vault_subtopics_kb(sid, tid, True))
    elif parts[0] == "v_del_f":
        sid, tid, subid, fid = parts[1], parts[2], parts[3], parts[4]
        subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
        subinfo["files"] = [f for f in subinfo["files"] if f["id"] != fid]
        await save_and_pin_backup(context.bot)
        await query.edit_message_text(f"✅ <b>फाइल डिलीट कर दी गई है।</b>
📁 {subinfo['name']}", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, True))
    elif parts[0] == "v_up":
        sid, tid, subid = parts[1], parts[2], parts[3]
        user_states[user_id] = {"mode": "ADMIN", "step": "V_BATCH_UPLOAD", "sid": sid, "tid": tid, "subid": subid}
        await query.edit_message_text("📤 <b>फाइल भेजें</b>
अपनी PDF, Video, Photo या Audio फाइल नीचे चैट में भेजें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ फाइल जोड़ें (समाप्त)", callback_data=f"v_st:{sid}:{tid}:{subid}")]],
    ))
    elif cb == "s_doubt":
        user_states[user_id] = {"mode": "STUDY", "step": "DOUBT_WAIT_Q"}
        await query.edit_message_text("🧠 <b>Bihar Board AI Doubt Solver</b>

किसी भी प्रश्न की <b>फोटो भेजें</b> या <b>प्रश्न लिखकर भेजें</b>:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ स्टडी मेनू", callback_data="mode_study")]]))
    elif cb in ("d_hint", "d_sol"):
        st = user_states.get(user_id, {})
        pending = {"action": ("DOUBT_HINT" if cb == "d_hint" else "DOUBT_SOL"), "q_text": st.get("q_text", "इस प्रश्न को हल करें"), "q_img": st.get("q_img")}
        await prompt_for_duration(query.message, user_id, pending, edit=True)
    elif cb == "s_rev":
        user_states[user_id] = {"mode": "STUDY"}
        await query.edit_message_text("📚 <b>Bihar Board Revision Bank</b>
अपना विषय चुनें:", parse_mode="HTML", reply_markup=revision_subjects_kb())
    elif parts[0] == "r_subj":
        subj, sdata = parts[1], REVISION_SUBJECTS[parts[1]]
        kb = [[InlineKeyboardButton(f"{ch['name']} ({len(ch['formulas'])} सूत्र)", callback_data=f"r_ch_opt:{subj}:{i}")] for i, ch in enumerate(sdata["chapters"])]
        kb.append([InlineKeyboardButton("⬅️ विषय सूची", callback_data="s_rev")])
        await query.edit_message_text(f"<b>{html.escape(sdata['title'])}</b>
चैप्टर चुनें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    elif parts[0] == "r_ch_opt":
        subj, idx = parts[1], int(parts[2])
        ch_obj, fcnt = REVISION_SUBJECTS[subj]["chapters"][idx], len(REVISION_SUBJECTS[subj]["chapters"][idx]["formulas"])
        btns = [[InlineKeyboardButton(f"📝 All {fcnt} Formulas (Fast Text Sheet)", callback_data=f"r_sheet_ask:{subj}:{idx}"), InlineKeyboardButton(f"🎯 All {fcnt} Formulas Live MCQ Test", callback_data=f"r_mcq_ask:{subj}:{idx}")] ]
        if subj == "bio": btns.append([InlineKeyboardButton("🧬 Chapter Diagrams & Labeling", callback_data=f"r_diag_ask:{idx}")])
        btns.append([InlineKeyboardButton("⬅️ चैप्टर्स", callback_data=f"r_subj:{subj}")])
        await query.edit_message_text(f"📘 <b>{html.escape(ch_obj['name'])}</b>
विकल्प चुनें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
    elif parts[0] == "r_sheet_ask":
        await prompt_for_duration(query.message, user_id, {"action": "REV_FORMULA_SHEET", "subj": parts[1], "idx": int(parts[2])}, edit=True)
    elif parts[0] == "r_diag_ask":
        await prompt_for_duration(query.message, user_id, {"action": "REV_BIO_DIAGRAMS", "idx": int(parts[1])}, edit=True)
    elif parts[0] in ("r_mcq_ask", "r_mcq_direct"):
        await prompt_for_duration(query.message, user_id, {"action": "REV_OFFLINE_MCQ", "subj": parts[1], "idx": int(parts[2])}, edit=True)
    elif cb == "r_hin":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ गद्य खंड (13 पाठ)", callback_data="r_hin_sec:gadya"), InlineKeyboardButton("📜 काव्य खंड (13 पाठ)", callback_data="r_hin_sec:kavya")], [InlineKeyboardButton("⬅️ विषय सूची", callback_data="s_rev")]])
        await query.edit_message_text("📖 <b>Hindi (दिगंत भाग-2)</b>
खंड चुनें:", parse_mode="HTML", reply_markup=kb)
    elif parts[0] == "r_hin_sec":
        sec = parts[1]
        kb = [[InlineKeyboardButton(x["ch"], callback_data=f"r_lang_opt:hin:{sec}:{i}")] for i, x in enumerate(HINDI_DATA[sec])]
        kb.append([InlineKeyboardButton("⬅️ हिंदी खंड", callback_data="r_hin")])
        await query.edit_message_text("पाठ चुनें:", reply_markup=InlineKeyboardMarkup(kb))
    elif cb == "r_eng":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📘 Prose (11 Chapters)", callback_data="r_eng_sec:prose"), InlineKeyboardButton("🖋️ Poetry (10 Chapters)", callback_data="r_eng_sec:poetry")], [InlineKeyboardButton("⬅️ Subjects", callback_data="s_rev")]])
        await query.edit_message_text("🔤 <b>English (Rainbow Part-2)</b>
Select Section:", parse_mode="HTML", reply_markup=kb)
    elif parts[0] == "r_eng_sec":
        sec = parts[1]
        kb = [[InlineKeyboardButton(x["ch"], callback_data=f"r_lang_opt:eng:{sec}:{i}")] for i, x in enumerate(ENGLISH_DATA[sec])]
        kb.append([InlineKeyboardButton("⬅️ English Sections", callback_data="r_eng")])
        await query.edit_message_text("Select Chapter:", reply_markup=InlineKeyboardMarkup(kb))
    elif parts[0] == "r_lang_opt":
        lang, sec, idx = parts[1], parts[2], int(parts[3])
        item = HINDI_DATA[sec][idx] if lang == "hin" else ENGLISH_DATA[sec][idx]
        back_cb = f"r_hin_sec:{sec}" if lang == "hin" else f"r_eng_sec:{sec}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📝 लेखक / कवि परिचय (Notes)", callback_data=f"r_ln_ask:{lang}:{sec}:{idx}"), InlineKeyboardButton("🎯 सारे संभावित Live MCQ Test", callback_data=f"r_lm_ask:{lang}:{sec}:{idx}")], [InlineKeyboardButton("⬅️ Chapters", callback_data=back_cb)]])
        await query.edit_text(f"📘 <b>{html.escape(item['ch'])}</b>
विकल्प चुनें:", parse_mode="HTML", reply_markup=kb)
    elif parts[0] == "r_ln_ask":
        await prompt_for_duration(query.message, user_id, {"action": "REV_LANG_NOTES", "lang": parts[1], "sec": parts[2], "idx": int(parts[3])}, edit=True)
    elif parts[0] in ("r_lm_ask", "r_lang_mcq_direct"):
        await prompt_for_duration(query.message, user_id, {"action": "REV_LANG_MCQ", "lang": parts[1], "sec": parts[2], "idx": int(parts[3])}, edit=True)
    elif parts[0] == "qz_ans":
        chosen = int(parts[1])
        quiz = user_states.get(user_id, {}).get("quiz")
        if not quiz: return
        questions, q_idx = quiz["questions"], quiz["idx"]
        item, correct, labels = questions[q_idx], item["ans"], ["A", "B", "C", "D"]
        if chosen == correct:
            quiz["score"] = quiz.get("score", 0) + 1
            quiz.setdefault("correct_indices", []).append(q_idx + 1)
            verdict = f"✅ <b>सही जवाब! ({labels[correct]})</b>"
        else:
            quiz.setdefault("wrongs", []).append({"q_num": q_idx + 1, "q": item["q"], "chosen": chosen, "chosen_txt": item["options"][chosen], "correct": correct, "correct_txt": item["options"][correct]})
            verdict = f"❌ <b>गलत जवाब!</b> आपने <b>{labels[chosen]}</b> चुना。
✅ <b>सही उत्तर: {labels[correct]}) {html.escape(item['options'][correct])}</b>"
        exp_txt = f"
💡 <i>{html.escape(item['exp'])}</i>" if item.get("exp") else ""
        quiz["idx"] = q_idx + 1
        next_label = "➡️ अगला प्रश्न" if quiz["idx"] < len(questions) else "🏆 फाइनल स्कोर कार्ड देखें"
        res_text = f"<b>प्रश्न {q_idx + 1}/{len(questions)}:</b> {html.escape(item['q'])}

{verdict}{exp_txt}"
        await query.edit_message_text(res_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(next_label, callback_data="qz_next")]]))
    elif cb == "qz_next":
        await send_quiz_question(query.message, user_id, context, edit=True)
    elif cb == "qz_finish_now":
        st = user_states.get(user_id, {})
        if st.get("quiz"):
            st["quiz"]["idx"] = len(st["quiz"]["questions"])
            await send_quiz_question(query.message, user_id, context, edit=True)
    elif cb in ("s_mis", "adm_mis"):
        is_adm = (cb == "adm_mis")
        user_states[user_id] = {"mode": "ADMIN" if is_adm else "STUDY"}
        title = "🛠️ <b>Manage Mistake Bank</b>
विषय चुनें:" if is_adm else "⚠️ <b>Mistake Bank (मेरी गलतियाँ)</b>
किस विषय की गलतियाँ देखनी हैं?"
        await query.edit_message_text(title, parse_mode="HTML", reply_markup=mistake_subjects_kb(is_adm))
    elif parts[0] in ("ma_sub", "ms_sub"):
        is_adm, subj = (parts[0] == "ma_sub"), parts[1]
        stitle, _ = get_chapter_list_for_subject(subj)
        await query.edit_message_text(f"<b>{html.escape(stitle)}</b>
चैप्टर चुनें:", parse_mode="HTML", reply_markup=mistake_chapters_kb(subj, is_adm))
    elif parts[0] == "ma_ch":
        subj, idx = parts[1], int(parts[2])
        _, ch_list = get_chapter_list_for_subject(subj)
        await query.edit_message_text(f"🛠️ <b>{html.escape(ch_list[idx])}</b>
यहाँ अपनी भूली हुई चीज़ें/गलतियाँ Add, Rename या Delete करें:", parse_mode="HTML", reply_markup=admin_mistake_items_kb(subj, idx))
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
        if not mitem: return
        if mitem.get("type") == "photo" and mitem.get("msg_id"):
            await context.bot.copy_message(chat_id=user_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=mitem["msg_id"], caption=mitem["title"])
        else:
            await query.message.reply_text(f"<b>{html.escape(mitem['title'])}</b>

{html.escape(mitem.get('text', ''))}", parse_mode="HTML")
    elif cb in ("s_html_test", "adm_html_test"):
        is_adm = (cb == "adm_html_test")
        user_states[user_id] = {"mode": "ADMIN" if is_adm else "STUDY"}
        t_count = len(vault_data.get("tests", {}))
        title = f"🛠️ <b>Manage HTML Tests ({t_count})</b>
अपलोड किए गए टेस्ट या नया <code>.html</code> टेस्ट अपलोड करें:" if is_adm else f"📝 <b>HTML Live MCQ Tests ({t_count})</b>
लाइव टेस्ट शुरू करने के लिए किसी भी टेस्ट पर क्लिक करें:"
        await query.edit_message_text(title, parse_mode="HTML", reply_markup=html_tests_kb(is_adm))
    elif cb == "ht_upload":
        user_states[user_id] = {"mode": "ADMIN", "step": "HT_WAIT_FILE"}
        await query.edit_message_text("📤 अपनी <b>HTML टेस्ट फाइल (.html)</b> यहाँ भेजें—बॉट उसमें से सारे MCQs निकालकर लाइव टेस्ट तैयार कर देगा!", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ वापस", callback_data="adm_html_test")]]))
    elif parts[0] == "ht_ren":
        user_states[user_id] = {"mode": "ADMIN", "step": "HT_REN", "tid": parts[1]}
        await query.edit_message_text("✏️ इस टेस्ट का नया नाम लिखकर भेजें:")
    elif parts[0] == "ht_del":
        vault_data["tests"].pop(parts[1], None)
        await save_and_pin_backup(context.bot)
        await query.edit_message_text("🗑️ HTML टेस्ट डिलीट हो गया।", reply_markup=html_tests_kb(True))
    elif parts[0] == "ht_run":
        await prompt_for_duration(query.message, user_id, {"action": "RUN_HTML_TEST", "tid": parts[1]}, edit=True)

async def handle_text(update, context):
    global login_expiry_ts
    user_id = update.effective_user.id
    if user_id != OWNER_ID: return
    text = update.message.text.strip()
    st = user_states.get(user_id, {})
    step = st.get("step")
    if not is_authorized(user_id):
        if step == "AUTH_WAIT_PASS" and text == BOT_PASSWORD:
            login_expiry_ts = time.time() + LOGIN_VALIDITY_SECONDS
            user_states.pop(user_id, None)
            try: await update.message.delete()
            except Exception: pass
            await update.message.reply_text("✅ <b>Sanskari Study में लॉगिन सफल!</b>
🔓 यह सेशन अगले <b>24 घंटे</b> तक सक्रिय रहेगा। अपना मोड चुनें:", parse_mode="HTML", reply_markup=root_mode_kb())
        return
    if step == "WAIT_DURATION":
        pending = st.get("pending")
        dur_sec, dur_label = parse_duration_seconds(text)
        user_states.pop(user_id, None)
        await execute_timed_action(update.message, user_id, pending, dur_sec, dur_label, context, from_button=False)
        return
    if step == "DOUBT_WAIT_Q":
        user_states[user_id] = {"mode": "STUDY", "q_text": text, "q_img": None}
        await update.message.reply_text("✅ प्रश्न मिल गया! अब चुनें:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💡 सटीक Hint", callback_data="d_hint")], [InlineKeyboardButton("📝 Step-by-Step Solution", callback_data="d_sol")], [InlineKeyboardButton("⬅️ स्टडी मेनू", callback_data="mode_study")]]))
        return
    if step == "V_ADD_SUB":
        sid = next_id("s")
        vault_data["subjects"][sid] = {"name": text, "topics": {}}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text(f"✅ विषय <b>{html.escape(text)}</b> बन गया!", parse_mode="HTML", reply_markup=vault_topics_kb(sid, True))
        return
    if step == "V_ADD_TOP":
        sid, tid = st["sid"], next_id("t")
        vault_data["subjects"][sid]["topics"][tid] = {"name": text, "subtopics": {}}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text(f"✅ चैप्टर <b>{html.escape(text)}</b> बन गया!", parse_mode="HTML", reply_markup=vault_subtopics_kb(sid, tid, True))
        return
    if step == "V_ADD_ST":
        sid, tid, subid = st["sid"], st["tid"], next_id("st")
        vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid] = {"name": text, "files": []}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text(f"✅ टॉपिक <b>{html.escape(text)}</b> बन गया!", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, True))
        return
    if step == "V_REN_SUB":
        sid = st["sid"]
        vault_data["subjects"][sid]["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ विषय का नाम अपडेट हो गया।", reply_markup=vault_topics_kb(sid, True))
        return
    if step == "V_REN_TOP":
        sid, tid = st["sid"], st["tid"]
        vault_data["subjects"][sid]["topics"][tid]["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await update.message.reply_text("✏️ चैप्टर का नाम अपडेट हो गया।", reply_markup=vault_subtopics_kb(sid, tid, True))
        return
    if step == "V_REN_ST":
        sid, tid, subid = st["sid"], st["tid"], st["subid"]
        vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
        await update.message.reply_text(f"✏️ <b>टॉपिक का नाम अपडेट हो गया।</b>
📁 {subinfo['name']}", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, True))
        return
    if step == "V_REN_F":
        sid, tid, subid, fid = st["sid"], st["tid"], st["subid"], st["fid"]
        for fitem in vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]["files"]:
            if fitem["id"] == fid: fitem["name"] = text
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
        await update.message.reply_text(f"✏️ <b>फाइल का नाम अपडेट हो गया।</b>
📁 {subinfo['name']}", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, True))
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
                if mitem.get("type") == "text": mitem["text"] = text
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

async def handle_media(update, context):
    user_id = update.effective_user.id
    if not is_authorized(user_id): return
    msg = update.message
    st = user_states.get(user_id, {})
    step = st.get("step")
    if step == "HT_WAIT_FILE" and msg.document:
        fname = msg.document.file_name or "Live_Test.html"
        if not fname.lower().endswith((".html", ".htm")):
            await msg.reply_text("⚠️ कृपया केवल <code>.html</code> या <code>.htm</code> टेस्ट फाइल भेजें।", parse_mode="HTML"); return
        doc_file = await msg.document.get_file()
        bio = io.BytesIO()
        await doc_file.download_to_memory(bio)
        parsed_qs = parse_html_mcq_file(bio.getvalue())
        if not parsed_qs:
            await msg.reply_text("⚠️ इस HTML फाइल से प्रश्न नहीं निकल पाए। सुनिश्चित करें कि फाइल में प्रश्न, 4 विकल्प (A, B, C, D) और उत्तर मौजूद हैं।", reply_markup=html_tests_kb(True)); return
        tid = next_id("ht")
        clean_name = re.sub(r"\.(html|htm)$", "", fname, flags=re.I)
        vault_data.setdefault("tests", {})[tid] = {"name": clean_name, "questions": parsed_qs}
        user_states[user_id] = {"mode": "ADMIN"}
        await save_and_pin_backup(context.bot)
        await msg.reply_text(f"✅ <b>HTML लाइव टेस्ट तैयार हो गया!</b>
📘 <b>नाम:</b> {html.escape(clean_name)}
🎯 <b>कुल प्रश्न:</b> {len(parsed_qs)} MCQs

अब आप <b>Study / Viewer Mode → HTML Live MCQ Test</b> में जाकर लाइव टेस्ट दे सकते हैं!", parse_mode="HTML", reply_markup=html_tests_kb(True))
        return
    if step == "DOUBT_WAIT_Q" and msg.photo:
        pfile = await msg.photo[-1].get_file()
        bio = io.BytesIO()
        await pfile.download_to_memory(bio)
        user_states[user_id] = {"mode": "STUDY", "q_text": msg.caption or "इस फोटो में दिए गए प्रश्न को हल करें।", "q_img": bio.getvalue()}
        await msg.reply_text("✅ प्रश्न की फोटो मिल गई! अब चुनें:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💡 सटीक Hint", callback_data="d_hint")], [InlineKeyboardButton("📝 Step-by-Step Solution", callback_data="d_sol")], [InlineKeyboardButton("⬅️ स्टडी मेनू", callback_data="mode_study")]]))
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
        if msg.document: icon, default_name = "📄", msg.document.file_name or "Document.pdf"
        elif msg.video: icon, default_name = "🎥", msg.video.file_name or (msg.caption[:40] if msg.caption else "Video Lecture")
        elif msg.photo: icon, default_name = "🖼️", msg.caption[:40] if msg.caption else "Handwritten Note"
        elif msg.audio or msg.voice: icon, default_name = "🎧", msg.caption[:40] if msg.caption else "Audio Note"
        else: return
        copied = await context.bot.copy_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=user_id, message_id=msg.message_id)
        fid = next_id("f")
        subinfo = vault_data["subjects"][sid]["topics"][tid]["subtopics"][subid]
        subinfo["files"].append({"id": fid, "name": default_name, "msg_id": copied.message_id, "icon": icon})
        await save_and_pin_backup(context.bot)
        await msg.reply_text(f"✅ <b>फाइल ऐड हो गई है</b>
📁 {subinfo['name']} • {html.escape(default_name)}", parse_mode="HTML", reply_markup=vault_files_kb(sid, tid, subid, True))
        return
    await msg.reply_text("ℹ️ फाइल अपलोड करने के लिए <b>Admin Mode</b> में जाकर संबंधित फोल्डर या <b>Manage HTML Tests</b> खोलें।", parse_mode="HTML")

class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Sanskari Study Bot Active")
        def log_message(self, format, *args): pass
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
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO | filters.AUDIO | filters.VOICE, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Sanskari Study Bot (Stylish + 2x2 Grid + Fully Fixed AI Solver) running...")
    app.run_polling()

if __name__ == "__main__":
    main()
