import os
import time
import sqlite3
import threading
from datetime import datetime
import pytz
import random
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# 📚 दूसरी फाइल से प्रश्न इम्पोर्ट करें
from questions import QUIZ_LIST

# .env से सभी क्रेडेंशियल्स लोड करें
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SUPPORT_GROUP_ID = os.getenv("SUPPORT_GROUP_ID")  # 👈 [UPDATED] .env से ग्रुप आईडी लोड करने के लिए

if not API_TOKEN:
    raise ValueError("Error: BOT_TOKEN एनवायरनमेंट वेरिएबल्स में नहीं मिला!")

bot = telebot.TeleBot(API_TOKEN)
DB_FILE = "bot_data.db"

# 🚀 परफ़ॉर्मेंस बूस्ट: ग्लोबल बॉट यूज़रनेम वेरिएबल
BOT_USERNAME = "Bot"
try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    pass

if OWNER_ID:
    try:
        OWNER_ID = int(OWNER_ID)
    except ValueError:
        OWNER_ID = None

# 📌 [UPDATED] ग्रुप आईडी को टेक्स्ट से पूर्णांक (Integer) संख्या में बदलें
if SUPPORT_GROUP_ID:
    try:
        SUPPORT_GROUP_ID = int(SUPPORT_GROUP_ID)
    except ValueError:
        SUPPORT_GROUP_ID = None

# 💾 परमानेंट डेटाबेस आर्किटेक्चर (रीस्टार्ट प्रूफ)
def init_db():
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                current_index INTEGER DEFAULT 0,
                last_poll_id INTEGER DEFAULT NULL,
                last_sent_time REAL DEFAULT 0,
                language TEXT DEFAULT 'hindi',
                interval INTEGER DEFAULT 1800,
                auto_delete INTEGER DEFAULT 1
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                join_time REAL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS poll_mapping (
                poll_id TEXT PRIMARY KEY,
                chat_id INTEGER,
                correct_id INTEGER,
                creation_time REAL DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_scores (
                chat_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('leaderboard_time', '22:00')")
        
        # 🔍 [ANTI-SPAM SETTINGS DB] पुराना सेटिंग्स कॉलम लॉजिक
        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN settings_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 

        # 🔍 [ANTI-SPAM START DB] /start मैसेज आईडी सेव करने के लिए नया कॉलम जोड़ा गया
        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN start_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 

        # 🔍 [ANTI-SPAM HELP DB] /help मैसेज आईडी सेव करने के लिए नया कॉलम जोड़ा गया
        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN help_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
            
        # 🔍 [ANTI-SPAM MYSCORE DB] यूज़र का पिछला स्कोर कार्ड मैसेज आईडी सेव करने के लिए कॉलम
        try:
            cursor.execute("ALTER TABLE daily_scores ADD COLUMN last_score_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
            
        try:
            cursor.execute("ALTER TABLE poll_mapping ADD COLUMN creation_time REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # 🔍 [WARNING TRACKER DB] बॉट एडमिन न होने पर वार्निंग टाइम याद रखने के लिए नया कॉलम
        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN last_warning_time REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
            
            
        conn.commit()

init_db()

def is_user_admin(chat_id, user_id):
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False

# 🚨 [NEW GLOBAL DICTIONARY] हर ग्रुप के लिए वार्निंग टाइमस्टैम्प याद रखने के लिए
# 🔄 हर ग्रुप के लिए कस्टमाइज्ड पोल शेड्यूलर लूप
def global_poll_manager():
    while True:
        try:
            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                # 🔍 SELECT क्वेरी में 'last_warning_time' कॉलम को भी जोड़ दिया है
                cursor.execute("SELECT chat_id, current_index, last_poll_id, last_sent_time, language, interval, auto_delete, last_warning_time FROM groups")
                all_groups = cursor.fetchall()
                current_now = time.time()

                # 💡 लूप के वेरिएबल्स में 'last_warning_time' को भी पास किया है
                for chat_id, current_index, last_poll_id, last_sent_time, language, interval, auto_delete, last_warning_time in all_groups:
                    if current_now - last_sent_time >= interval:
                        
                        # चेक करें कि क्या बॉट अभी भी ग्रुप में एडमिन है?
                        is_bot_admin = False
                        try:
                            bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
                            if bot_member.status in ['administrator', 'creator']:
                                is_bot_admin = True
                        except Exception:
                            is_bot_admin = False

                        # ⚠️ अगर बॉट एडमिन नहीं है
                        if not is_bot_admin:
                            # ⏱️ 12 घंटे = 43200 सेकंड्स (फिक्स टाइमर)
                            warning_interval = 43200 
                            
                            # 🎯 अब मेमोरी से नहीं, सीधे डेटाबेस के रिकॉर्ड (last_warning_time) से चेक होगा
                            if last_warning_time is None or current_now - last_warning_time >= warning_interval:
                                try:
                                    bot.send_message(
                                        chat_id=chat_id, 
                                        text="⚠️ **alert!**\n\nTo send polls in this group, you must re-promote the bot to Admin **(Administrator)** and grant permissions।",
                                        parse_mode="Markdown"
                                    )
                                    # 💾 डेटाबेस में वार्निंग भेजने का टाइम तुरंत सेव करें (रीस्टार्ट प्रूफ)
                                    cursor.execute("UPDATE groups SET last_warning_time = ? WHERE chat_id = ?", (current_now, chat_id))
                                except Exception:
                                    pass
                            
                            # बार-बार डेटाबेस लूप को एक्टिवेट न करने के लिए last_sent_time को नॉर्मल इंटरवल तक बढ़ाएं
                            cursor.execute("UPDATE groups SET last_sent_time = ? WHERE chat_id = ?", (current_now, chat_id))
                            conn.commit()
                            continue  # इस ग्रुप को स्किप करें

                        # --- पुराना पोल डिलीट करने का लॉजिक (एडमिन होने पर ही चलेगा) ---
                        if last_poll_id is not None and auto_delete == 1:
                            try:
                                bot.delete_message(chat_id=chat_id, message_id=last_poll_id)
                            except Exception:
                                pass

                        filtered_quiz = [q for q in QUIZ_LIST if q.get("lang", "hindi") == language]
                        if not filtered_quiz:
                            filtered_quiz = QUIZ_LIST

                        if current_index >= len(filtered_quiz):
                            current_index = 0

                        quiz = filtered_quiz[current_index]
                        explanation_text = quiz.get("explanation", None)
                        
                        try:
                            sent_message = bot.send_poll(
                                chat_id=chat_id,
                                question=quiz["question"],
                                options=quiz["options"],
                                type="quiz",
                                correct_option_id=quiz["correct_id"],
                                is_anonymous=False,  
                                explanation=explanation_text
                            )
                            new_poll_id = sent_message.message_id
                            poll_api_id = sent_message.poll.id
                            
                            cursor.execute("INSERT INTO poll_mapping (poll_id, chat_id, correct_id, creation_time) VALUES (?, ?, ?, ?)", 
                                           (poll_api_id, chat_id, quiz["correct_id"], time.time()))

                            new_index = (current_index + 1) % len(filtered_quiz)
                            cursor.execute('''
                                UPDATE groups 
                                SET current_index = ?, last_poll_id = ?, last_sent_time = ? 
                                WHERE chat_id = ?
                            ''', (new_index, new_poll_id, current_now, chat_id))
                            conn.commit()

                        except Exception as e:
                            if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                                cursor.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
                                conn.commit()
        except Exception as db_err:
            print(f"डेटाबेस लूप एरर: {db_err}")
        time.sleep(5)
        

# ⚙️ मुख्य सेटिंग्स मेनू यूआई जेनरेटर
def get_settings_markup(chat_id):
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT language, interval, auto_delete FROM groups WHERE chat_id = ?", (chat_id,))
        res = cursor.fetchone()
    if not res: return None, None
    lang, interval, auto_delete = res[0], res[1], res[2]
    interval_mins = interval // 60
    del_status = "ON ✅" if auto_delete == 1 else "OFF 📴"
    
    text = (
        "⚙️ **Settings Panel (Quiz Settings)**\n\n"
        f"🌐 **Current Language:** {lang.upper()}\n"
        f"⏱️ **Quiz Interval:** {interval_mins} min\n"
        f"🗑️ **Auto Delete Poll:** {del_status}\n\n"
        "Click on the buttons below to change configurations:"
    )
    markup = InlineKeyboardMarkup()
    lang_text = "🌐 भाषा: HINDI 🇮🇳" if lang == 'hindi' else "🌐 Lang: ENGLISH 🇬🇧"
    
    btn_lang = InlineKeyboardButton(text=lang_text, callback_data=f"set_lang_{chat_id}", style="primary")
    btn_autodel = InlineKeyboardButton(text="🗑️ Auto-Delete Settings", callback_data=f"menu_autodel_{chat_id}", style="primary")
    
    btn_15m = InlineKeyboardButton(text="⏱️ 15 Min", callback_data=f"set_time_900_{chat_id}", style="success")
    btn_30m = InlineKeyboardButton(text="⏱️ 30 Min", callback_data=f"set_time_1800_{chat_id}", style="success")
    btn_45m = InlineKeyboardButton(text="⏱️ 45 Min", callback_data=f"set_time_2700_{chat_id}", style="success")
    btn_60m = InlineKeyboardButton(text="⏱️ 60 Min", callback_data=f"set_time_3600_{chat_id}", style="success")
    
    btn_close = InlineKeyboardButton(text="Close ❌", callback_data=f"panel_close_{chat_id}", style="danger")
    
    markup.row(btn_lang)
    markup.row(btn_autodel)
    markup.row(btn_15m, btn_30m)
    markup.row(btn_45m, btn_60m)
    markup.row(btn_close)
    return text, markup

def get_autodelete_markup(chat_id):
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_delete FROM groups WHERE chat_id = ?", (chat_id,))
        res = cursor.fetchone()
    auto_delete = res[0] if res else 1
    status_text = "ON" if auto_delete == 1 else "OFF"
    text = (
        "🗑️ **Auto-Delete Settings**\n\n"
        "⚠️ **Click on the control buttons**\n\n"
        f"📊 **Status:** \" {status_text} \"\n\n"
        "ℹ️ **What does this do?**\n"
        "• When ON: Previous quiz poll will be deleted automatically.\n"
        "• When OFF: Old quizzes will stay in chat history.\n\n"
        "👇 Toggle auto-delete setting:"
    )
    markup = InlineKeyboardMarkup()
    
    btn_on = InlineKeyboardButton(text="Turn On ✅", callback_data=f"autodel_on_{chat_id}", style="success")
    btn_off = InlineKeyboardButton(text="Turn Off 📴", callback_data=f"autodel_off_{chat_id}", style="danger")
    btn_back = InlineKeyboardButton(text="Back 🔙", callback_data=f"autodel_back_{chat_id}", style="danger")
    
    markup.row(btn_on, btn_off)
    markup.row(btn_back)
    return text, markup

@bot.message_handler(commands=['settings'])
def group_settings(message):
    chat_type = message.chat.type

    if chat_type == 'private':
        try: bot.reply_to(message, "❌ This command can only be used in groups.")
        except Exception: pass
        return  

    if not is_user_admin(message.chat.id, message.from_user.id):
        try: bot.reply_to(message, "❌ Only group admin's can change the settings.")
        except Exception: pass
        return
        
    # 🔍 [ANTI-SPAM LOGIC] डेटाबेस से पुराना सेटिंग्स मैसेज आईडी ढूँढना और उसे डिलीट करना
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT settings_msg_id FROM groups WHERE chat_id = ?", (message.chat.id,))
        row = cursor.fetchone()
        old_msg_id = row[0] if row and row[0] else 0

    if old_msg_id > 0:
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except Exception:
            pass  # अगर पुराना मैसेज पहले ही कोई डिलीट कर चुका है तो एरर स्किप करें

    text, markup = get_settings_markup(message.chat.id)
    if text: 
        try: 
            # नया सेटिंग्स मैसेज भेजना
            new_msg = bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
            
            # 📌 [SAVE NEW ID] नए मैसेज की आईडी को डेटाबेस में सेव करना ताकि अगली बार इसे डिलीट किया जा सके
            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE groups SET settings_msg_id = ? WHERE chat_id = ?", (new_msg.message_id, message.chat.id))
                conn.commit()
        except Exception: 
            pass
  
# 🔄 सेटिंग्स बटन प्रोसेसर (मल्टी-इंडेक्स आर्किटेक्चर फिक्स्ड)
@bot.callback_query_handler(func=lambda call: call.data.startswith(('set_lang_', 'set_time_', 'menu_autodel_', 'autodel_', 'panel_close_')))
def handle_settings_callbacks(call):
    user_id = call.from_user.id
    data_parts = call.data.split('_')
    
    action = data_parts[0]       
    sub_action = data_parts[1]   
    chat_id = int(data_parts[-1]) 
    
    if not is_user_admin(chat_id, user_id):
        bot.answer_callback_query(call.id, "❌ You do not have admin permissions!", show_alert=True)
        return

    # 🛑 [UPDATED] क्लोज बटन दबाने पर डेटाबेस से आईडी साफ़ करना और मैसेज डिलीट करना
    if action == "panel" and sub_action == "close":
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE groups SET settings_msg_id = 0 WHERE chat_id = ?", (chat_id,))
            conn.commit()
        try: 
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception: 
            pass
        return

    show_main_menu = True
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        
        if action == "set" and sub_action == "lang":
            cursor.execute("SELECT language FROM groups WHERE chat_id = ?", (chat_id,))
            res = cursor.fetchone()
            current_lang = res[0] if res else 'hindi'
            new_lang = 'english' if current_lang == 'hindi' else 'hindi'
            cursor.execute("UPDATE groups SET language = ? WHERE chat_id = ?", (new_lang, chat_id))
            bot.answer_callback_query(call.id, f"भाषा बदलकर {new_lang.upper()} कर दी गई है।")
            
        elif action == "set" and sub_action == "time":
            new_interval = int(data_parts[2]) 
            cursor.execute("UPDATE groups SET interval = ? WHERE chat_id = ?", (new_interval, chat_id))
            bot.answer_callback_query(call.id, f"समय अंतराल बदलकर {new_interval // 60} मिनट कर दिया गया है।")
            
        elif action == "menu" and sub_action == "autodel":
            show_main_menu = False
            bot.answer_callback_query(call.id) 
            
        elif action == "autodel":
            if sub_action == "on":
                cursor.execute("UPDATE groups SET auto_delete = 1 WHERE chat_id = ?", (chat_id,))
                bot.answer_callback_query(call.id, "Auto-Delete चालू (ON) कर दिया गया है।")
                show_main_menu = False
            elif sub_action == "off":
                cursor.execute("UPDATE groups SET auto_delete = 0 WHERE chat_id = ?", (chat_id,))
                bot.answer_callback_query(call.id, "Auto-Delete बंद (OFF) कर दिया गया है।")
                show_main_menu = False
            elif sub_action == "back":
                bot.answer_callback_query(call.id, "मुख्य मेनू पर वापस जा रहे हैं...")
                show_main_menu = True
                
        conn.commit()
        
    if show_main_menu: 
        text, markup = get_settings_markup(chat_id)
    else: 
        text, markup = get_autodelete_markup(chat_id)
        
    try: 
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except Exception: 
        pass
            

# 👑 ओनर कमांड - टाइम सेट करना (Strict Group & Owner Security Added)
@bot.message_handler(commands=['settime'])
def set_global_leaderboard_time(message):
    is_owner = (OWNER_ID and message.from_user.id == OWNER_ID)
    is_valid_chat = (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))

    if not (is_owner and is_valid_chat):
        try: bot.send_message(message.chat.id, "❌ This command is only valid for the bot owner and in authorized chats.")
        except Exception: pass
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "⚠️ **गलत फॉर्मेट!**\nकृपया इस तरह लिखें: `/settime HH:MM` \nउदाहरण: `/settime 22:00`", parse_mode="Markdown")
        return
        
    time_str = args[1].strip()
    try:
        datetime.strptime(time_str, "%H:%M")
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE bot_settings SET value = ? WHERE key = 'leaderboard_time'", (time_str,))
            conn.commit()
        bot.send_message(message.chat.id, f"✅ **Chief, the time has been updated!**\nFrom now on, daily results will be auto-sent at exactly **{time_str}**", parse_mode="Markdown")
    except ValueError:
        bot.send_message(message.chat.id, "❌ **Invalid time format!**\nPlease use the 24-hour format.(ex: 13:00, 22:30)।")

# 👑 📢 ओनर कमांड - अपडेटेड ब्रॉडकास्ट फ़ीचर (Strict Group & Owner Security Added)
# STEP 1: Command Handler - Jo message par reply karne par Yes/No buttons poochega
@bot.message_handler(commands=['broadcast'])
def handle_owner_broadcast(message):
    is_owner = (OWNER_ID and message.from_user.id == OWNER_ID)
    is_valid_chat = (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))

    if not (is_owner and is_valid_chat):
        try: bot.send_message(message.chat.id, "❌ This command is only valid for the bot owner and in authorized chats.")
        except Exception: pass
        return

    if not message.reply_to_message:
        bot.send_message(
            message.chat.id, 
            "⚠️ **उपयोग कैसे करें?**\n"
            "1. वह टेक्स्ट, फोटो, वीडियो या स्टिकर भेजें जिसे ब्रॉडकास्ट करना है।\n"
            "2. उस मैसेज पर **Reply** करके लिखें: `/broadcast`", 
            parse_mode="Markdown"
        )
        return

    # 🎨 Asli Green (Success) aur Red (Danger) Button Styles Ke Sath
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            text=" YES (Pin Karein)", 
            callback_data=f"bcast_yes_{message.reply_to_message.message_id}",
            style="success"  # 👈 Isse button poora GREEN rang ka dikhega
        ),
        InlineKeyboardButton(
            text="NO (Pin Nahi Karein)", 
            callback_data=f"bcast_no_{message.reply_to_message.message_id}",
            style="danger"   # 👈 Isse button poora RED rang ka dikhega
        )
    )

    bot.send_message(
        chat_id=message.chat.id,
        text="🏵️ **क्या आप इस ब्रॉडकास्ट मैसेज को सभी ग्रुप्स में PIN करना चाहते हैं?**",
        reply_markup=markup,
        parse_mode="Markdown"
    )


# STEP 2: Callback Query Handler - Jo button dabaane par actual broadcast shuru karega
@bot.callback_query_handler(func=lambda call: call.data.startswith(('bcast_yes_', 'bcast_no_')))
def execute_broadcast_callback(call):
    # Security: Sirf Bot Owner hi button par click kar sakta hai
    if OWNER_ID and call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ You are not authorized to control this broadcast!", show_alert=True)
        return

    # Data split karke decision nikalna
    data_parts = call.data.split('_')
    should_pin = (data_parts[1] == 'yes')
    target_msg_id = int(data_parts[2])

    # Status message me badle aur buttons ko screen se hataye
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="📢 **Initializing broadcast process, please wait....**",
        parse_mode="Markdown"
    )

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM groups")
        all_chats = cursor.fetchall()
        cursor.execute("SELECT user_id FROM users")
        all_users = cursor.fetchall()

    g_success, g_fail = 0, 0
    u_success, u_fail = 0, 0

    # 👥 1. ग्रुप्स में ब्रॉडकास्ट (Yes hone par pin hoga)
    for (chat_id,) in all_chats:
        try:
            # Message copy karein aur return object nikalen
            sent_msg = bot.copy_message(
                chat_id=chat_id, 
                from_chat_id=call.message.chat.id, 
                message_id=target_msg_id
                # Note: Aapka original reply_markup target_msg_id se automatic chala jata hai
            )
            
            # 🔔 Agar user ne YES dabaaya tha, toh message ko group me pin karein
            if should_pin and sent_msg and hasattr(sent_msg, 'message_id'):
                try:
                    bot.pin_chat_message(
                        chat_id=chat_id, 
                        message_id=sent_msg.message_id, 
                        disable_notification=False
                    )
                except Exception:
                    pass  # Agar kisi group me Admin permission na ho, toh crash na ho

            g_success += 1
            time.sleep(0.15)  
        except Exception: 
            g_fail += 1

    # 👤 2. प्राइवेट यूज़र्स में ब्रॉडकास्ट (Isme pin ka koi roll nahi hota)
    for (user_id,) in all_users:
        try:
            bot.copy_message(
                chat_id=user_id, 
                from_chat_id=call.message.chat.id, 
                message_id=target_msg_id
            )
            u_success += 1
            time.sleep(0.15)  
        except Exception: u_fail += 1

    # 📊 Final Report screen par dikhaye
    bot.edit_message_text(
        chat_id=call.message.chat.id, 
        message_id=call.message.message_id, 
        text=f"📊 **Global Broadcast Report:**\n\n"
             f"📌 **Group Pin Status:** {'✅ Pinned' if should_pin else '❌ Not Pinned'}\n\n"
             f"👥 **group's:**\n"
             f"✅ **done: {g_success}** | ❌ **Undone: {g_fail}**\n\n"
             f"👤 **Private User's:**\n"
             f"✅ **done: {u_success}** | ❌ **Undone: {u_fail}**\n\n"
             f"🎯 **Broadcast completed successfully!**", 
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['sendresult'])
def manual_leaderboard_sender(message):
    is_owner = (OWNER_ID and message.from_user.id == OWNER_ID)
    is_valid_chat = (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))

    if not (is_owner and is_valid_chat):
        try: bot.send_message(message.chat.id, "❌ This command is only valid for the bot owner and in authorized chats.")
        except Exception: pass
        return
        
    status_msg = bot.send_message(message.chat.id, "⏳ **Sending new result to all groups immediately...**")
    IST = pytz.timezone('Asia/Kolkata')
    now = datetime.now(IST)
    
    markup = InlineKeyboardMarkup()
    add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    
    # [UPDATED] बटन में style="success" जोड़ दिया है, जिससे यह हरे रंग (Green) का दिखेगा
    markup.add(InlineKeyboardButton(
        text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", 
        url=add_to_group_url,
        style="success"
    ))

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM groups")
        all_chats = cursor.fetchall()
        success_count = 0
        
        for (chat_id,) in all_chats:
            cursor.execute("SELECT user_name, correct_count, wrong_count FROM daily_scores WHERE chat_id = ?", (chat_id,))
            all_users = cursor.fetchall()
            
            calculated_leaderboard = []
            for name, correct, wrong in all_users:
                final_score = (correct * 2) - (wrong * 0.5)
                if (correct + wrong) > 0:
                    calculated_leaderboard.append((final_score, name, correct, wrong))
            
            # [FIXED] x[0] की जगह केवल x किया ताकि स्कोर बराबर होने पर नाम मैचिंग से एरर न आए
            calculated_leaderboard.sort(key=lambda x: x, reverse=True)
            top_20 = calculated_leaderboard[:20]
            
            lb_text = "🏆 **Result [Top 20 user's Leaderboard]**\n"
            lb_text += f"---------------------------------------\n" 
            lb_text += f"📅 Date: {now.strftime('%d-%m-%Y')} | ⏰ Time: {now.strftime('%H:%M')} (Manual)\n"
            lb_text += "📊 Marking: Right (+2) | Wrong (-0.5)\n"
            lb_text += f"---------------------------------------\n\n" 
            
            if top_20:
                medals = {1: "🥇", 2: "🥈", 3: "🥉"}
                for idx, (final_score, name, correct, wrong) in enumerate(top_20, 1):
                    medal = medals.get(idx, f"{idx}.")
                    display_score = f"{final_score:.1f}" if final_score % 0.5 != 0 else f"{int(final_score)}"
                    
                    lb_text += f"{medal} **{name}**\n"
                    lb_text += f"🔥 Score: **{display_score}** pts | ✅ {correct} | ❌ {wrong}\n"
                    lb_text += f"---------------------------------------\n" 
            else:
                lb_text += "⚠️ No users participated in the quiz today.\n"
                lb_text += f"---------------------------------------\n"
                
            lb_text += "\n🎯 Amazing effort! Get ready for a new quiz tomorrow! 🚀\n"
            lb_text += "\n⭐ If you don't want to wait for the results, you can\n"
            lb_text += "\nuse the ☞ `/myscore` command at any time."
            try: 
                bot.send_message(chat_id=chat_id, text=lb_text, reply_markup=markup, parse_mode="Markdown")
                success_count += 1
                time.sleep(0.15)
            except Exception: pass
            
        cursor.execute("DELETE FROM daily_scores")
        cursor.execute("DELETE FROM poll_mapping")
        conn.commit()
        
    try:
        bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=f"✅ **Chief, the manual result has been successfully sent!**\n📊 Total **{success_count}** Leaderboards sent to active groups and scores have been reset!", parse_mode="Markdown")
    except Exception: pass

def daily_leaderboard_scheduler():
    has_sent_today = False
    last_checked_date = ""
    
    markup = InlineKeyboardMarkup()
    add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    
    # [UPDATED] बटन को आकर्षक हरे रंग (Green) का बनाने के लिए style="success" जोड़ा
    markup.add(InlineKeyboardButton(
        text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", 
        url=add_to_group_url,
        style="success"
    ))
    
    while True:
        try:
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime.now(IST)
            current_date_str = now.strftime("%Y-%m-%d")
            
            if current_date_str != last_checked_date:
                has_sent_today = False
                last_checked_date = current_date_str

            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM bot_settings WHERE key = 'leaderboard_time'")
                res = cursor.fetchone()
                db_time = res[0] if res else "22:00"
            
            try: 
                target_hour, target_minute = map(int, db_time.split(':'))
            except Exception: 
                target_hour, target_minute = 22, 0
            
            if now.hour == target_hour and now.minute == target_minute and not has_sent_today:
                with sqlite3.connect(DB_FILE, timeout=20) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT chat_id FROM groups")
                    all_chats = cursor.fetchall()
                    
                    for (chat_id,) in all_chats:
                        cursor.execute("SELECT user_name, correct_count, wrong_count FROM daily_scores WHERE chat_id = ?", (chat_id,))
                        all_users = cursor.fetchall()
                        
                        calculated_leaderboard = []
                        for name, correct, wrong in all_users:
                            final_score = (correct * 2) - (wrong * 0.5)
                            if (correct + wrong) > 0:
                                calculated_leaderboard.append((final_score, name, correct, wrong))
                                
                        # 🎯 [FIXED - CRASH PROOF] केवल स्कोर कंपेयर होगा, नाम में इमोजी होने पर भी कभी क्रैश नहीं होगा
                        calculated_leaderboard.sort(key=lambda x: x, reverse=True)
                        top_20 = calculated_leaderboard[:20]
                        
                        lb_text = "🏆 **Result [Top 20 user's Leaderboard]**\n"
                        lb_text += f"---------------------------------------\n" 
                        lb_text += f"📅 Date: {now.strftime('%d-%m-%Y')} | ⏰ Time: {db_time}\n"
                        lb_text += "🎓 Performance of the Last 24 Hours:\n"
                        lb_text += "📊 Marking: Right (+2) | Wrong (-0.5)\n"
                        lb_text += f"---------------------------------------\n\n" 
                        
                        if top_20:
                            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
                            for idx, (final_score, name, correct, wrong) in enumerate(top_20, 1):
                                medal = medals.get(idx, f"{idx}.")
                                display_score = f"{final_score:.1f}" if final_score % 0.5 != 0 else f"{int(final_score)}"
                                
                                lb_text += f"{medal} **{name}**\n"
                                lb_text += f"🔥 Score: **{display_score}** point | ✅ {correct} | ❌ {wrong}\n"
                                lb_text += f"---------------------------------------\n" 
                        else:
                            lb_text += "⚠️ No users participated in the quiz today.\n"
                            lb_text += f"---------------------------------------\n"
                            
                        lb_text += "\n🎯 Amazing effort! Get ready for a new quiz tomorrow! 🚀\n"
                        lb_text += "\n⭐ If you don't want to wait for the results, you can\n" 
                        lb_text += "\nuse the ☞ `/myscore` command at any time."
                        try: 
                            bot.send_message(chat_id=chat_id, text=lb_text, reply_markup=markup, parse_mode="Markdown")
                            time.sleep(0.15)
                        except Exception: 
                            pass
                            
                    # [FIXED - LOGIC] सभी ग्रुप्स को मैसेज भेजने के बाद ही डेटाबेस साफ़ होगा
                    cursor.execute("DELETE FROM daily_scores")
                    cursor.execute("DELETE FROM poll_mapping")
                    conn.commit()
                    
                has_sent_today = True
                time.sleep(60) 
                
        except Exception as sched_err:
            print(f"शेड्यूलर एरर: {sched_err}")
        time.sleep(20)
        
# 🎯 LIVE पोल उत्तर ट्रैकर (OLD POLL STOPPER FEATURE LOADED ✅)
@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    # [FIXED] poll_id को हमेशा साफ़ स्ट्रिंग में बदलें ताकि डेटाबेस से मैच हो सके
    poll_id = str(poll_answer.poll_id)
    user_id = poll_answer.user.id
    
    first_name = poll_answer.user.first_name if poll_answer.user.first_name else ""
    last_name = poll_answer.user.last_name if poll_answer.user.last_name else ""
    user_name = f"{first_name} {last_name}".strip()
    if not user_name: 
        user_name = f"User_{user_id}"

    # अगर यूज़र ने अपना वोट वापस ले लिया (Retract Vote) तो स्कोर चेंज नहीं होगा
    if not poll_answer.option_ids:
        return

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        
        # [SAFE CHECK] पोल आईडी को स्ट्रिंग बनाकर ही सर्च करें
        cursor.execute("SELECT chat_id, correct_id, creation_time FROM poll_mapping WHERE poll_id = ?", (poll_id,))
        mapping = cursor.fetchone()
        
        if not mapping:
            print(f"⚠️ चेतावनी: Poll ID {poll_id} डेटाबेस मैपिंग में नहीं मिली!")
            return  

        chat_id = mapping[0]
        correct_id = mapping[1]
        creation_time = mapping[2] if mapping[2] is not None else time.time()
        chosen_option = poll_answer.option_ids[0]
        
        # 24 घंटे का एंटी-चीट फ़िल्टर
        if time.time() - creation_time > 86400:
            return  

        # स्कोर अपडेट लॉजिक
        if chosen_option == correct_id:
            cursor.execute('''
                INSERT INTO daily_scores (chat_id, user_id, user_name, correct_count, wrong_count)
                VALUES (?, ?, ?, 1, 0)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name = excluded.user_name,
                correct_count = daily_scores.correct_count + 1
            ''', (chat_id, user_id, user_name))
        else:
            cursor.execute('''
                INSERT INTO daily_scores (chat_id, user_id, user_name, correct_count, wrong_count)
                VALUES (?, ?, ?, 0, 1)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name = excluded.user_name,
                wrong_count = daily_scores.wrong_count + 1
            ''', (chat_id, user_id, user_name))
            
        conn.commit()

# 📊 यूजर लाइव स्कोर ट्रैकर कस्टमाइज्ड कमांड (प्राइवेट चैट ब्लॉक के साथ)
@bot.message_handler(commands=['myscore'])
def check_user_score(message):
    chat_type = message.chat.type

    # 🚨 अगर यूजर प्राइवेट चैट (DM) में कमांड डालता है
    if chat_type == 'private':
        try: bot.reply_to(message, "❌ This command can only be used in groups.")
        except Exception: pass
        return  

    user_id = message.from_user.id
    chat_id = message.chat.id

    # 🗑️ [ANTI-SPAM 1] यूज़र द्वारा भेजे गए कमांड टेक्स्ट (/myscore) को तुरंत डिलीट करें
    try: bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception: pass

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        # स्कोर के साथ-साथ यूज़र के पिछले स्कोर मैसेज की आईडी (last_score_msg_id) भी फ़ेच करें
        cursor.execute("SELECT correct_count, wrong_count, last_score_msg_id FROM daily_scores WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        res = cursor.fetchone()
    
    if res:
        correct = res[0]
        wrong = res[1]
        old_score_msg_id = res[2] if res[2] else 0
        final_score = (correct * 2) - (wrong * 0.5)
    else:
        correct, wrong, old_score_msg_id, final_score = 0, 0, 0, 0.0

    # 🗑️ [ANTI-SPAM 2] अगर इस यूज़र का कोई पुराना स्कोर कार्ड ग्रुप में खुला है, तो उसे डिलीट करें
    if old_score_msg_id > 0:
        try: bot.delete_message(chat_id=chat_id, message_id=old_score_msg_id)
        except Exception: pass

    # स्कोर फ़ॉर्मेट (.0 हटाने के लिए)
    display_score = f"{final_score:.1f}" if final_score % 0.5 != 0 else f"{int(final_score)}"

    score_text = (
        f"🎉 **Congratulations {message.from_user.first_name}**, your today's quiz score!\n\n"
        f"✅ Correct Ans: **{correct}** (+{correct * 2} point)\n"
        f"❌ Wrong Ans: **{wrong}** (-{wrong * 0.5} point)\n"
        f"🔥 **Final Score: {display_score} point**\n\n"
        f"ℹ️ Note: This score will be reset after the leaderboard is published.\n"
        f"⭐ If you don't want to wait for the results, you can\n"
        f"use the ☞ `/myscore` command at any time."
    )

    try: 
        # नया स्कोर कार्ड भेजें (चूँकि पुराना डिलीट हो चुका है, इसलिए reply_to के बजाय सीधे send_message करेंगे)
        new_score_msg = bot.send_message(chat_id=chat_id, text=score_text, parse_mode="Markdown")
        
        # 📌 [SAVE NEW ID] नए स्कोर कार्ड की आईडी को डेटाबेस में इस यूज़र के डेटा के साथ अपडेट करें
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            # सुनिश्चित करें कि यूज़र की एंट्री डेटाबेस में मौजूद हो, फिर अपडेट करें
            cursor.execute("""
                INSERT INTO daily_scores (chat_id, user_id, user_name, correct_count, wrong_count, last_score_msg_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET last_score_msg_id = excluded.last_score_msg_id
            """, (chat_id, user_id, message.from_user.first_name, correct, wrong, new_score_msg.message_id))
            conn.commit()
    except Exception: 
        pass

# 💬 /start कमांड (Strict Group Validation के साथ 100% FIXED)
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_type = message.chat.type
    message_text = message.text.strip() if message.text else ""
    
    # 🚨 Check if the command is for this bot specifically in groups
    if chat_type in ['group', 'supergroup']:
        expected_full_command = f"/start@{BOT_USERNAME}"
        if "@" in message_text and not message_text.startswith(expected_full_command):
            return  

    first_name = message.from_user.first_name if message.from_user.first_name else ""
    last_name = message.from_user.last_name if message.from_user.last_name else ""
    full_name = f"{first_name} {last_name}".strip()
    if not full_name: full_name = f"User_{user_id}"

    # 🖼️ [DYNAMIC LOGIC] 'images' फोल्डर से रैंडम फोटो चुनना
    image_folder = "images"  # आपके फोल्डर का नाम
    selected_image_path = None

    try:
        # चेक करें कि फोल्डर मौजूद है या नहीं और उसमें फाइल्स हैं या नहीं
        if os.path.exists(image_folder) and os.path.isdir(image_folder):
            # फोल्डर के अंदर की सभी फाइल्स की लिस्ट (केवल png, jpg, jpeg)
            all_images = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            if all_images:
                # फोल्डर के रास्ते के साथ रैंडम इमेज का पाथ जोड़ें (जैसे: images/photo1.png)
                selected_image_path = os.path.join(image_folder, random.choice(all_images))
    except Exception as e:
        print(f"इमेज फोल्डर रीड करने में एरर: {e}")

    # 📌 Group Chat Logic
    if chat_type in ['group', 'supergroup']:
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT start_msg_id FROM groups WHERE chat_id = ?", (message.chat.id,))
            row = cursor.fetchone()
            # [FIXED - CRASH PROOF] अगर रो मौजूद है (not None) तो ही इंडेक्स 0 निकालेगा, वरना 0 असाइन करेगा
            old_start_id = row[0] if row is not None else 0

        if old_start_id > 0:
            try: bot.delete_message(chat_id=message.chat.id, message_id=old_start_id)
            except Exception: pass

        group_text = (
            f"🎉 **Bot activated successfully!**\n"
            f"📢 Automated quizzes have been activated for this group.\n\n"
            f"🇮🇳 **Group Name:** [{message.chat.title}]\n"
            f"This bot is the easiest way to keep your groups active and engaged.\n\n"
            f"📌 **My Features:**\n"
            f"📊 **Daily Auto Poll:** Automatically sends a new poll every day at your set time interval.\n"
            f"🏆 **Auto Result:** Generates results daily at 10 PM showing the Top 20 users' scores with negative marking.\n\n"
            f"🚀 **How to Get Started:**\n"
            f"1. Make me a **Group Admin** (so I have permission to send polls).\n"
            f"2. Use the `/settings` command inside your group to configure everything.\n\n"
            f"For any help, simply type `/help`."
        )
        group_markup = InlineKeyboardMarkup()
        add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        group_markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url, style="success"))
        
        new_msg = None
        try: 
            # अगर इमेज पाथ मिल गया है तो फोटो भेजें
            if selected_image_path:
                with open(selected_image_path, "rb") as photo_file:
                    new_msg = bot.send_photo(
                        chat_id=message.chat.id, 
                        photo=photo_file, 
                        caption=group_text, 
                        reply_markup=group_markup, 
                        parse_mode="Markdown"
                    )
            else:
                # अगर फोल्डर खाली है या नहीं मिला तो सिर्फ टेक्स्ट भेजें
                raise ValueError("No image found")
        except Exception: 
            try:
                new_msg = bot.send_message(chat_id=message.chat.id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
            except Exception: pass

        # [FIXED - DATABASE SAVE] नया मैसेज भेजने के बाद उसकी ID को सुरक्षित रूप से स्टोर करना
        if new_msg:
            try:
                with sqlite3.connect(DB_FILE, timeout=20) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO groups (chat_id, start_msg_id) VALUES (?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET start_msg_id = excluded.start_msg_id
                    ''', (message.chat.id, new_msg.message_id))
                    conn.commit()
            except Exception: pass
        return  

    # Private Chat Logic
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, user_name, join_time) VALUES (?, ?, ?)", (user_id, full_name, time.time()))
        conn.commit()

    if OWNER_ID and user_id == OWNER_ID:
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'leaderboard_time'")
            res = cursor.fetchone()
            # [FIXED - TUPLE ERROR] डेटाबेस से साफ़ स्ट्रिंग निकालने के लिए res[0] किया
            db_time = res[0] if res is not None else "22:00"
            
        welcome_text = (
            f"👑 **प्रणाम मालिक ({message.from_user.first_name})!**\n\n"
            f"📊 वर्तमान लीडरबोर्ड टाइम: **{db_time}**\n"
            "⚙️ आप सीधे यहीं पर `/settime HH:MM` लिखकर टाइम बदल सकते हैं।\n"
            "🏆 तुरंत रिज़ल्ट भेजने and स्कोर रीसेट करने के लिए `/sendresult` लिखें।\n"
            "📢 किसी भी मैसेज पर रिप्लाई करके `/broadcast` लिखने से वह सभी ग्रुप्स और यूज़र्स के पर्सनल इनबॉक्स में चला जाएगा।\n"
            "📊 बॉट का लाइव स्टैट्स देखने के लिए `/status` का उपयोग करें।\n\n"
            "बॉट को ग्रुप में जोड़ने के लिए नीचे दिए बटन का उपयोग करें।"
        )
    else:
        welcome_text = (
            f"👋 **Hello** {message.from_user.first_name}!\n"
            f"**Welcome!** This bot is the easiest way to keep your groups active and engaged.\n\n"
            f"**📌 My Features:**\n\n"
            f"📊 **Daily Auto Poll:**\n"
            "Automatically sends a new poll every day at your set time interval.\n\n"
            "🏆 **Auto Result:**\n"
            "Generates results daily at 10 PM showing the Top 20 users' scores with negative marking.\n\n"
            "🚀 **How to Get Started:**\n\n"
            "**1. Add me** to your Telegram group.\n"
            "**2. Make me a **Group Admin** (so I have permission to send polls).\n"
            "**3. Use the `/settings` command inside your group to configure everything.**\n\n"
            "For any help, simply type `/help` ."
        )
        
    markup = InlineKeyboardMarkup()
    add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url, style="success"))
    
    try: 
        if selected_image_path:
            with open(selected_image_path, "rb") as photo_file:
                bot.send_photo(
                    chat_id=message.chat.id, 
                    photo=photo_file, 
                    caption=welcome_text, 
                    reply_markup=markup, 
                    parse_mode="Markdown"
                )
        else:
            bot.send_message(chat_id=message.chat.id, text=welcome_text, reply_markup=markup, parse_mode="Markdown")
    except Exception: 
        try: bot.send_message(chat_id=message.chat.id, text=welcome_text, reply_markup=markup, parse_mode="Markdown")
        except Exception: pass
                
        
# ℹ️ हेल्प कमांड (Strict Username Validation के साथ FIXED)
@bot.message_handler(commands=['help'])
def send_help(message):
    chat_type = message.chat.type
    message_text = message.text.strip() if message.text else ""
    
    # 🚨 चेक करें कि क्या कमांड सिर्फ इसी बॉट के लिए है?
    if chat_type in ['group', 'supergroup']:
        expected_full_command = f"/help@{BOT_USERNAME}"
        if "@" in message_text and not message_text.startswith(expected_full_command):
            return  # ❌ दूसरे बॉट की कमांड है, मेरा बॉट शांत रहेगा

    # 📌 Group Chat Logic (With Anti-Spam Auto-Delete)
    if chat_type in ['group', 'supergroup']:
        # 🔍 डेटाबेस से पुराने /help मैसेज की आईडी निकालना
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT help_msg_id FROM groups WHERE chat_id = ?", (message.chat.id,))
            row = cursor.fetchone()
            old_help_id = row[0] if row and row[0] else 0

        # अगर पुराना मैसेज मौजूद है, तो उसे चैट से साफ़ (Delete) करें
        if old_help_id > 0:
            try: 
                bot.delete_message(chat_id=message.chat.id, message_id=old_help_id)
            except Exception: 
                pass

    help_text = (
        "⚡ **Help & Guide - Daily Poll Bot:**\n\n"
        "Here is a quick guide on how to configure and use the bot in your group:\n\n"
        "🛠 **Setup Instructions:**\n\n"
        "**Step 1:** Add this bot to your group.\n"
        "**Step 2:** Grant the bot Admin Permissions.\n"
        "**Step 3:** Type `/settings` inside the group to set up your poll timing and quiz language.\n\n"
        "🕒 **How the System Works:**\n\n"
        "**Polls:** Sent automatically during your configured daytime intervals.\n"
        "**Leaderboard:** Published automatically every single night at **10:00 PM.**\n"
        "Scoring: Accuracy matters! The leaderboard calculates the Top 20 users with a **negative marking system** applied for wrong answers.\n\n"
        "🔐 `/settings` - Open the configuration panel (Group Admins only)."
    )
    markup = InlineKeyboardMarkup()
    
    # 👑 .env से लोडेड OWNER_ID का उपयोग करके ऑटोमैटिक परमानेंट लिंक बनाया
    owner_url = f"tg://user?id={int(OWNER_ID)}"
    markup.add(InlineKeyboardButton(text="💬 Contact Support", url=owner_url))
    
    try: 
        # 1. सबसे पहले नया हेल्प मैसेज (Response) भेजें
        new_help_msg = bot.send_message(chat_id=message.chat.id, text=help_text, reply_markup=markup, parse_mode="Markdown")
        
        # 2. नए हेल्प मैसेज की आईडी को डेटाबेस में अपडेट करें (सिर्फ ग्रुप्स के लिए)
        if chat_type in ['group', 'supergroup']:
            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE groups SET help_msg_id = ? WHERE chat_id = ?", (new_help_msg.message_id, message.chat.id))
                conn.commit()
                
            # 🗑️ [NEW LOGIC] नया रिस्पॉन्स सुरक्षित भेजने के बाद, यूजर की भेजी हुई '/help' कमांड को डिलीट करें
            try:
                bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
            except Exception:
                pass  # अगर बॉट के पास मैसेज डिलीट करने की परमिशन नहीं होगी, तो भी बॉट क्रैश नहीं होगा
                
    except Exception: 
        pass
        

# 📊 लाइव स्टेटस कमांड (Strict Group & Owner Security Added)
GROUPS_PER_PAGE = 10

@bot.message_handler(commands=['status'])
def send_stats(message):
    is_owner = (OWNER_ID and message.from_user.id == OWNER_ID)
    is_valid_chat = (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))

    if not (is_owner and is_valid_chat):
        try: bot.send_message(message.chat.id, "❌ This command is only valid for the bot owner and in authorized chats.")
        except Exception: pass
        return

    # [UPDATED] शुरुआत में ही एक लोडिंग मैसेज भेजेंगे ताकि यूज़र को लगे कि बॉट एक्टिव है
    status_msg = bot.send_message(message.chat.id, "⏳ **Fetching statistics and group data... Please wait...**", parse_mode="Markdown")
    
    # डेटा लोड करके पुराने मैसेज को एडिट कर देंगे
    text, markup = generate_status_page(page=0)
    try:
        bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        try: bot.send_message(message.chat.id, text=text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception: pass

def generate_status_page(page=0):
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM groups")
        all_chats = cursor.fetchall()
        
        cursor.execute("SELECT COUNT(*) FROM users")
        res_u = cursor.fetchone()
        u_count = res_u[0] if res_u else 0

    g_count = len(all_chats)
    start_idx = page * GROUPS_PER_PAGE
    end_idx = start_idx + GROUPS_PER_PAGE
    current_page_groups = all_chats[start_idx:end_idx]
    
    total_pages = (g_count + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE
    if total_pages == 0: total_pages = 1

    stats_text = (
        f"📊 **Bot Live Status & Statistics**\n"
        f"---------------------------------------\n"
        f"🎯 Total Active Groups: **{g_count}**\n"
        f"👤 Total Active Users: **{u_count}**\n"
        f"📖 Page: **{page + 1} / {total_pages}**\n"
        f"---------------------------------------\n\n"
        f"⚡ **Active Groups List:**\n\n"
    )

    if current_page_groups:
        for idx, (chat_id,) in enumerate(current_page_groups, start_idx + 1):
            try:
                chat_info = bot.get_chat(chat_id)
                group_name = chat_info.title
                
                try:
                    invite_link = bot.export_chat_invite_link(chat_id)
                    link_text = f"[Click to Join]({invite_link})"
                except Exception:
                    if chat_info.username:
                        link_text = f"[Click to Join](https://t.me{chat_info.username})"
                    else:
                        link_text = "⚠️ No Admin (No Link)"
                
                stats_text += f"{idx}. **{group_name}**\n🆔 ` {chat_id} `\n🔗 {link_text}\n"
                stats_text += f"---------------------------------------\n"
            except Exception:
                stats_text += f"{idx}. 🛑 **Unknown/Left Group**\n🆔 ` {chat_id} `\n---------------------------------------\n"
    else:
        stats_text += "⚠️ No groups found on this page.\n"

    markup = InlineKeyboardMarkup()
    buttons_row = []

    if page > 0:
        buttons_row.append(InlineKeyboardButton(text="⏮️ Previous", callback_data=f"statpage_{page-1}", style="primary"))
    if end_idx < g_count:
        buttons_row.append(InlineKeyboardButton(text="Next Page 🔀", callback_data=f"statpage_{page+1}", style="primary"))

    if buttons_row:
        markup.row(*buttons_row)
        
    markup.row(InlineKeyboardButton(text="Close ❌", callback_data="status_close", style="danger"))
    return stats_text, markup

# 🔄 पेज बदलने और क्लोज करने का बटन हैंडलर
@bot.callback_query_handler(func=lambda call: call.data.startswith("statpage_") or call.data == "status_close")
def handle_status_pagination(call):
    # सिर्फ बॉट ओनर ही बटन दबा सकता है
    if not (OWNER_ID and call.from_user.id == OWNER_ID):
        bot.answer_callback_query(call.id, text="❌ This menu is only for the bot owner.", show_alert=True)
        return

    if call.data == "status_close":
        try:
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            pass
        return

    # 'statpage_1' में से पेज नंबर (1) अलग निकालना
    try:
        target_page = int(call.data.split("_")[1])
        
        # टेलीग्राम को बताएं कि लोडिंग हो रही है
        bot.answer_callback_query(call.id, text=f"Loading Page {target_page + 1}...")
        
        # नए पेज का डेटा जेनरेट करें
        text, markup = generate_status_page(page=target_page)
        
        # पुराने मैसेज को नए पेज के डेटा से एडिट करें
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        print(f"पेज बदलने में एरर: {e}")
            

# 🤖 ग्रुप जॉइन/लीव ट्रैकर (सेम वेलकम मैसेज आर्किटेक्चर)
@bot.my_chat_member_handler()
def handle_left_or_joined(my_chat_member):
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status  # 🔍 पुराना स्टेटस ट्रैक करें
    chat_id = my_chat_member.chat.id
    chat_title = my_chat_member.chat.title
    
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        
        if new_status in ["administrator", "member"]:
            # Check karein ki kya group pehle se database mein maujood hai?
            cursor.execute("SELECT chat_id FROM groups WHERE chat_id = ?", (chat_id,))
            group_exists = cursor.fetchone()
            
            # 🎯 अगर ग्रुप पहले से मौजूद नहीं है (यानी बॉट सच में नया जॉइन हुआ है)
            # या फिर बॉट पहले ग्रुप से पूरी तरह लेफ्ट/किक हो चुका था, सिर्फ तभी वेलकम मैसेज भेजेगा।
            if not group_exists or old_status in ["left", "kicked"]:
                if not group_exists:
                    cursor.execute("INSERT OR IGNORE INTO groups (chat_id, interval, last_sent_time) VALUES (?, 1800, 0)", (chat_id,))
                    conn.commit()
                
                # 🖼️ 'images' फोल्डर से रैंडम फोटो चुनना
                image_folder = "images"
                selected_image_path = None
                try:
                    if os.path.exists(image_folder) and os.path.isdir(image_folder):
                        all_images = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
                        if all_images:
                            selected_image_path = os.path.join(image_folder, random.choice(all_images))
                except Exception as e:
                    print(f"इमेज फोल्डर रीड करने में एरर: {e}")
                
                group_text = (
                    f"🎉 **Join Group Successfully!**\n"
                    f"📢 Automated quizzes have been activated for this group.\n\n"
                    f"🇮🇳 **Group Name:** [{chat_title}]\n"
                    f"This bot is the easiest way to keep your groups active and engaged.\n\n"
                    f"📌 **My Features:**\n"
                    f"📊 **Daily Auto Poll:** Automatically sends a new poll every day at your set time interval.\n"
                    f"🏆 **Auto Result:** Generates results daily at 10 PM showing the Top 20 users' scores with negative marking.\n"
                    f"💡 **Results** ka wait nahi karna chahte to `/myscore` command send kare!\n\n"
                    f"🚀 **How to Get Started:**\n"
                    f"1. Make me a **Group Admin** (so I have permission to send polls).\n"
                    f"2. Use the `/settings` command inside your group to configure everything.\n\n"
                    f"For any help, simply type `/help`."
                )
                
                group_markup = InlineKeyboardMarkup()
                # 🛠️ [FIXED] t.me के बाद forward slash ( / ) जोड़ दिया गया है
                add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
                group_markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url, style="primary"))
                
                try:
                    if selected_image_path:
                        with open(selected_image_path, "rb") as photo_file:
                            bot.send_photo(chat_id=chat_id, photo=photo_file, caption=group_text, reply_markup=group_markup, parse_mode="Markdown")
                    else:
                        bot.send_message(chat_id=chat_id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
                except Exception:
                    try:
                        bot.send_message(chat_id=chat_id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
                    except Exception: pass
                
        elif new_status in ["left", "kicked"]:
            # Bot ko group se nikalne par data automatically clean ho jayega
            cursor.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
            conn.commit()
                
# ❤️‍🩹 थ्रेड्स स्टार्ट करें
threading.Thread(target=global_poll_manager, daemon=True).start()
threading.Thread(target=daily_leaderboard_scheduler, daemon=True).start()

print("Successfully 🇮🇳 deployed...🚀")

bot.infinity_polling(timeout=60, long_polling_timeout=60)
                                         
