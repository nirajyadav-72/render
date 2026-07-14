import os
import time
import threading
import requests
from datetime import datetime
import pytz
import random
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# 📚 दूसरी फाइल से प्रश्न इम्पोर्ट करें
from questions import QUIZ_LIST

# .env से सभी क्रेडेंशियल्स लोड करें
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SUPPORT_GROUP_ID = os.getenv("SUPPORT_GROUP_ID")
MONGODB_URI = os.getenv("MONGODB_URI")  # 👈 MongoDB URI .env से लोड करें

if not API_TOKEN:
    raise ValueError("Error: BOT_TOKEN एनवायरनमेंट वेरिएबल्स में नहीं मिला!")

if not MONGODB_URI:
    raise ValueError("Error: MONGODB_URI एनवायरनमेंट वेरिएबल्स में नहीं मिला!")

bot = telebot.TeleBot(API_TOKEN)

# 🚀 MongoDB Connection Setup
try:
    # 🛠️ यहाँ बदलाव किया गया है: SSL/TLS हैंडशेक एरर को ठीक करने के लिए पैरामीटर्स जोड़े गए हैं
    mongo_client = MongoClient(
        MONGODB_URI, 
        serverSelectionTimeoutMS=5000,
        tls=True,
        tlsAllowInvalidCertificates=True
    )
    # Connection को टेस्ट करें
    mongo_client.admin.command('ping')
    print("✅ MongoDB Connection Successful!")
    
    db = mongo_client['quiz_bot_db']  # डेटाबेस का नाम
    
    # Collections को define करें
    groups_collection = db['groups']
    users_collection = db['users']
    poll_mapping_collection = db['poll_mapping']
    daily_scores_collection = db['daily_scores']
    bot_settings_collection = db['bot_settings']
    
    # Indexes बनाएं (performance के लिए)
    groups_collection.create_index("chat_id", unique=True)
    users_collection.create_index("user_id", unique=True)
    poll_mapping_collection.create_index("poll_id", unique=True)
    daily_scores_collection.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
    
except PyMongoError as e:
    print(f"❌ MongoDB Connection Error: {e}")
    raise ValueError(f"MongoDB से कनेक्ट नहीं हो सके: {e}")

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

# 📌 ग्रुप आईडी को टेक्स्ट से पूर्णांक (Integer) संख्या में बदलें
if SUPPORT_GROUP_ID:
    try:
        SUPPORT_GROUP_ID = int(SUPPORT_GROUP_ID)
    except ValueError:
        SUPPORT_GROUP_ID = None
    

# 💾 MongoDB के साथ Database Initialization
def init_db():
    try:
        # Default settings को MongoDB में डालें (अगर मौजूद नहीं है)
        bot_settings_collection.update_one(
            {"key": "leaderboard_time"},
            {"$set": {"value": "22:00"}},
            upsert=True
        )
        print("✅ MongoDB Database Initialized!")
    except PyMongoError as e:
        print(f"❌ Database Initialization Error: {e}")

init_db()

def is_user_admin(chat_id, user_id):
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False

# 🔄 हर ग्रुप के लिए कस्टमाइज्ड पोल शेड्यूलर लूप (MongoDB के साथ)
def global_poll_manager():
    while True:
        try:
            # MongoDB से सभी ग्रुप्स लोड करें
            all_groups = list(groups_collection.find({}))
            current_now = time.time()

            for group_data in all_groups:
                chat_id = group_data.get('chat_id')
                current_index = group_data.get('current_index', 0)
                last_poll_id = group_data.get('last_poll_id')
                last_sent_time = group_data.get('last_sent_time', 0)
                language = group_data.get('language', 'hindi')
                interval = group_data.get('interval', 1800)
                auto_delete = group_data.get('auto_delete', 1)
                last_warning_time = group_data.get('last_warning_time', 0)

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
                        warning_interval = 43200  # 12 घंटे
                        
                        if not last_warning_time or current_now - last_warning_time >= warning_interval:
                            try:
                                bot.send_message(
                                    chat_id=chat_id, 
                                    text="⚠️ **alert!**\n\nTo send polls in this group, you must re-promote the bot to Admin **(Administrator)** and grant permissions।",
                                    parse_mode="Markdown"
                                )
                                # MongoDB में वार्निंग टाइम अपडेट करें
                                groups_collection.update_one(
                                    {"chat_id": chat_id},
                                    {"$set": {"last_warning_time": current_now}}
                                )
                            except Exception:
                                pass
                        
                        groups_collection.update_one(
                            {"chat_id": chat_id},
                            {"$set": {"last_sent_time": current_now}}
                        )
                        continue

                    # --- पुराना पोल डिलीट करने का लॉजिक ---
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
                        
                        # MongoDB में poll mapping स्टोर करें
                        poll_mapping_collection.insert_one({
                            "poll_id": str(poll_api_id),
                            "chat_id": chat_id,
                            "correct_id": quiz["correct_id"],
                            "creation_time": time.time()
                        })

                        new_index = (current_index + 1) % len(filtered_quiz)
                        groups_collection.update_one(
                            {"chat_id": chat_id},
                            {"$set": {
                                "current_index": new_index,
                                "last_poll_id": new_poll_id,
                                "last_sent_time": current_now
                            }}
                        )

                    except Exception as e:
                        if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                            groups_collection.delete_one({"chat_id": chat_id})
        except Exception as db_err:
            print(f"डेटाबेस लूप एरर: {db_err}")
        time.sleep(5)

# ⚙️ मुख्य सेटिंग्स मेनू यूआई जेनरेटर (MongoDB के साथ)
def get_settings_markup(chat_id):
    group_data = groups_collection.find_one({"chat_id": chat_id})
    if not group_data:
        return None, None
    
    lang = group_data.get('language', 'hindi')
    interval = group_data.get('interval', 1800)
    auto_delete = group_data.get('auto_delete', 1)
    
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
    group_data = groups_collection.find_one({"chat_id": chat_id})
    auto_delete = group_data.get('auto_delete', 1) if group_data else 1
    
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
        
    # MongoDB से पुराना सेटिंग्स मैसेज आईडी ढूँढना
    group_data = groups_collection.find_one({"chat_id": message.chat.id})
    old_msg_id = group_data.get('settings_msg_id', 0) if group_data else 0

    if old_msg_id > 0:
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except Exception:
            pass

    text, markup = get_settings_markup(message.chat.id)
    if text: 
        try: 
            new_msg = bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
            
            # नए मैसेज की आईडी को MongoDB में सेव करें
            groups_collection.update_one(
                {"chat_id": message.chat.id},
                {"$set": {"settings_msg_id": new_msg.message_id}},
                upsert=True
            )
        except Exception: 
            pass

# 🔄 सेटिंग्स बटन प्रोसेसर (MongoDB के साथ)
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

    if action == "panel" and sub_action == "close":
        groups_collection.update_one(
            {"chat_id": chat_id},
            {"$set": {"settings_msg_id": 0}}
        )
        try: 
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception: 
            pass
        return

    show_main_menu = True
    
    if action == "set" and sub_action == "lang":
        group_data = groups_collection.find_one({"chat_id": chat_id})
        current_lang = group_data.get('language', 'hindi') if group_data else 'hindi'
        new_lang = 'english' if current_lang == 'hindi' else 'hindi'
        groups_collection.update_one(
            {"chat_id": chat_id},
            {"$set": {"language": new_lang}},
            upsert=True
        )
        bot.answer_callback_query(call.id, f"भाषा बदलकर {new_lang.upper()} कर दी गई है।")
        
    elif action == "set" and sub_action == "time":
        new_interval = int(data_parts[2]) 
        groups_collection.update_one(
            {"chat_id": chat_id},
            {"$set": {"interval": new_interval}},
            upsert=True
        )
        bot.answer_callback_query(call.id, f"समय अंतराल बदलकर {new_interval // 60} मिनट कर दिया गया है।")
        
    elif action == "menu" and sub_action == "autodel":
        show_main_menu = False
        bot.answer_callback_query(call.id) 
        
    elif action == "autodel":
        if sub_action == "on":
            groups_collection.update_one(
                {"chat_id": chat_id},
                {"$set": {"auto_delete": 1}},
                upsert=True
            )
            bot.answer_callback_query(call.id, "Auto-Delete चालू (ON) कर दिया गया है।")
            show_main_menu = False
        elif sub_action == "off":
            groups_collection.update_one(
                {"chat_id": chat_id},
                {"$set": {"auto_delete": 0}},
                upsert=True
            )
            bot.answer_callback_query(call.id, "Auto-Delete बंद (OFF) कर दिया गया है।")
            show_main_menu = False
        elif sub_action == "back":
            bot.answer_callback_query(call.id, "मुख्य मेनू पर वापस जा रहे हैं...")
            show_main_menu = True
        
    if show_main_menu: 
        text, markup = get_settings_markup(chat_id)
    else: 
        text, markup = get_autodelete_markup(chat_id)
        
    try: 
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except Exception: 
        pass

# 👑 ओनर कमांड - टाइम सेट करना
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
        bot_settings_collection.update_one(
            {"key": "leaderboard_time"},
            {"$set": {"value": time_str}},
            upsert=True
        )
        bot.send_message(message.chat.id, f"✅ **Chief, the time has been updated!**\nFrom now on, daily results will be auto-sent at exactly **{time_str}**", parse_mode="Markdown")
    except ValueError:
        bot.send_message(message.chat.id, "❌ **Invalid time format!**\nPlease use the 24-hour format.(ex: 13:00, 22:30)।")

# 👑 📢 ओनर कमांड - ब्रॉडकास्ट फ़ीचर
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

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            text=" YES (Pin Karein)", 
            callback_data=f"bcast_yes_{message.reply_to_message.message_id}",
            style="success"
        ),
        InlineKeyboardButton(
            text="NO (Pin Nahi Karein)", 
            callback_data=f"bcast_no_{message.reply_to_message.message_id}",
            style="danger"
        )
    )

    bot.send_message(
        chat_id=message.chat.id,
        text="🏵️ **क्या आप इस ब्रॉडकास्ट मैसेज को सभी ग्रुप्स में PIN करना चाहते हैं?**",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith(('bcast_yes_', 'bcast_no_')))
def execute_broadcast_callback(call):
    if OWNER_ID and call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "❌ You are not authorized to control this broadcast!", show_alert=True)
        return

    data_parts = call.data.split('_')
    should_pin = (data_parts[1] == 'yes')
    target_msg_id = int(data_parts[2])

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="📢 **Initializing broadcast process, please wait....**",
        parse_mode="Markdown"
    )

    # MongoDB से सभी चैट्स लोड करें
    all_chats = list(groups_collection.find({}, {"chat_id": 1}))
    all_users = list(users_collection.find({}, {"user_id": 1}))

    g_success, g_fail = 0, 0
    u_success, u_fail = 0, 0

    for chat_doc in all_chats:
        chat_id = chat_doc.get('chat_id')
        try:
            sent_msg = bot.copy_message(
                chat_id=chat_id, 
                from_chat_id=call.message.chat.id, 
                message_id=target_msg_id
            )
            
            if should_pin and sent_msg and hasattr(sent_msg, 'message_id'):
                try:
                    bot.pin_chat_message(
                        chat_id=chat_id, 
                        message_id=sent_msg.message_id, 
                        disable_notification=False
                    )
                except Exception:
                    pass

            g_success += 1
            time.sleep(0.15)  
        except Exception: 
            g_fail += 1

    for user_doc in all_users:
        user_id = user_doc.get('user_id')
        try:
            bot.copy_message(
                chat_id=user_id, 
                from_chat_id=call.message.chat.id, 
                message_id=target_msg_id
            )
            u_success += 1
            time.sleep(0.15)  
        except Exception: 
            u_fail += 1

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
    
    markup.add(InlineKeyboardButton(
        text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", 
        url=add_to_group_url,
        style="success"
    ))

    # MongoDB से सभी ग्रुप्स लोड करें
    all_chats = list(groups_collection.find({}, {"chat_id": 1}))
    success_count = 0
    
    for chat_doc in all_chats:
        chat_id = chat_doc.get('chat_id')
        
        # MongoDB से scores लोड करें
        all_users = list(daily_scores_collection.find({"chat_id": chat_id}))
        
        calculated_leaderboard = []
        for user_doc in all_users:
            correct = user_doc.get('correct_count', 0)
            wrong = user_doc.get('wrong_count', 0)
            name = user_doc.get('user_name', 'Unknown')
            
            final_score = (correct * 2) - (wrong * 0.5)
            if (correct + wrong) > 0:
                calculated_leaderboard.append((final_score, name, correct, wrong))
        
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
        except Exception: 
            pass
        
    # MongoDB से scores साफ़ करें
    daily_scores_collection.delete_many({})
    poll_mapping_collection.delete_many({})
        
    try:
        bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=f"✅ **Chief, the manual result has been successfully sent!**\n📊 Total **{success_count}** Leaderboards sent.", parse_mode="Markdown")
    except Exception: 
        pass

def daily_leaderboard_scheduler():
    has_sent_today = False
    last_checked_date = ""
    
    markup = InlineKeyboardMarkup()
    add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    
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

            # MongoDB से leaderboard_time लोड करें
            time_setting = bot_settings_collection.find_one({"key": "leaderboard_time"})
            db_time = time_setting.get('value', "22:00") if time_setting else "22:00"
            
            try: 
                target_hour, target_minute = map(int, db_time.split(':'))
            except Exception: 
                target_hour, target_minute = 22, 0
            
            if now.hour == target_hour and now.minute == target_minute and not has_sent_today:
                # MongoDB से सभी ग्रुप्स लोड करें
                all_chats = list(groups_collection.find({}, {"chat_id": 1}))
                
                for chat_doc in all_chats:
                    chat_id = chat_doc.get('chat_id')
                    
                    # MongoDB से scores लोड करें
                    all_users = list(daily_scores_collection.find({"chat_id": chat_id}))
                    
                    calculated_leaderboard = []
                    for user_doc in all_users:
                        correct = user_doc.get('correct_count', 0)
                        wrong = user_doc.get('wrong_count', 0)
                        name = user_doc.get('user_name', 'Unknown')
                        
                        final_score = (correct * 2) - (wrong * 0.5)
                        if (correct + wrong) > 0:
                            calculated_leaderboard.append((final_score, name, correct, wrong))
                            
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
                        
                # MongoDB से सभी scores हटाएं
                daily_scores_collection.delete_many({})
                poll_mapping_collection.delete_many({})
                    
                has_sent_today = True
                time.sleep(60) 
                
        except Exception as sched_err:
            print(f"शेड्यूलर एरर: {sched_err}")
        time.sleep(20)

# 🎯 LIVE पोल उत्तर ट्रैकर (MongoDB के साथ)
@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    poll_id = str(poll_answer.poll_id)
    user_id = poll_answer.user.id
    
    first_name = poll_answer.user.first_name if poll_answer.user.first_name else ""
    last_name = poll_answer.user.last_name if poll_answer.user.last_name else ""
    user_name = f"{first_name} {last_name}".strip()
    if not user_name: 
        user_name = f"User_{user_id}"

    if not poll_answer.option_ids:
        return

    # MongoDB से poll mapping लोड करें
    mapping = poll_mapping_collection.find_one({"poll_id": poll_id})
    
    if not mapping:
        print(f"⚠️ चेतावनी: Poll ID {poll_id} डेटाबेस मैपिंग में नहीं मिली!")
        return  

    chat_id = mapping.get('chat_id')
    correct_id = mapping.get('correct_id')
    creation_time = mapping.get('creation_time', time.time())
    chosen_option = poll_answer.option_ids[0]
    
    # 24 घंटे का एंटी-चीट फ़िल्टर
    if time.time() - creation_time > 86400:
        return  

    # MongoDB में score अपडेट करें
    if chosen_option == correct_id:
        daily_scores_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {
                "$set": {"user_name": user_name},
                "$inc": {"correct_count": 1}
            },
            upsert=True
        )
    else:
        daily_scores_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {
                "$set": {"user_name": user_name},
                "$inc": {"wrong_count": 1}
            },
            upsert=True
        )

# 📊 यूजर लाइव स्कोर ट्रैकर कस्टमाइज्ड कमांड
@bot.message_handler(commands=['myscore'])
def check_user_score(message):
    chat_type = message.chat.type

    if chat_type == 'private':
        try: bot.reply_to(message, "❌ This command can only be used in groups.")
        except Exception: pass
        return  

    user_id = message.from_user.id
    chat_id = message.chat.id

    try: bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception: pass

    # MongoDB से score लोड करें
    score_doc = daily_scores_collection.find_one({"chat_id": chat_id, "user_id": user_id})
    
    if score_doc:
        correct = score_doc.get('correct_count', 0)
        wrong = score_doc.get('wrong_count', 0)
        old_score_msg_id = score_doc.get('last_score_msg_id', 0)
        final_score = (correct * 2) - (wrong * 0.5)
    else:
        correct, wrong, old_score_msg_id, final_score = 0, 0, 0, 0.0

    if old_score_msg_id > 0:
        try: bot.delete_message(chat_id=chat_id, message_id=old_score_msg_id)
        except Exception: pass

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
        new_score_msg = bot.send_message(chat_id=chat_id, text=score_text, parse_mode="Markdown")
        
        # नए स्कोर कार्ड की आईडी को MongoDB में अपडेट करें
        daily_scores_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {
                "$set": {
                    "user_name": message.from_user.first_name,
                    "last_score_msg_id": new_score_msg.message_id
                }
            },
            upsert=True
        )
    except Exception: 
        pass

# 💬 /start कमांड (MongoDB के साथ)
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_type = message.chat.type
    message_text = message.text.strip() if message.text else ""
    
    if chat_type in ['group', 'supergroup']:
        expected_full_command = f"/start@{BOT_USERNAME}"
        if "@" in message_text and not message_text.startswith(expected_full_command):
            return  

    first_name = message.from_user.first_name if message.from_user.first_name else ""
    last_name = message.from_user.last_name if message.from_user.last_name else ""
    full_name = f"{first_name} {last_name}".strip()
    if not full_name: 
        full_name = f"User_{user_id}"

    image_folder = "images"
    selected_image_path = None

    try:
        if os.path.exists(image_folder) and os.path.isdir(image_folder):
            all_images = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            if all_images:
                selected_image_path = os.path.join(image_folder, random.choice(all_images))
    except Exception as e:
        print(f"इमेज फोल्डर रीड करने में एरर: {e}")

    # 📌 Group Chat Logic
    if chat_type in ['group', 'supergroup']:
        group_data = groups_collection.find_one({"chat_id": message.chat.id})
        old_start_id = group_data.get('start_msg_id', 0) if group_data else 0

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
                raise ValueError("No image found")
        except Exception: 
            try:
                new_msg = bot.send_message(chat_id=message.chat.id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
            except Exception: pass

        if new_msg:
            try:
                groups_collection.update_one(
                    {"chat_id": message.chat.id},
                    {"$set": {"start_msg_id": new_msg.message_id}},
                    upsert=True
                )
            except Exception: pass
        return  

    # Private Chat Logic
    users_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_name": full_name,
                "join_time": time.time()
            }
        },
        upsert=True
    )

    if OWNER_ID and user_id == OWNER_ID:
        time_setting = bot_settings_collection.find_one({"key": "leaderboard_time"})
        db_time = time_setting.get('value', "22:00") if time_setting else "22:00"
        
        welcome_text = (
            f"👑 **प्रणाम मालिक ({message.from_user.first_name})!**\n\n"
            f"📊 वर्तमान लीडरबोर्ड टाइम: **{db_time}**\n"
            "⚙️ आप सीधे यहीं पर `/settime HH:MM` लिखकर टाइम बदल सकते हैं।\n"
            "🏆 तुरंत रिज़ल्ट भेजने के लिए `/sendresult` लिखें।\n"
            "📢 किसी भी मैसेज पर रिप्लाई करके `/broadcast` लिखने से वह सभी ग्रुप्स में जाएगा।\n"
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

# ℹ️ हेल्प कमांड (MongoDB के साथ)
@bot.message_handler(commands=['help'])
def send_help(message):
    chat_type = message.chat.type
    message_text = message.text.strip() if message.text else ""
    
    if chat_type in ['group', 'supergroup']:
        expected_full_command = f"/help@{BOT_USERNAME}"
        if "@" in message_text and not message_text.startswith(expected_full_command):
            return

    if chat_type in ['group', 'supergroup']:
        group_data = groups_collection.find_one({"chat_id": message.chat.id})
        old_help_id = group_data.get('help_msg_id', 0) if group_data else 0

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
    
    owner_url = f"tg://user?id={int(OWNER_ID)}"
    markup.add(InlineKeyboardButton(text="💬 Contact Support", url=owner_url))
    
    try: 
        new_help_msg = bot.send_message(chat_id=message.chat.id, text=help_text, reply_markup=markup, parse_mode="Markdown")
        
        if chat_type in ['group', 'supergroup']:
            groups_collection.update_one(
                {"chat_id": message.chat.id},
                {"$set": {"help_msg_id": new_help_msg.message_id}},
                upsert=True
            )
                
            try:
                bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
            except Exception:
                pass
                
    except Exception: 
        pass

# 📊 लाइव स्टेटस कमांड (MongoDB के साथ)
GROUPS_PER_PAGE = 10

@bot.message_handler(commands=['status'])
def send_stats(message):
    is_owner = (OWNER_ID and message.from_user.id == OWNER_ID)
    is_valid_chat = (message.chat.type == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))

    if not (is_owner and is_valid_chat):
        try: bot.send_message(message.chat.id, "❌ This command is only valid for the bot owner and in authorized chats.")
        except Exception: pass
        return

    status_msg = bot.send_message(message.chat.id, "⏳ **Fetching statistics and group data... Please wait...**", parse_mode="Markdown")
    
    text, markup = generate_status_page(page=0)
    try:
        bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        try: bot.send_message(chat_id=message.chat.id, text=text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception: pass

def generate_status_page(page=0):
    # MongoDB से सभी ग्रुप्स लोड करें
    all_chats = list(groups_collection.find({}, {"chat_id": 1}))
    
    # MongoDB से सभी यूजर्स काउंट लोड करें
    u_count = users_collection.count_documents({})

    g_count = len(all_chats)
    start_idx = page * GROUPS_PER_PAGE
    end_idx = start_idx + GROUPS_PER_PAGE
    current_page_groups = all_chats[start_idx:end_idx]
    
    total_pages = (g_count + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE
    if total_pages == 0: 
        total_pages = 1

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
        for idx, chat_doc in enumerate(current_page_groups, start_idx + 1):
            chat_id = chat_doc.get('chat_id')
            try:
                chat_info = bot.get_chat(chat_id)
                group_name = chat_info.title
                
                try:
                    invite_link = bot.export_chat_invite_link(chat_id)
                    link_text = f"[Click to Join]({invite_link})"
                except Exception:
                    if chat_info.username:
                        link_text = f"[Click to Join](https://t.me/{chat_info.username})"
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("statpage_") or call.data == "status_close")
def handle_status_pagination(call):
    if not (OWNER_ID and call.from_user.id == OWNER_ID):
        bot.answer_callback_query(call.id, text="❌ This menu is only for the bot owner.", show_alert=True)
        return

    if call.data == "status_close":
        try:
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            pass
        return

    try:
        target_page = int(call.data.split("_")[1])
        bot.answer_callback_query(call.id, text=f"Loading Page {target_page + 1}...")
        text, markup = generate_status_page(page=target_page)
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        print(f"पेज बदलने में एरर: {e}")

# 🤖 ग्रुप जॉइन/लीव ट्रैकर (MongoDB के साथ)
@bot.my_chat_member_handler()
def handle_left_or_joined(my_chat_member):
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status
    chat_id = my_chat_member.chat.id
    chat_title = my_chat_member.chat.title
    
    if new_status in ["administrator", "member"]:
        group_exists = groups_collection.find_one({"chat_id": chat_id})
        
        if not group_exists or old_status in ["left", "kicked"]:
            if not group_exists:
                groups_collection.insert_one({
                    "chat_id": chat_id,
                    "interval": 1800,
                    "last_sent_time": 0,
                    "current_index": 0,
                    "language": "hindi",
                    "auto_delete": 1
                })
            
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
                f"💡 **Results** का wait नहीं करना चाहते तो `/myscore` command भेजें!\n\n"
                f"🚀 **How to Get Started:**\n"
                f"1. Make me a **Group Admin** (so I have permission to send polls).\n"
                f"2. Use the `/settings` command inside your group to configure everything.\n\n"
                f"For any help, simply type `/help`."
            )
            
            group_markup = InlineKeyboardMarkup()
            add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
            group_markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url))
            
            try:
                if selected_image_path:
                    with open(selected_image_path, "rb") as photo_file:
                        bot.send_photo(chat_id=chat_id, photo=photo_file, caption=group_text, reply_markup=group_markup, parse_mode="Markdown")
                else:
                    bot.send_message(chat_id=chat_id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
            except Exception:
                try:
                    bot.send_message(chat_id=chat_id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
                except Exception: 
                    pass
            
    elif new_status in ["left", "kicked"]:
        groups_collection.delete_one({"chat_id": chat_id})

# 🌐 Flask Web Server Setup (Render के लिए)
from flask import Flask
app = Flask('')

@app.route('/')
def home():
    return "Bot is running perfectly 24/7!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# 🔄 Self-Ping Function
def keep_alive_ping():
    while True:
        time.sleep(300)
        try:
            render_url = "https://quiz-bot-2-rhg0.onrender.com" 
            requests.get(render_url)
            print("Self-ping successful, keeping bot alive!")
        except Exception as e:
            print(f"Self-ping failed: {e}")

if __name__ == "__main__":
    # Web server को background thread में start करें
    threading.Thread(target=run_web_server, daemon=True).start()

    # Self-ping loop को background thread में start करें
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    # Background functions start करें
    try:
        threading.Thread(target=global_poll_manager, daemon=True).start()
        threading.Thread(target=daily_leaderboard_scheduler, daemon=True).start()
    except NameError:
        print("Warning: Background functions नहीं मिले!")

    print("Successfully 🇮🇳 deployed with MongoDB...🚀")
    
    # Infinity polling loop शुरू करें
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
