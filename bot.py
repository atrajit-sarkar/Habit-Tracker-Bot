import telebot
from telebot import types
import datetime
import tempfile
import os
import threading
import time
import requests
import urllib3

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass
from db import StreakDB
from firestore_db import FirestoreDB
from utils import format_progress_message, generate_progress_html, generate_dashboard_html

# Hardcode Firestore credentials JSON path (adjust filename if you rename the key)
try:
    _CRED_PATH = os.path.join(os.path.dirname(__file__), 'service.json')
    if os.path.isfile(_CRED_PATH) and not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = _CRED_PATH
except Exception:
    pass

# Initialize bot with your token (fetched from environment)
API_TOKEN = os.environ.get("API_TOKEN", "")
if not API_TOKEN:
    print("⚠️ API_TOKEN not set. Set it in environment or .env file.")
bot = telebot.TeleBot(API_TOKEN) if API_TOKEN else telebot.TeleBot('TEST_PLACEHOLDER')

# Restrict bot to a single chat (private chat or group). Set via env ALLOWED_CHAT_ID or hardcode.
# When set (non-zero), the bot will IGNORE all other chats and will not send any messages.
def _safe_int(val: str | None, default: int = 0) -> int:
    try:
        return int(val) if val not in (None, "") else default
    except Exception:
        return default

ALLOWED_CHAT_ID = _safe_int(os.environ.get("ALLOWED_CHAT_ID"), 0)

def is_allowed_chat(chat_id: int):
    try:
        return ALLOWED_CHAT_ID == 0 or int(chat_id) == ALLOWED_CHAT_ID
    except Exception:
        return False
# Choose database backend: Firestore if configured and available, else SQLite
DB_BACKEND = os.environ.get("DB_BACKEND", "firestore").lower()  # 'firestore' | 'sqlite' | 'auto'
SQLITE_PATH = os.path.join(os.path.dirname(__file__), 'streaks.db')
db = None
if DB_BACKEND in ("firestore", "auto"):
    try:
        db = FirestoreDB(sqlite_path=SQLITE_PATH, project_id=os.environ.get("GCP_PROJECT"))
        print("🗄️ Using Firestore database backend")
    except Exception as e:
        if DB_BACKEND == "firestore":
            raise
        print(f"⚠️ Firestore unavailable, falling back to SQLite: {e}")
        db = StreakDB(SQLITE_PATH)
else:
    db = StreakDB(SQLITE_PATH)
    print("🗄️ Using SQLite database backend")

# User contexts for multi-step commands
user_contexts = {}

# Store poll contexts for tracking poll responses
poll_contexts = {}

# Track sent reminders to avoid duplicate notifications across minute boundaries
# Keyed by (user_id, task_id, date_str, schedule_time)
sent_reminders = set()

def create_main_menu():
    """Create main menu keyboard"""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        types.KeyboardButton("📊 Progress Dashboard"),
        types.KeyboardButton("✅ Mark Complete"),
        types.KeyboardButton("📝 Add Task"),
        types.KeyboardButton("🗑️ Delete Task")
    )
    return markup

def send_scheduled_poll(user_id, task_id, task_name):
    """Send a poll for a scheduled task"""
    try:
        # Gate: only send to allowed chat
        if not is_allowed_chat(user_id):
            return
        poll = bot.send_poll(
            user_id,
            f'⏰ Reminder: Did you complete "{task_name}" today?',
            ['✅ Yes, I completed it!', '❌ No, not yet'],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        # Store poll context for later processing
        poll_contexts[poll.poll.id] = {
            'user_id': user_id,
            'task_id': task_id,
            'task_name': task_name,
            'sent_at': datetime.datetime.now()
        }
        
        # Also send a fallback message
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"✅ Mark {task_name} Complete", callback_data=f"complete_{task_id}"))
        
        bot.send_message(
            user_id,
            f"⏰ <b>Scheduled Reminder</b>\n\n📝 Time to work on: <b>{task_name}</b>\n\nVote in the poll above or use the button below!",
            parse_mode='HTML',
            reply_markup=markup
        )
        
    except Exception as e:
        print(f"Error sending scheduled poll to user {user_id}: {e}")

def check_scheduled_tasks():
    """Check for tasks that need polling reminders"""
    while True:
        try:
            now = datetime.datetime.now()
            # Prune sent reminders from previous days
            today_str = datetime.date.today().strftime('%Y-%m-%d')
            global sent_reminders
            sent_reminders = {k for k in sent_reminders if k[2] == today_str}
            prev = now - datetime.timedelta(minutes=1)
            # Build set of times to check: padded and unpadded hour variants
            def time_variants(dt: datetime.datetime):
                hhmm = dt.strftime('%H:%M')
                h = dt.hour
                mm = dt.strftime('%M')
                variants = {hhmm}
                if h < 10:
                    variants.add(f"{h}:{mm}")
                return variants
            times_to_check = set()
            times_to_check |= time_variants(now)
            times_to_check |= time_variants(prev)

            # Backend-agnostic lookup with deduplication
            scheduled_tasks = []
            seen_pairs = set()
            if hasattr(db, 'get_tasks_scheduled_at'):
                for t in times_to_check:
                    try:
                        for row in db.get_tasks_scheduled_at(t):
                            key = (row[0], row[1])  # (user_id, task_id)
                            if key not in seen_pairs:
                                seen_pairs.add(key)
                                scheduled_tasks.append(row)
                    except Exception as _e:
                        continue
            else:
                # Fallback path for SQLite backend using public API
                with db._lock:
                    conn = db._conn()
                    cur = conn.cursor()
                    cur.execute("SELECT DISTINCT user_id FROM tasks WHERE is_active=1")
                    users = [r[0] for r in cur.fetchall()]
                    for uid in users:
                        for t in times_to_check:
                            cur.execute(
                                "SELECT id, name, schedule_time FROM tasks WHERE user_id=? AND is_active=1 AND schedule_time=?",
                                (uid, t)
                            )
                            for tid, tname, stime in cur.fetchall():
                                key = (uid, tid)
                                if key not in seen_pairs:
                                    seen_pairs.add(key)
                                    scheduled_tasks.append((uid, tid, tname, stime))
                    conn.close()
            
            # Send polls for scheduled tasks
            for user_id, task_id, task_name, schedule_time in scheduled_tasks:
                # Check if we already sent a poll today for this task
                today = today_str
                # Skip if we already sent reminder for this user/task/time today
                key_sent = (user_id, task_id, today, schedule_time)
                if key_sent in sent_reminders:
                    continue
                # Check if already completed today
                if hasattr(db, 'is_completed_on_date'):
                    already_completed = db.is_completed_on_date(user_id, task_id, today)
                else:
                    with db._lock:
                        conn = db._conn()
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT COUNT(*) FROM streaks WHERE user_id=? AND date=? AND task_id=?",
                            (user_id, today, task_id)
                        )
                        already_completed = cur.fetchone()[0] > 0
                        conn.close()
                
                if not already_completed:
                    if not is_allowed_chat(user_id):
                        # Skip but log lightly for diagnostics
                        print(f"Scheduler: skip user {user_id} not allowed for task {task_id}")
                    else:
                        send_scheduled_poll(user_id, task_id, task_name)
                        # Mark as sent to avoid duplicates in the next minute
                        sent_reminders.add(key_sent)
            
            # Sleep for 60 seconds before checking again
            time.sleep(60)
            
        except Exception as e:
            print(f"Error in scheduled task checker: {e}")
            time.sleep(60)

# Start the scheduled task checker in a background thread
def start_scheduler():
    scheduler_thread = threading.Thread(target=check_scheduled_tasks, daemon=True)
    scheduler_thread.start()
    print("📅 Scheduled task checker started")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    welcome_text = f"""
🎯 <b>Welcome to your Habit Tracker!</b>

I'll help you track your daily habits and build consistent streaks!

<b>Available Commands:</b>
• 📊 <b>Dashboard</b> - View comprehensive progress
• ✅ <b>Mark Complete</b> - Mark today's task as done
• 📝 <b>Add Task</b> - Create new habit to track
• 🗑️ <b>Delete Task</b> - Remove existing task

Let's build some amazing habits together! 🚀
    """
    bot.send_message(message.chat.id, welcome_text, parse_mode='HTML', reply_markup=create_main_menu())

@bot.message_handler(commands=['help'])
def send_help(message):
    if not is_allowed_chat(message.chat.id):
        return
    help_text = """
🤖 <b>Enhanced Habit Tracker Bot</b>

<b>📊 Progress & Analytics:</b>
/progress - Comprehensive dashboard with beautiful charts
/simple - Quick text progress summary

<b>✅ Task Management:</b>
/complete - Mark task complete (manual anytime)
/add - Add new habit with scheduling options
/tasks - List all your active habits
/alltasks - Detailed list with schedules, streaks, and metadata
/report - Get an individual task HTML progress report
/delete - Delete a habit (cleans all data)

<b>⏰ Smart Scheduling System:</b>
• Set reminder times when creating tasks
• Get automatic <b>polls</b> at scheduled times
• Vote "Yes" in poll → auto-marks complete + progress report
• Vote "No" → encouraging message
• Manual completion always available alongside polls

<b>🧪 Testing & Features:</b>
/testpoll - Test the polling system
/start - Reset and show main menu

<b>🎯 How It Works:</b>
<b>1. Scheduled Tasks:</b>
   • ⏰ At your set time (e.g., 7:00 AM)
   • 🔍 Bot checks if task already done today
   • ❌ If not → Sends poll reminder
   • 🗳️ Vote Yes → Auto-complete + HTML progress report
   
<b>2. Manual Completion:</b>
   • Use ✅ Mark Complete button anytime
   • Smart duplicate detection (won't count twice)
   • Instant feedback with total count

<b>3. Beautiful Reports:</b>
   • Personalized HTML dashboards
   • Individual task progress with calendars
   • Responsive design for all devices

<b>✨ Key Features:</b>
• No duplicate counting (smart detection)
• Deleted tasks removed from all reports
• Both poll and manual completion methods
• Beautiful, professional progress reports
• Motivational feedback and encouragement

Start building amazing habits! 🚀
    """
    bot.send_message(message.chat.id, help_text, parse_mode='HTML')

@bot.message_handler(commands=['progress'])
def cmd_progress(message):
    """Show comprehensive dashboard"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    today = datetime.date.today()
    
    try:
        html_content = generate_dashboard_html(db, user_id, today)
        
        # Save to temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                bot.send_document(
                    message.chat.id, 
                    f, 
                    caption="📊 Your Complete Habit Dashboard\n\nOpen this file in your browser to view your beautiful progress report!",
                    visible_file_name="habit_dashboard.html"
                )
        finally:
            os.unlink(temp_path)
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error generating dashboard: {str(e)}")

@bot.message_handler(commands=['simple'])
def cmd_simple_progress(message):
    """Show simple text progress"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    today = datetime.date.today()
    
    try:
        progress_msg = format_progress_message(db, user_id, today)
        bot.send_message(message.chat.id, progress_msg, parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['report'])
def cmd_task_report(message):
    """Let user pick a task and send its individual HTML progress report"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id)

    if not tasks:
        bot.send_message(
            message.chat.id,
            "❌ You don't have any tasks yet. Use /add to create one!",
            reply_markup=create_main_menu()
        )
        return

    markup = types.InlineKeyboardMarkup()
    for task in tasks:
        task_id, name, *_ = task
        markup.add(types.InlineKeyboardButton(f"📊 {name}", callback_data=f"report_{task_id}"))
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel"))

    bot.send_message(
        message.chat.id,
        "<b>Which task do you want a progress report for?</b>",
        reply_markup=markup,
        parse_mode='HTML'
    )

@bot.message_handler(commands=['complete'])
def cmd_complete(message):
    """Mark task as complete"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id)
    
    if not tasks:
        bot.send_message(
            message.chat.id, 
            "❌ You don't have any tasks yet! Use /add to create your first habit.",
            reply_markup=create_main_menu()
        )
        return
    
    # Create task selection keyboard
    markup = types.InlineKeyboardMarkup()
    for task in tasks:
        task_id, name, description, frequency, schedule_time, created_at = task
        markup.add(types.InlineKeyboardButton(f"✅ {name}", callback_data=f"complete_{task_id}"))
    
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    
    bot.send_message(
        message.chat.id, 
        "📝 <b>Which task did you complete today?</b>", 
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.message_handler(commands=['add'])
def cmd_add_task(message):
    """Start add task workflow"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    user_contexts[user_id] = {"step": "task_name"}
    
    bot.send_message(
        message.chat.id,
        "📝 <b>Let's create a new habit!</b>\n\nWhat would you like to call this task?\n\n<i>Example: 'Daily Exercise', 'Read for 30 minutes', 'Drink 8 glasses of water'</i>",
        parse_mode='HTML'
    )

@bot.message_handler(commands=['tasks'])
def cmd_list_tasks(message):
    """List all user tasks"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id)
    
    if not tasks:
        bot.send_message(
            message.chat.id, 
            "📝 You don't have any tasks yet!\n\nUse /add to create your first habit.",
            reply_markup=create_main_menu()
        )
        return
    
    response = ["📋 <b>Your Habits:</b>\n"]
    for i, task in enumerate(tasks, 1):
        task_id, name, description, frequency, schedule_time, created_at = task
        total_days = db.total_days(user_id, task_id)
        
        schedule_info = f"⏰ {schedule_time}" if schedule_time else "📅 Manual"
        
        response.append(
            f"<b>{i}. {name}</b>\n"
            f"   📝 {description}\n"
            f"   🔄 {frequency.title()} | {schedule_info}\n"
            f"   📊 {total_days} days completed\n"
        )
    
    bot.send_message(message.chat.id, '\n'.join(response), parse_mode='HTML')

@bot.message_handler(commands=['alltasks'])
def cmd_list_all_tasks(message):
    """List all tasks with detailed metadata and schedules"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id)

    if not tasks:
        bot.send_message(
            message.chat.id,
            "📝 You don't have any tasks yet!\n\nUse /add to create your first habit.",
            reply_markup=create_main_menu()
        )
        return

    lines = ["🧾 <b>Your Tasks (Detailed)</b>\n"]
    for i, task in enumerate(tasks, 1):
        task_id, name, description, frequency, schedule_time, created_at = task
        total = db.total_days(user_id, task_id)
        month_cnt = db.get_task_month_count(user_id, task_id)
        last_done = db.get_task_last_done(user_id, task_id)
        cur_streak, best_streak = db.get_task_streaks(user_id, task_id)
        schedules = db.get_schedules_for_task(user_id, task_id)

        lines.append(f"<b>{i}. {name}</b> (ID: {task_id})")
        if description:
            lines.append(f"   📝 {description}")
        lines.append(f"   🔄 Frequency: <b>{frequency.title()}</b>")
        if schedule_time:
            lines.append(f"   ⏰ Default time: <b>{schedule_time}</b>")
        if schedules:
            sch_list = ", ".join([f"{t} {z}" for _, t, z in schedules])
            lines.append(f"   📅 Schedules: {sch_list}")
        else:
            lines.append("   📅 Schedules: None")

        lines.append(f"   📊 Totals: <b>{total}</b> days (This month: {month_cnt})")
        lines.append(f"   🔥 Streaks: current <b>{cur_streak}</b> | best <b>{best_streak}</b>")
        lines.append(f"   🗓️ Created: {created_at}")
        lines.append(f"   ✅ Last done: {last_done if last_done else '—'}\n")

    bot.send_message(message.chat.id, "\n".join(lines), parse_mode='HTML')

@bot.message_handler(commands=['delete'])
def cmd_delete_task(message):
    """Delete a task"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id)
    
    if not tasks:
        bot.send_message(
            message.chat.id, 
            "❌ You don't have any tasks to delete!",
            reply_markup=create_main_menu()
        )
        return
    
    # Create task selection keyboard
    markup = types.InlineKeyboardMarkup()
    for task in tasks:
        task_id, name, description, frequency, schedule_time, created_at = task
        markup.add(types.InlineKeyboardButton(f"🗑️ {name}", callback_data=f"delete_{task_id}"))
    
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    
    bot.send_message(
        message.chat.id, 
        "🗑️ <b>Which task would you like to delete?</b>\n\n⚠️ <i>This action cannot be undone!</i>", 
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.message_handler(commands=['testpoll'])
def cmd_test_poll(message):
    """Test polling functionality (for debugging)"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id)
    
    if not tasks:
        bot.send_message(message.chat.id, "❌ You need to create tasks first!")
        return
    
    markup = types.InlineKeyboardMarkup()
    for task in tasks:
        task_id, name, description, frequency, schedule_time, created_at = task
        markup.add(types.InlineKeyboardButton(f"📊 Test {name}", callback_data=f"testpoll_{task_id}"))
    
    bot.send_message(
        message.chat.id,
        "🧪 <b>Test Poll Feature</b>\n\nSelect a task to test the polling system:",
        parse_mode='HTML',
        reply_markup=markup
    )

# Handle keyboard button presses
@bot.message_handler(func=lambda message: message.text in [
    "📊 Progress Dashboard", "✅ Mark Complete", "📝 Add Task", "🗑️ Delete Task"
])
def handle_keyboard_buttons(message):
    if not is_allowed_chat(message.chat.id):
        return
    if message.text == "📊 Progress Dashboard":
        cmd_progress(message)
    elif message.text == "✅ Mark Complete":
        cmd_complete(message)
    elif message.text == "📝 Add Task":
        cmd_add_task(message)
    elif message.text == "🗑️ Delete Task":
        cmd_delete_task(message)

@bot.message_handler(func=lambda message: message.from_user.id in user_contexts)
def handle_context_messages(message):
    """Handle multi-step command workflows"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    context = user_contexts[user_id]
    step = context["step"]
    
    if step == "task_name":
        # Store task name and ask for description
        context["name"] = message.text.strip()
        context["step"] = "task_description"
        
        bot.send_message(
            message.chat.id,
            f"✅ Great! Task name: <b>'{context['name']}'</b>\n\nNow, please provide a brief description of this habit:\n\n<i>Example: 'Go for a 30-minute walk', 'Read at least 10 pages', 'Meditate for 15 minutes'</i>",
            parse_mode='HTML'
        )
        
    elif step == "task_description":
        # Store description and ask for frequency
        context["description"] = message.text.strip()
        context["step"] = "task_frequency"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("📅 Daily", callback_data="freq_daily"),
            types.InlineKeyboardButton("📅 Weekly", callback_data="freq_weekly")
        )
        markup.add(types.InlineKeyboardButton("📅 Monthly", callback_data="freq_monthly"))
        markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
        
        bot.send_message(
            message.chat.id,
            f"✅ Description: <b>'{context['description']}'</b>\n\n🔄 <b>How often do you want to do this habit?</b>",
            parse_mode='HTML',
            reply_markup=markup
        )
        
    elif step == "schedule_time":
        # Store schedule time and create task
        schedule_time = message.text.strip()
        
        # Validate time format (basic validation)
        try:
            # Try to parse as HH:MM
            time_parts = schedule_time.split(':')
            if len(time_parts) == 2:
                hours = int(time_parts[0])
                minutes = int(time_parts[1])
                if 0 <= hours <= 23 and 0 <= minutes <= 59:
                    # Normalize to HH:MM with leading zeros
                    context["schedule_time"] = f"{hours:02d}:{minutes:02d}"
                    create_task_final(message, context)
                    return
            
            raise ValueError("Invalid time format")
            
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ <b>Invalid time format!</b>\n\nPlease use HH:MM format (24-hour).\n\n<i>Examples: 07:30, 14:00, 22:15</i>\n\nOr type 'skip' to create without scheduled reminders:",
                parse_mode='HTML'
            )

def create_task_final(message, context):
    """Create the task with all collected information"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    
    try:
        task_id = db.create_task(
            user_id=user_id,
            name=context["name"],
            description=context["description"],
            frequency=context.get("frequency", "daily"),
            schedule_time=context.get("schedule_time")
        )
        
        # Clean up context
        del user_contexts[user_id]
        
        # Create success message
        schedule_info = f"⏰ Daily at {context['schedule_time']}" if context.get("schedule_time") else "📅 Manual tracking"
        
        success_msg = f"""
✅ <b>Task Created Successfully!</b>

📝 <b>Name:</b> {context['name']}
📋 <b>Description:</b> {context['description']}
🔄 <b>Frequency:</b> {context.get('frequency', 'daily').title()}
⏰ <b>Schedule:</b> {schedule_info}

Your new habit is ready to track! Use the <b>✅ Mark Complete</b> button when you complete it.
        """
        
        bot.send_message(
            message.chat.id, 
            success_msg, 
            parse_mode='HTML',
            reply_markup=create_main_menu()
        )
        
    except Exception as e:
        bot.send_message(
            message.chat.id, 
            f"❌ Error creating task: {str(e)}",
            reply_markup=create_main_menu()
        )
        del user_contexts[user_id]

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    """Handle all inline keyboard callbacks"""
    if call.message and not is_allowed_chat(call.message.chat.id):
        return
    user_id = call.from_user.id
    data = call.data
    
    if data == "cancel":
        # Cancel any ongoing operation
        if user_id in user_contexts:
            del user_contexts[user_id]
        bot.edit_message_text(
            "❌ Operation cancelled.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.send_message(call.message.chat.id, "How can I help you next?", reply_markup=create_main_menu())
        
    elif data.startswith("complete_"):
        # Handle task completion
        task_id = int(data.split("_")[1])
        today = datetime.date.today().strftime('%Y-%m-%d')
        
        try:
            result = db.add_streak_if_missing(user_id, today, task_id)
            
            # Get task name for confirmation
            task_info = db.get_task_by_id(task_id, user_id)
            task_name = task_info[1] if task_info else "Task"
            
            total_days = db.total_days(user_id, task_id)
            
            if result:
                # New completion recorded
                message_text = f"🎉 <b>Awesome!</b>\n\n✅ '{task_name}' marked as complete for today!\n\n📊 Total completions: <b>{total_days} days</b>\n\nKeep up the great work! 💪"
                
                # Send progress HTML file
                try:
                    html_content = generate_progress_html(db, user_id, datetime.date.today(), task_id)
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                        f.write(html_content)
                        temp_path = f.name
                    
                    # First send the confirmation message
                    bot.edit_message_text(
                        message_text,
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode='HTML'
                    )
                    
                    # Then send the progress report
                    try:
                        with open(temp_path, 'rb') as f:
                            bot.send_document(
                                call.message.chat.id, 
                                f, 
                                caption=f"📊 {task_name} Progress Report",
                                visible_file_name=f"{task_name.replace(' ', '_')}_progress.html"
                            )
                    finally:
                        os.unlink(temp_path)
                        
                except Exception as e:
                    # If HTML generation fails, just show the text message
                    print(f"Error generating progress HTML: {e}")
                    
            else:
                # Already completed today
                message_text = f"✅ <b>Already Done!</b>\n\n'{task_name}' was already marked as complete for today!\n\n📊 Total completions: <b>{total_days} days</b>\n\nYou're on fire! 🔥"
                
                bot.edit_message_text(
                    message_text,
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML'
                )
            
        except Exception as e:
            bot.edit_message_text(
                f"❌ Error recording completion: {str(e)}",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data.startswith("delete_"):
        # Handle task deletion
        task_id = int(data.split("_")[1])
        
        try:
            # Get task name before deletion
            task_info = db.get_task_by_id(task_id, user_id)
            task_name = task_info[1] if task_info else "Task"
            
            # Fix: delete_task expects (user_id, task_id)
            db.delete_task(user_id, task_id)
            
            bot.edit_message_text(
                f"✅ <b>Task Deleted</b>\n\n🗑️ '{task_name}' has been permanently removed.\n\nAll associated progress data has been deleted.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML'
            )
            
        except Exception as e:
            bot.edit_message_text(
                f"❌ Error deleting task: {str(e)}",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data.startswith("testpoll_"):
        # Handle test poll request
        task_id = int(data.split("_")[1])
        task_info = db.get_task_by_id(task_id, user_id)
        
        if task_info:
            task_name = task_info[1]
            send_scheduled_poll(user_id, task_id, task_name)
            bot.edit_message_text(
                f"🧪 <b>Test Poll Sent!</b>\n\nSent a poll for '{task_name}' to test the polling system.\n\nVote in the poll to see how it works!",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML'
            )
        else:
            bot.edit_message_text(
                "❌ Task not found!",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data.startswith("report_"):
        # Send individual task report
        task_id = int(data.split("_")[1])
        task_info = db.get_task_by_id(task_id, user_id)
        task_name = task_info[1] if task_info else "Task"

        if not task_info:
            bot.edit_message_text(
                "❌ Task not found!",
                call.message.chat.id,
                call.message.message_id
            )
            return

        try:
            html_content = generate_progress_html(db, user_id, datetime.date.today(), task_id)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(html_content)
                temp_path = f.name

            # Update inline message and send the document
            bot.edit_message_text(
                f"📊 Generating report for '<b>{task_name}</b>'...",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML'
            )

            try:
                with open(temp_path, 'rb') as fdoc:
                    bot.send_document(
                        call.message.chat.id,
                        fdoc,
                        caption=f"📊 {task_name} Progress Report",
                        visible_file_name=f"{task_name.replace(' ', '_')}_progress.html"
                    )
            finally:
                os.unlink(temp_path)
        except Exception as e:
            bot.edit_message_text(
                f"❌ Error generating report: {str(e)}",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data.startswith("freq_"):
        # Handle frequency selection
        frequency = data.split("_")[1]
        
        if user_id in user_contexts:
            user_contexts[user_id]["frequency"] = frequency
            user_contexts[user_id]["step"] = "schedule_time"
            
            bot.edit_message_text(
                f"✅ Frequency: <b>{frequency.title()}</b>\n\n⏰ <b>When would you like to be reminded?</b>\n\nPlease enter a time in HH:MM format (24-hour).\n\n<i>Examples:</i>\n• 07:30 (7:30 AM)\n• 14:00 (2:00 PM)\n• 22:15 (10:15 PM)\n\nOr type <b>'skip'</b> to create without scheduled reminders:",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML'
            )
        else:
            bot.edit_message_text(
                "❌ Session expired. Please start over with /add",
                call.message.chat.id,
                call.message.message_id
            )

@bot.message_handler(func=lambda message: message.text.lower() == 'skip' and message.from_user.id in user_contexts)
def handle_skip_schedule(message):
    """Handle skipping schedule time"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    
    if user_id in user_contexts and user_contexts[user_id]["step"] == "schedule_time":
        context = user_contexts[user_id]
        context["schedule_time"] = None
        create_task_final(message, context)

@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    """Handle poll responses for scheduled tasks"""
    user_id = poll_answer.user.id
    poll_id = poll_answer.poll_id
    option_ids = poll_answer.option_ids
    
    if not option_ids or poll_id not in poll_contexts:
        return
    
    poll_context = poll_contexts[poll_id]
    # Gate: only process polls for allowed chat (PM chat id equals user id)
    if not is_allowed_chat(poll_context.get('user_id')):
        return
    task_id = poll_context['task_id']
    task_name = poll_context['task_name']
    
    # option 0 = Yes, option 1 = No
    if option_ids[0] == 0:  # User selected "Yes"
        today = datetime.date.today().strftime('%Y-%m-%d')
        result = db.add_streak_if_missing(user_id, today, task_id)
        
        if result:
            total_days = db.total_days(user_id, task_id)
            response = f"🎉 <b>Excellent!</b>\n\n✅ '{task_name}' marked as complete for today!\n\n📊 Total completions: <b>{total_days} days</b>\n\nYou're building a great habit! 💪"
        else:
            response = f"✅ '{task_name}' was already marked as complete for today!\n\nKeep up the great work! 🚀"
        
        if is_allowed_chat(user_id):
            bot.send_message(user_id, response, parse_mode='HTML')
        
        # Send progress update
        try:
            html_content = generate_progress_html(db, user_id, datetime.date.today(), task_id)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                f.write(html_content)
                temp_path = f.name
            
            try:
                with open(temp_path, 'rb') as f:
                    if is_allowed_chat(user_id):
                        bot.send_document(
                            user_id, 
                            f, 
                            caption=f"📊 {task_name} Progress Report",
                            visible_file_name=f"{task_name.replace(' ', '_')}_progress.html"
                        )
            finally:
                os.unlink(temp_path)
        except Exception as e:
            print(f"Error sending progress report: {e}")
            
    else:  # User selected "No"
        if is_allowed_chat(user_id):
            bot.send_message(
                user_id,
                f"💪 No worries! Every journey has its challenges.\n\n'{task_name}' can be completed later today.\n\nUse the ✅ Mark Complete button when you're ready! 🌟",
                parse_mode='HTML',
                reply_markup=create_main_menu()
            )
    
    # Clean up poll context
    if poll_id in poll_contexts:
        del poll_contexts[poll_id]

@bot.message_handler(func=lambda message: True)
def handle_unknown(message):
    """Handle unknown messages"""
    if not is_allowed_chat(message.chat.id):
        return
    user_id = message.from_user.id
    
    # If user is in a context, don't show unknown message
    if user_id in user_contexts:
        return
    
    bot.send_message(
        message.chat.id,
        "🤔 I didn't understand that command.\n\nUse the menu buttons below or type /help to see available commands!",
        reply_markup=create_main_menu()
    )

if __name__ == "__main__":
    print("🤖 Habit Tracker Bot starting...")
    print("📊 Chart-free version - Windows compatible!")
    print("📅 Scheduled polling system enabled!")
    if ALLOWED_CHAT_ID:
        print(f"🔒 Restricted to chat ID: {ALLOWED_CHAT_ID}")
    else:
        print("ℹ️ No chat restriction set (ALLOWED_CHAT_ID=0). Responding to all chats.")
    if not API_TOKEN or API_TOKEN == 'TEST_PLACEHOLDER':
        print("❌ No valid API token configured. Set API_TOKEN in environment/.env before running in production.")
    
    # Start the scheduled task checker
    start_scheduler()
    
    # Robust polling loop to avoid crashes on transient network errors
    backoff = 2  # seconds, exponential up to 60s
    while True:
        try:
            print("🚀 Starting Telegram polling loop...")
            # Use longer timeouts so HTTP read timeout exceeds Telegram long-poll wait
            bot.infinity_polling(timeout=50, long_polling_timeout=55)
            # If infinity_polling returns (rare), restart after a short pause
            print("ℹ️ Polling stopped gracefully. Restarting in 3s...")
            time.sleep(3)
            backoff = 2
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError, urllib3.exceptions.ReadTimeoutError) as e:
            print(f"⚠️ Network timeout/connection issue: {e}. Retrying in {backoff}s...")
            try:
                bot.stop_polling()
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except Exception as e:
            print(f"❌ Bot error: {e}. Retrying in {backoff}s...")
            try:
                bot.stop_polling()
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
