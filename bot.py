import logging
import os
import shlex
import subprocess
import threading
import time

from telegram import Document, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.error import BadRequest
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackQueryHandler,
    CallbackContext,
)

# --- Configuration ---
# Replace 'YOUR_TELEGRAM_BOT_TOKEN' with your actual bot token from BotFather
BOT_TOKEN = "6175063990:AAFTmx6mX3n7_vHMEfQt_y7jkBMgN58yx5M"

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State Definitions for ConversationHandler ---
(
    WAITING_VIDEO,
    WAITING_SUBTITLE,
    SELECTING_OPTIONS,
    PROCESSING,
) = range(4)

# --- Default Settings ---
DEFAULT_SETTINGS = {
    "resolution": "720p",
    "crf": "24",
    "codec": "libx264",
    "preset": "medium",
    "font_name": "HelveticaRounded-Bold",
    "font_size": "24",
    "margin_v": "25",
}

# --- Font Mapping ---
# Maps the font name from settings to its file path.
# Add other fonts here if you want to support more.
FONT_MAP = {
    "HelveticaRounded-Bold": os.path.join("fonts", "HelveticaRounded-Bold.ttf")
}

# --- Helper Functions ---

def get_video_duration(video_path: str) -> float:
    """Gets the total duration of a video file in seconds using ffprobe."""
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return float(result.stdout)
    except FileNotFoundError:
        logger.error("ffprobe not found. Please install FFmpeg and ensure it's in your PATH.")
        return 0.0
    except (subprocess.CalledProcessError, ValueError) as e:
        logger.error(f"Error getting video duration for {video_path}: {e}")
        return 0.0

def get_resolution_values(res_key):
    """Returns width and height for a given resolution key."""
    resolutions = {
        "480p": "854x480",
        "720p": "1280x720",
        "1080p": "1920x1080",
        "source": None
    }
    return resolutions.get(res_key)

def build_main_menu(settings: dict) -> InlineKeyboardMarkup:
    """Creates the main inline keyboard menu with current settings."""
    keyboard = [
        [
            InlineKeyboardButton(f"Resolution: {settings['resolution']}", callback_data="change_resolution"),
            InlineKeyboardButton(f"CRF: {settings['crf']}", callback_data="change_crf"),
        ],
        [
            InlineKeyboardButton(f"Codec: {settings['codec']}", callback_data="change_codec"),
            InlineKeyboardButton(f"Preset: {settings['preset']}", callback_data="change_preset"),
        ],
        [
            InlineKeyboardButton(f"Font: {settings['font_name']}", callback_data="change_font_name"),
            InlineKeyboardButton(f"Font Size: {settings['font_size']}", callback_data="change_font_size"),
        ],
        [
            InlineKeyboardButton(f"Bottom Margin: {settings['margin_v']}", callback_data="change_margin_v"),
        ],
        [
            InlineKeyboardButton("✅ Start Muxing ✅", callback_data="start_muxing"),
        ],
        [
            InlineKeyboardButton("❌ Cancel ❌", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def build_submenu(option_key: str) -> InlineKeyboardMarkup:
    """Creates a submenu for a specific option."""
    options = {
        "resolution": [("Source", "source"), ("480p", "480p"), ("720p", "720p"), ("1080p", "1080p")],
        "crf": [("20 (High Quality)", "20"), ("24 (Good)", "24"), ("28 (Low Quality)", "28")],
        "codec": [("H.264 (libx264)", "libx264"), ("H.265 (libx265)", "libx265")],
        "preset": [("Slow", "slow"), ("Medium", "medium"), ("Fast", "fast"), ("Very Fast", "veryfast")],
        "font_name": [("Helvetica Rounded", "HelveticaRounded-Bold")],
        "font_size": [("18", "18"), ("24", "24"), ("30", "30"), ("36", "36")],
        "margin_v": [("10", "10"), ("25", "25"), ("50", "50"), ("75", "75")],
    }
    buttons = [
        InlineKeyboardButton(text, callback_data=f"set_{option_key}_{value}")
        for text, value in options.get(option_key, [])
    ]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("« Back", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

# --- Command and Conversation Handlers ---

def start(update: Update, context: CallbackContext) -> int:
    """Handler for the /start command."""
    user = update.effective_user
    update.message.reply_html(
        f"👋 Hi {user.mention_html()}!\n\n"
        "I can hardcode subtitles into your videos.\n\n"
        "To get started, use the /mux command.",
    )
    return ConversationHandler.END

def mux_start(update: Update, context: CallbackContext) -> int:
    """Starts the conversation and asks for the video file."""
    context.user_data.clear()
    context.user_data['settings'] = DEFAULT_SETTINGS.copy()
    context.user_data['files'] = {}
    update.message.reply_text(
        "Great! Please send me the video file you want to process.\n"
        "You can send it as a video or as a document.\n\n"
        "You can send /cancel at any time to stop."
    )
    return WAITING_VIDEO

def video_handler(update: Update, context: CallbackContext) -> int:
    """Handles receiving a video as a direct upload or as a document."""
    file = update.message.video or update.message.document

    # Validate that the file is actually a video
    if not file or (isinstance(file, Document) and file.mime_type and not file.mime_type.startswith('video/')):
        update.message.reply_text(
            "That doesn't look like a video. Please send a video file (either as a video or a document)."
        )
        return WAITING_VIDEO

    user_dir = f"temp_{update.effective_user.id}"
    os.makedirs(user_dir, exist_ok=True)
    
    file_name = file.file_name or f"input_video_{file.file_unique_id}.mp4"
    file_path = os.path.join(user_dir, file_name)
    
    status_msg = update.message.reply_text("Downloading video...")

    try:
        new_file = context.bot.get_file(file.file_id, timeout=120)
        new_file.download(file_path, timeout=600)
    except Exception as e:
        logger.error(f"Error downloading video: {e}", exc_info=True)
        status_msg.edit_text(f"❌ Error downloading video: {e}\nPlease try again or send a different file.")
        return cancel(update, context)

    context.user_data['files']['video'] = file_path
    status_msg.edit_text("Video received! Now, please send the subtitle file (.srt or .ass).")
    return WAITING_SUBTITLE

def subtitle_handler(update: Update, context: CallbackContext) -> int:
    """Handles receiving the subtitle file."""
    doc = update.message.document
    if not doc or not (doc.file_name.lower().endswith('.srt') or doc.file_name.lower().endswith('.ass')):
        update.message.reply_text("That's not a valid subtitle file. Please send a .srt or .ass file.")
        return WAITING_SUBTITLE

    file_path = os.path.join(os.path.dirname(context.user_data['files']['video']), doc.file_name)
    status_msg = update.message.reply_text("Downloading subtitle file...")

    try:
        new_file = context.bot.get_file(doc.file_id, timeout=120)
        new_file.download(file_path, timeout=120)
    except Exception as e:
        logger.error(f"Error downloading subtitle: {e}", exc_info=True)
        status_msg.edit_text(f"❌ Error downloading subtitle: {e}\nPlease try again.")
        return WAITING_SUBTITLE

    context.user_data['files']['subtitle'] = file_path
    settings = context.user_data['settings']
    status_msg.edit_text(
        "Files received! Here are the default settings. You can change them or start muxing.",
        reply_markup=build_main_menu(settings)
    )
    return SELECTING_OPTIONS

def main_menu_callback_handler(update: Update, context: CallbackContext) -> int:
    """Handles button presses on the main settings menu."""
    query = update.callback_query
    query.answer()
    callback_data = query.data

    if callback_data == "start_muxing":
        query.edit_message_text("Starting the encoding process. This may take a while...", reply_markup=None)
        thread = threading.Thread(target=run_ffmpeg_process, args=(update, context))
        thread.start()
        return PROCESSING
    elif callback_data == "cancel":
        return cancel(update, context, message=query.message)
    elif callback_data.startswith("change_"):
        option_key = callback_data.split("_", 1)[1]
        query.edit_message_text(f"Select a new value for {option_key.replace('_', ' ').title()}:", reply_markup=build_submenu(option_key))
        return SELECTING_OPTIONS
    return SELECTING_OPTIONS

def submenu_callback_handler(update: Update, context: CallbackContext) -> int:
    """Handles button presses on the submenus for changing a setting."""
    query = update.callback_query
    query.answer()
    callback_data = query.data

    if callback_data == "back_to_main":
        settings = context.user_data['settings']
        query.edit_message_text(
            "Here are the current settings. You can change them or start muxing.",
            reply_markup=build_main_menu(settings)
        )
        return SELECTING_OPTIONS
    elif callback_data.startswith("set_"):
        parts = callback_data.split("_")
        option_key = parts[1]
        new_value = "_".join(parts[2:])
        context.user_data['settings'][option_key] = new_value
        settings = context.user_data['settings']
        query.edit_message_text(
            "Setting updated! Here are the current settings.",
            reply_markup=build_main_menu(settings)
        )
        return SELECTING_OPTIONS
    return SELECTING_OPTIONS

def run_ffmpeg_process(update: Update, context: CallbackContext):
    """Constructs and runs the FFmpeg command, handling errors and progress."""
    query = update.callback_query
    chat_id = query.message.chat_id
    status_message = query.message

    try:
        settings = context.user_data['settings']
        files = context.user_data['files']
        video_path = files['video']
        subtitle_path = files['subtitle']

        # --- FONT HANDLING ---
        font_file_path = FONT_MAP.get(settings['font_name'])
        if not font_file_path or not os.path.exists(font_file_path):
            error_msg = (
                f"🚨 **Font File Not Found\!**\n\n"
                f"I could not find the required font file for '{settings['font_name']}' at:\n`{font_file_path or 'Not Defined'}`\n\n"
                f"Please make sure you have created a `fonts` folder and that the font file is inside it."
            )
            status_message.edit_text(error_msg, parse_mode=ParseMode.MARKDOWN_V2)
            cleanup_files(context)
            return
        # --- END FONT HANDLING ---

        total_duration = get_video_duration(video_path)
        if total_duration == 0.0:
            context.bot.send_message(chat_id, "⚠️ Could not determine video duration. Progress bar will not be shown.")

        escaped_subtitle_path = subtitle_path.replace('\\', '/').replace(':', '\\:')
        escaped_font_path = font_file_path.replace('\\', '/').replace(':', '\\:')
        
        output_path = os.path.join(os.path.dirname(video_path), "output.mp4")

        command = [
            "ffmpeg", "-hide_banner",
            "-i", video_path,
            "-y",
            "-progress", "pipe:1",
            "-c:a", "copy",
            "-c:v", settings['codec'],
            "-preset", settings['preset'],
            "-crf", settings['crf'],
        ]
        resolution_value = get_resolution_values(settings['resolution'])
        if resolution_value:
            command.extend(["-s", resolution_value])
            
        subtitle_filter = (
            f"subtitles='{escaped_subtitle_path}':"
            f"force_style='FontFile={escaped_font_path},"
            f"FontSize={settings['font_size']},"
            f"MarginV={settings['margin_v']}'"
        )
        command.extend(["-vf", subtitle_filter])
        command.append(output_path)

        logger.info(f"Executing FFmpeg command: {' '.join(shlex.quote(c) for c in command)}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding='utf-8'
        )

        last_update_time = 0
        
        for line in iter(process.stdout.readline, ''):
            if total_duration > 0 and 'out_time_ms' in line:
                out_time_ms = int(line.strip().split('=')[1])
                current_time_s = out_time_ms / 1_000_000
                percentage = int((current_time_s / total_duration) * 100)

                current_monotonic_time = time.monotonic()
                if current_monotonic_time - last_update_time > 2:
                    last_update_time = current_monotonic_time
                    bar_length = 20
                    filled_length = int(bar_length * percentage / 100)
                    bar = '█' * filled_length + '─' * (bar_length - filled_length)
                    progress_text = f"⚙️ Encoding...\n\n`[{bar}] {percentage}%`"
                    try:
                        status_message.edit_text(text=progress_text, parse_mode=ParseMode.MARKDOWN_V2)
                    except BadRequest as e:
                        if "Message is not modified" not in str(e):
                            logger.warning(f"Could not update progress message: {e}")

        stderr_output = process.communicate()[1]
        process.wait()

        if process.returncode != 0:
            error_message = f"❌ FFmpeg failed\!\n\n**Error:**\n`{stderr_output[-1000:]}`"
            status_message.edit_text(error_message, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            status_message.edit_text("✅ Encoding complete! Now uploading as a document...")
            with open(output_path, 'rb') as doc_file:
                context.bot.send_document(
                    chat_id,
                    document=doc_file,
                    filename=os.path.basename(output_path),
                    caption="Here is your processed video.",
                    timeout=600
                )
            context.bot.send_message(chat_id, "Done! ✨")

    except Exception as e:
        logger.error(f"An error occurred in ffmpeg thread: {e}", exc_info=True)
        context.bot.send_message(chat_id, f"An unexpected error occurred: {e}")
    finally:
        cleanup_files(context)

def cleanup_files(context: CallbackContext):
    """Removes temporary files and directories for a user."""
    if not context.user_data:
        return
    user_dir = f"temp_{context._user_id_and_data[0]}"
    if os.path.exists(user_dir):
        try:
            for root, dirs, files in os.walk(user_dir, topdown=False):
                for name in files:
                    try:
                        os.remove(os.path.join(root, name))
                    except OSError as e:
                        logger.error(f"Error removing file {name}: {e}")
                for name in dirs:
                    try:
                        os.rmdir(os.path.join(root, name))
                    except OSError as e:
                        logger.error(f"Error removing dir {name}: {e}")
            os.rmdir(user_dir)
            logger.info(f"Cleaned up directory: {user_dir}")
        except Exception as e:
            logger.error(f"Error during cleanup of {user_dir}: {e}")
    context.user_data.clear()

def cancel(update: Update, context: CallbackContext, message=None) -> int:
    """Cancels the current operation and cleans up files."""
    if not message:
        message = update.message
    message.reply_text("Operation cancelled.")
    cleanup_files(context)
    return ConversationHandler.END

def main() -> None:
    """Starts the bot."""
    updater = Updater(BOT_TOKEN)
    dispatcher = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('mux', mux_start)],
        states={
            WAITING_VIDEO: [MessageHandler(Filters.video | Filters.document, video_handler)],
            WAITING_SUBTITLE: [MessageHandler(Filters.document, subtitle_handler)],
            SELECTING_OPTIONS: [
                CallbackQueryHandler(main_menu_callback_handler, pattern="^change_.*|start_muxing|cancel$"),
                CallbackQueryHandler(submenu_callback_handler, pattern="^set_.*|back_to_main$")
            ],
            PROCESSING: [CallbackQueryHandler(lambda u, c: u.callback_query.answer("Processing... Please wait."))]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=True,
        per_message=False
    )

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(conv_handler)

    updater.start_polling()
    logger.info("Bot started and polling...")
    updater.idle()

if __name__ == '__main__':
    main()
