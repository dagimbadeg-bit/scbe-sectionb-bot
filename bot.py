"""
Section B Grade Management Telegram Bot
Python 3.10+
Uses the existing Supabase PostgreSQL schema created for the grade system.

Required environment variables:
  TOKEN         Telegram bot token
  DATABASE_URL  Supabase PostgreSQL connection string
  ADMIN_IDS     Comma-separated Telegram numeric IDs for Super Admins

Supported grade-file formats include the university sheet headings:
  STUDENT ID NO | FULL NAME | GENDER | SECTION | Score
and simplified headings such as student_id, score.
Student ID is authoritative; full name and gender are retained for reference.
CSV, XLS, XLSX and XLSM files are supported. Uploads are validated before database changes.
"""

import os
import csv
import io
import re
import hmac
import hashlib
import secrets
import logging
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

try:
    from openpyxl import load_workbook
except ImportError:
    load_workbook = None

try:
    import xlrd
except ImportError:
    xlrd = None


# ---------------------------------------------------------------------------
# Configuration and logging
# ---------------------------------------------------------------------------

TOKEN = os.getenv("TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable.")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL environment variable.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper()
)
logger = logging.getLogger("section_b_grades")

app_flask = Flask(__name__)


@app_flask.get("/")
@app_flask.get("/health")
def health():
    return "Section B Grade Management Bot is running.", 200


def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app_flask.run(host="0.0.0.0", port=port, use_reloader=False)


threading.Thread(target=run_flask, daemon=True).start()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def db_cursor():
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=10,
        cursor_factory=RealDictCursor
    )
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()


REQUIRED_TABLES = {
    "students", "instructors", "courses", "instructor_courses",
    "assessments", "results", "audit_logs"
}


def check_database_schema():
    with db_cursor() as cur:
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
        """, (list(REQUIRED_TABLES),))
        found = {row["table_name"] for row in cur.fetchall()}
    missing = REQUIRED_TABLES - found
    if missing:
        raise RuntimeError(
            "Missing grade-system tables: " + ", ".join(sorted(missing))
        )


def audit(actor_type, actor_id, action):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO audit_logs (actor_type, actor_id, action)
            VALUES (%s, %s, %s)
        """, (actor_type, actor_id, action[:1000]))


# ---------------------------------------------------------------------------
# Passwords and authentication
# ---------------------------------------------------------------------------

PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8
LOGIN_ATTEMPTS = {}
LOGIN_LOCK_SECONDS = 300
MAX_LOGIN_ATTEMPTS = 5


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, salt.hex(), digest.hex()
    )


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def login_is_locked(user_id):
    import time
    attempts = LOGIN_ATTEMPTS.get(user_id)
    if not attempts:
        return False, 0
    count, first_time, locked_until = attempts
    if locked_until and time.time() < locked_until:
        return True, int(locked_until - time.time()) + 1
    if locked_until and time.time() >= locked_until:
        LOGIN_ATTEMPTS.pop(user_id, None)
    return False, 0


def record_login_failure(user_id):
    import time
    now = time.time()
    count, first_time, locked_until = LOGIN_ATTEMPTS.get(
        user_id, (0, now, 0)
    )
    if now - first_time > LOGIN_LOCK_SECONDS:
        count, first_time = 0, now
    count += 1
    if count >= MAX_LOGIN_ATTEMPTS:
        LOGIN_ATTEMPTS[user_id] = (count, first_time, now + LOGIN_LOCK_SECONDS)
    else:
        LOGIN_ATTEMPTS[user_id] = (count, first_time, 0)


def clear_login_failures(user_id):
    LOGIN_ATTEMPTS.pop(user_id, None)


def is_admin_telegram(user_id):
    return user_id in ADMIN_IDS


async def safe_delete(message):
    try:
        await message.delete()
    except Exception:
        pass


def set_session(context, role, record_id, display_name):
    context.user_data["session"] = {
        "role": role,
        "id": record_id,
        "name": display_name
    }


def get_session(update):
    return update.effective_user and update.effective_user.id


def session_for(update, context):
    user_id = get_session(update)
    if user_id in ADMIN_IDS:
        return {"role": "admin", "id": user_id, "name": "Super Admin"}
    return context.user_data.get("session")


def require_role(update, context, roles):
    session = session_for(update, context)
    return session if session and session.get("role") in roles else None


# ---------------------------------------------------------------------------
# Keyboards and common responses
# ---------------------------------------------------------------------------

ADMIN_MENU = [
    [KeyboardButton("Add student"), KeyboardButton("Bulk Add Students")],
    [KeyboardButton("Reset All Student Credentials")],
    [KeyboardButton("Add instructor"), KeyboardButton("Add course")],
    [KeyboardButton("Assign course"), KeyboardButton("List courses")],
    [KeyboardButton("Logout")]
]
INSTRUCTOR_MENU = [
    [KeyboardButton("My courses"), KeyboardButton("Create assessment")],
    [KeyboardButton("Upload grades"), KeyboardButton("Publish grades")],
    [KeyboardButton("Edit Published Grades")],
    [KeyboardButton("Logout")]
]
STUDENT_MENU = [
    [KeyboardButton("My courses"), KeyboardButton("View my grades")],
    [KeyboardButton("Change Password")],
    [KeyboardButton("Logout")]
]


def menu_for(role):
    rows = {
        "admin": ADMIN_MENU,
        "instructor": INSTRUCTOR_MENU,
        "student": STUDENT_MENU
    }[role]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def show_menu(update, context, text="Choose an action:"):
    session = session_for(update, context)
    if not session:
        await update.effective_message.reply_text(
            "You are not logged in. Send /login to continue."
        )
        return
    await update.effective_message.reply_text(
        text, reply_markup=menu_for(session["role"])
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin_telegram(update.effective_user.id):
        await update.message.reply_text(
            "Welcome, Super Admin. You are authenticated through ADMIN_IDS.",
            reply_markup=menu_for("admin")
        )
    elif context.user_data.get("session"):
        await show_menu(update, context, "Welcome back.")
    else:
        await update.message.reply_text(
            "Welcome to the Section B Grade Management System.\n\n"
            "Use /login to sign in with your Student ID or instructor username."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — Open the bot\n"
        "/login — Sign in\n"
        "/logout — Sign out\n"
        "/menu — Show your menu\n"
        "Students can use Change Password from their menu to update their password.\n"
        "/cancel — Cancel the current operation\n\n"
        "Students use Student ID + password. Instructors use their username "
        "+ password. Grade files must contain student_id and score columns."
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "You have been logged out. Send /login to sign in again.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("/login")]], resize_keyboard=True
        )
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("flow", None)
    context.user_data.pop("upload_assessment_id", None)
    context.user_data.pop("edit_published_mode", None)
    context.user_data.pop("pending_published_edits", None)
    for key in (
        "bulk_roster", "bulk_preview", "bulk_file_name",
        "password_change_new", "password_change_old_hash",
        "password_change_attempts"
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text("Operation cancelled.")
    await show_menu(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Login conversation
# ---------------------------------------------------------------------------

LOGIN_ID, LOGIN_PASSWORD = range(2)


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin_telegram(update.effective_user.id):
        await update.message.reply_text(
            "You are already authenticated as Super Admin.",
            reply_markup=menu_for("admin")
        )
        return ConversationHandler.END

    locked, remaining = login_is_locked(update.effective_user.id)
    if locked:
        await update.message.reply_text(
            f"Too many failed attempts. Try again in about {remaining} seconds."
        )
        return ConversationHandler.END

    context.user_data.pop("session", None)
    await update.message.reply_text(
        "Enter your Student ID (students) or instructor username:"
    )
    return LOGIN_ID


async def login_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    identity = update.message.text.strip()
    if not identity or len(identity) > 100:
        await update.message.reply_text("Invalid ID/username. Try again:")
        return LOGIN_ID
    context.user_data["login_identity"] = identity
    await update.message.reply_text("Enter your password:")
    return LOGIN_PASSWORD


async def login_get_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    await safe_delete(update.message)
    identity = context.user_data.pop("login_identity", "")
    user_id = update.effective_user.id

    locked, remaining = login_is_locked(user_id)
    if locked:
        await update.effective_chat.send_message(
            f"Login temporarily locked. Try again in {remaining} seconds."
        )
        return ConversationHandler.END

    record = None
    role = None
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT id, student_id AS identity, full_name, password_hash
                FROM students
                WHERE student_id = %s AND is_active = TRUE
            """, (identity,))
            record = cur.fetchone()
            if record:
                role = "student"
            else:
                cur.execute("""
                    SELECT id, username AS identity, full_name, password_hash
                    FROM instructors
                    WHERE username = %s AND is_active = TRUE
                """, (identity,))
                record = cur.fetchone()
                if record:
                    role = "instructor"
    except Exception:
        logger.exception("Login database lookup failed")
        await update.effective_chat.send_message(
            "The database is temporarily unavailable. Please try again."
        )
        return ConversationHandler.END

    if not record or not verify_password(password, record["password_hash"]):
        record_login_failure(user_id)
        await update.effective_chat.send_message(
            "Incorrect ID/username or password. Use /login to try again."
        )
        return ConversationHandler.END

    clear_login_failures(user_id)
    set_session(context, role, record["id"], record["full_name"])
    try:
        with db_cursor() as cur:
            table = "students" if role == "student" else "instructors"
            cur.execute(
                f"UPDATE {table} SET telegram_id = %s WHERE id = %s "
                "AND (telegram_id IS NULL OR telegram_id = %s)",
                (user_id, record["id"], user_id)
            )
        audit(role, record["id"], "Successful Telegram login")
    except Exception:
        logger.exception("Could not bind Telegram ID or audit login")

    await update.effective_chat.send_message(
        f"Welcome, {record['full_name']}!",
        reply_markup=menu_for(role)
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Student self-service password change
# ---------------------------------------------------------------------------

CHANGE_PASSWORD_CURRENT, CHANGE_PASSWORD_NEW, CHANGE_PASSWORD_CONFIRM = range(53, 56)


async def student_change_password_start(update, context):
    session = require_role(update, context, ["student"])
    if not session:
        await update.effective_message.reply_text(
            "Please log in with your student account before changing your password."
        )
        return ConversationHandler.END

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "For your security, change your password only in a private chat with this bot."
        )
        return ConversationHandler.END

    context.user_data.pop("password_change_new", None)
    context.user_data["password_change_attempts"] = 0
    await update.effective_message.reply_text(
        "Enter your current password. Your message will be deleted after it is received."
    )
    return CHANGE_PASSWORD_CURRENT


async def student_change_password_current(update, context):
    current_password = update.message.text
    await safe_delete(update.message)
    session = require_role(update, context, ["student"])
    if not session:
        context.user_data.pop("password_change_new", None)
        return ConversationHandler.END

    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM students WHERE id=%s AND is_active=TRUE",
                (session["id"],)
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("Could not verify current student password")
        await update.effective_chat.send_message(
            "The database is temporarily unavailable. Please try again later."
        )
        return ConversationHandler.END

    if not row or not verify_password(current_password, row["password_hash"]):
        attempts = int(context.user_data.get("password_change_attempts", 0)) + 1
        context.user_data["password_change_attempts"] = attempts
        if attempts >= 3:
            context.user_data.pop("password_change_attempts", None)
            await update.effective_chat.send_message(
                "Too many incorrect attempts. Password change cancelled."
            )
            return ConversationHandler.END
        await update.effective_chat.send_message(
            f"Current password is incorrect. {3 - attempts} attempt(s) remaining. Enter it again:"
        )
        return CHANGE_PASSWORD_CURRENT

    context.user_data["password_change_old_hash"] = row["password_hash"]
    await update.effective_chat.send_message(
        f"Enter your new password (at least {MIN_PASSWORD_LENGTH} characters):"
    )
    return CHANGE_PASSWORD_NEW


async def student_change_password_new(update, context):
    new_password = update.message.text
    await safe_delete(update.message)
    if len(new_password) < MIN_PASSWORD_LENGTH:
        await update.effective_chat.send_message(
            f"Password is too short. Use at least {MIN_PASSWORD_LENGTH} characters. Enter a new password:"
        )
        return CHANGE_PASSWORD_NEW

    old_hash = context.user_data.get("password_change_old_hash")
    if old_hash and verify_password(new_password, old_hash):
        await update.effective_chat.send_message(
            "Your new password must be different from your current password. Enter another password:"
        )
        return CHANGE_PASSWORD_NEW

    context.user_data["password_change_new"] = new_password
    await update.effective_chat.send_message(
        "Re-enter your new password to confirm it:"
    )
    return CHANGE_PASSWORD_CONFIRM


async def student_change_password_confirm(update, context):
    confirmation = update.message.text
    await safe_delete(update.message)
    new_password = context.user_data.get("password_change_new")
    old_hash = context.user_data.get("password_change_old_hash")
    session = require_role(update, context, ["student"])

    if not session or not new_password or not old_hash:
        context.user_data.pop("password_change_new", None)
        context.user_data.pop("password_change_old_hash", None)
        await update.effective_chat.send_message(
            "Password change session expired. Please start again from the student menu."
        )
        return ConversationHandler.END

    if not hmac.compare_digest(new_password, confirmation):
        context.user_data.pop("password_change_new", None)
        context.user_data.pop("password_change_old_hash", None)
        await update.effective_chat.send_message(
            "The passwords did not match. Nothing was changed. Select Change Password to try again."
        )
        return ConversationHandler.END

    try:
        new_hash = hash_password(new_password)
        with db_cursor() as cur:
            cur.execute("""
                UPDATE students
                SET password_hash=%s
                WHERE id=%s AND is_active=TRUE AND password_hash=%s
            """, (new_hash, session["id"], old_hash))
            if cur.rowcount != 1:
                raise RuntimeError("Student password changed concurrently or account is inactive.")
            cur.execute("""
                INSERT INTO audit_logs (actor_type, actor_id, action)
                VALUES (%s, %s, %s)
            """, ("student", session["id"], "Student changed their password"))
    except Exception:
        logger.exception("Student password change failed")
        context.user_data.pop("password_change_new", None)
        context.user_data.pop("password_change_old_hash", None)
        await update.effective_chat.send_message(
            "Password change failed. Your previous password should remain valid. Please try again later."
        )
        return ConversationHandler.END

    context.user_data.pop("password_change_new", None)
    context.user_data.pop("password_change_old_hash", None)
    context.user_data.pop("password_change_attempts", None)
    await update.effective_chat.send_message(
        "Your password has been changed successfully. Use your new password the next time you log in."
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Super Admin account and course management
# ---------------------------------------------------------------------------

ADMIN_VALUE = 10


async def admin_only(update, context):
    session = session_for(update, context)
    if not session or session["role"] != "admin":
        await update.effective_message.reply_text(
            "This action is restricted to the Super Admin."
        )
        return None
    return session


async def admin_add_student_start(update, context):
    if not await admin_only(update, context):
        return ConversationHandler.END
    context.user_data["admin_flow"] = "student"
    await update.message.reply_text("Enter the student's Student ID:")
    return ADMIN_VALUE


async def admin_add_instructor_start(update, context):
    if not await admin_only(update, context):
        return ConversationHandler.END
    context.user_data["admin_flow"] = "instructor"
    await update.message.reply_text("Enter the instructor's username:")
    return ADMIN_VALUE


async def admin_add_course_start(update, context):
    if not await admin_only(update, context):
        return ConversationHandler.END
    context.user_data["admin_flow"] = "course"
    await update.message.reply_text("Enter the course code:")
    return ADMIN_VALUE


async def admin_flow_step(update, context):
    flow = context.user_data.get("admin_flow")
    value = update.message.text.strip()
    data = context.user_data.setdefault("admin_data", {})

    if flow == "student":
        if "identity" not in data:
            data["identity"] = value
            await update.message.reply_text("Enter the student's full name:")
            return ADMIN_VALUE
        if "name" not in data:
            data["name"] = value
            await update.message.reply_text(
                f"Set a temporary password (at least {MIN_PASSWORD_LENGTH} characters):"
            )
            return ADMIN_VALUE
        if len(value) < MIN_PASSWORD_LENGTH:
            await update.message.reply_text("Password too short. Enter at least 8 characters:")
            return ADMIN_VALUE
        try:
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO students (student_id, full_name, password_hash)
                    VALUES (%s, %s, %s)
                """, (data["identity"], data["name"], hash_password(value)))
            audit("admin", update.effective_user.id,
                  f"Created student account {data['identity']}")
            await update.message.reply_text(
                f"Student account created for {data['identity']}."
            )
        except psycopg2.errors.UniqueViolation:
            await update.message.reply_text("That Student ID already exists.")
        except Exception:
            logger.exception("Student creation failed")
            await update.message.reply_text("Could not create the student account.")
    elif flow == "instructor":
        if "identity" not in data:
            data["identity"] = value
            await update.message.reply_text("Enter the instructor's full name:")
            return ADMIN_VALUE
        if "name" not in data:
            data["name"] = value
            await update.message.reply_text(
                f"Set a temporary password (at least {MIN_PASSWORD_LENGTH} characters):"
            )
            return ADMIN_VALUE
        if len(value) < MIN_PASSWORD_LENGTH:
            await update.message.reply_text("Password too short. Enter at least 8 characters:")
            return ADMIN_VALUE
        try:
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO instructors (username, full_name, password_hash)
                    VALUES (%s, %s, %s)
                """, (data["identity"], data["name"], hash_password(value)))
            audit("admin", update.effective_user.id,
                  f"Created instructor account {data['identity']}")
            await update.message.reply_text(
                f"Instructor account created for {data['identity']}."
            )
        except psycopg2.errors.UniqueViolation:
            await update.message.reply_text("That instructor username already exists.")
        except Exception:
            logger.exception("Instructor creation failed")
            await update.message.reply_text("Could not create the instructor account.")
    elif flow == "course":
        if "code" not in data:
            data["code"] = value
            await update.message.reply_text("Enter the course name:")
            return ADMIN_VALUE
        try:
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO courses (course_code, course_name)
                    VALUES (%s, %s)
                """, (data["code"], value))
            audit("admin", update.effective_user.id,
                  f"Created course {data['code']}")
            await update.message.reply_text(f"Course {data['code']} created.")
        except psycopg2.errors.UniqueViolation:
            await update.message.reply_text("That course code already exists.")
        except Exception:
            logger.exception("Course creation failed")
            await update.message.reply_text("Could not create the course.")
    elif flow == "assign":
        if "instructor" not in data:
            data["instructor"] = value
            await update.message.reply_text("Enter the course code:")
            return ADMIN_VALUE
        try:
            with db_cursor() as cur:
                cur.execute(
                    "SELECT id FROM instructors WHERE username=%s AND is_active=TRUE",
                    (data["instructor"],)
                )
                instructor = cur.fetchone()
                cur.execute(
                    "SELECT id FROM courses WHERE course_code=%s AND is_active=TRUE",
                    (value,)
                )
                course = cur.fetchone()
                if not instructor or not course:
                    await update.message.reply_text(
                        "Instructor username or course code not found."
                    )
                else:
                    cur.execute("""
                        INSERT INTO instructor_courses (instructor_id, course_id)
                        VALUES (%s, %s)
                        ON CONFLICT (instructor_id, course_id) DO NOTHING
                    """, (instructor["id"], course["id"]))
                    await update.message.reply_text("Course assignment saved.")
            audit("admin", update.effective_user.id,
                  f"Assigned {value} to instructor {data['instructor']}")
        except Exception:
            logger.exception("Course assignment failed")
            await update.message.reply_text("Could not assign the course.")
    else:
        await update.message.reply_text("Unknown operation. Start again.")
    context.user_data.pop("admin_flow", None)
    context.user_data.pop("admin_data", None)
    await show_menu(update, context)
    return ConversationHandler.END


async def assign_course_start(update, context):
    if not await admin_only(update, context):
        return ConversationHandler.END
    context.user_data["admin_flow"] = "assign"
    context.user_data["admin_data"] = {}
    await update.message.reply_text("Enter the instructor username:")
    return ADMIN_VALUE


async def assign_course_step(update, context):
    data = context.user_data.setdefault("admin_data", {})
    value = update.message.text.strip()
    if "instructor" not in data:
        data["instructor"] = value
        await update.message.reply_text("Enter the course code:")
        return ADMIN_VALUE
    try:
        with db_cursor() as cur:
            cur.execute("SELECT id FROM instructors WHERE username=%s AND is_active=TRUE",
                        (data["instructor"],))
            instructor = cur.fetchone()
            cur.execute("SELECT id FROM courses WHERE course_code=%s AND is_active=TRUE",
                        (value,))
            course = cur.fetchone()
            if not instructor or not course:
                await update.message.reply_text("Instructor username or course code not found.")
            else:
                cur.execute("""
                    INSERT INTO instructor_courses (instructor_id, course_id)
                    VALUES (%s, %s) ON CONFLICT (instructor_id, course_id) DO NOTHING
                """, (instructor["id"], course["id"]))
                await update.message.reply_text("Course assignment saved.")
        audit("admin", update.effective_user.id,
              f"Assigned {value} to instructor {data['instructor']}")
    except Exception:
        logger.exception("Course assignment failed")
        await update.message.reply_text("Could not assign the course.")
    context.user_data.pop("admin_flow", None)
    context.user_data.pop("admin_data", None)
    await show_menu(update, context)
    return ConversationHandler.END


async def list_courses(update, context):
    if not await admin_only(update, context):
        return
    with db_cursor() as cur:
        cur.execute("""
            SELECT course_code, course_name, semester
            FROM courses WHERE is_active=TRUE ORDER BY course_code
        """)
        rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("No active courses have been created.")
        return
    lines = [
        f"• {r['course_code']} — {r['course_name']}"
        + (f" ({r['semester']})" if r["semester"] else "")
        for r in rows
    ]
    await update.message.reply_text("Section B courses:\n\n" + "\n".join(lines))



# ---------------------------------------------------------------------------
# Bulk student registration (Super Admin only)
# ---------------------------------------------------------------------------

BULK_UPLOAD = 40
BULK_CONFIRM = 41
MAX_ROSTER_BYTES = 8 * 1024 * 1024
MAX_ROSTER_ROWS = 500


def _roster_column_indexes(headers):
    aliases = {
        "student_id": {
            "studentidno", "studentidnumber", "studentid", "studentno",
            "studentnumber", "idno", "idnumber", "studentcode"
        },
        "full_name": {"fullname", "studentname", "name"},
        "gender": {"gender", "sex"},
        "section": {"section", "classsection", "group"}
    }
    indexes = {}
    for index, heading in enumerate(headers):
        normalized = _normalize_grade_header(heading)
        for field, accepted in aliases.items():
            if normalized in accepted and field not in indexes:
                indexes[field] = index
    if "student_id" not in indexes or "full_name" not in indexes:
        raise ValueError(
            "Required columns not found. Include 'STUDENT ID NO' and 'FULL NAME'."
        )
    return indexes


def parse_student_roster_file(filename, raw_bytes):
    """Parse a Section B roster. Gender and other extra columns are optional."""
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    rows = []

    def append_rows(headers, data_rows, first_line=2):
        indexes = _roster_column_indexes(headers)
        for line_number, values in enumerate(data_rows, start=first_line):
            if not any(_cell_text(value) for value in values):
                continue
            def get_value(field):
                index = indexes.get(field)
                return _cell_text(values[index]) if index is not None and index < len(values) else ""
            rows.append({
                "line": line_number,
                "student_id": get_value("student_id"),
                "full_name": get_value("full_name"),
                "gender": get_value("gender"),
                "section": get_value("section")
            })
            if len(rows) > MAX_ROSTER_ROWS:
                raise ValueError(f"Roster exceeds the {MAX_ROSTER_ROWS}-student limit.")

    if extension == "csv":
        decoded = None
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                decoded = raw_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            raise ValueError("CSV encoding is unsupported. Save as UTF-8 CSV.")
        reader = csv.reader(io.StringIO(decoded))
        headers = next(reader, None)
        if not headers:
            raise ValueError("CSV file is empty.")
        append_rows(headers, reader)
    elif extension in ("xlsx", "xlsm"):
        if load_workbook is None:
            raise ValueError("Excel support is unavailable. Install openpyxl.")
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        try:
            iterator = workbook.active.iter_rows(values_only=True)
            headers = next(iterator, None)
            if not headers:
                raise ValueError("Excel file is empty.")
            append_rows(headers, iterator)
        finally:
            workbook.close()
    elif extension == "xls":
        if xlrd is None:
            raise ValueError("Legacy .xls support is unavailable. Install xlrd.")
        try:
            workbook = xlrd.open_workbook(file_contents=raw_bytes)
            sheet = workbook.sheet_by_index(0)
        except Exception as exc:
            raise ValueError("Could not read this .xls workbook.") from exc
        if sheet.nrows < 1:
            raise ValueError("Excel file is empty.")
        append_rows(sheet.row_values(0),
                    (sheet.row_values(i) for i in range(1, sheet.nrows)))
    else:
        raise ValueError("Upload a .csv, .xls, .xlsx or .xlsm roster.")

    if not rows:
        raise ValueError("No student records were found below the header row.")
    return rows


async def bulk_add_students_start(update, context):
    if not await admin_only(update, context):
        return ConversationHandler.END
    context.user_data.pop("bulk_roster", None)
    context.user_data.pop("bulk_preview", None)
    await update.message.reply_text(
        "Upload the Section B student roster as an Excel or CSV document.\n\n"
        "Required columns: STUDENT ID NO and FULL NAME.\n"
        "Optional columns: GENDER and SECTION. Extra columns such as No are ignored.\n"
        "Only Section B records are accepted. Maximum file size: 8 MB; maximum 500 students.\n\n"
        "No accounts will be created until you confirm the preview. Send /cancel to stop."
    )
    return BULK_UPLOAD


async def receive_student_roster(update, context):
    if not await admin_only(update, context):
        return ConversationHandler.END
    document = update.message.document
    if not document:
        await update.message.reply_text("Please upload the roster as a document.")
        return BULK_UPLOAD
    if document.file_size and document.file_size > MAX_ROSTER_BYTES:
        await update.message.reply_text("File too large. Maximum supported size is 8 MB.")
        return BULK_UPLOAD

    filename = document.file_name or "roster.xlsx"
    try:
        tg_file = await context.bot.get_file(document.file_id)
        raw_bytes = bytes(await tg_file.download_as_bytearray())
        parsed = parse_student_roster_file(filename, raw_bytes)

        seen = set()
        valid = []
        errors = []
        for row in parsed:
            sid = row["student_id"].strip()
            name = row["full_name"].strip()
            section = row["section"].strip()
            if not sid:
                errors.append(f"Row {row['line']}: missing Student ID.")
                continue
            if not name:
                errors.append(f"Row {row['line']}: missing full name ({sid}).")
                continue
            if section and section.casefold() not in {"b", "section b"}:
                errors.append(f"Row {row['line']}: section '{section}' is not Section B.")
                continue
            if sid in seen:
                errors.append(f"Row {row['line']}: duplicate Student ID inside this file ({sid}).")
                continue
            seen.add(sid)
            valid.append({
                "student_id": sid,
                "full_name": name,
                "gender": row["gender"],
                "section": section or "B",
                "line": row["line"]
            })

        if errors:
            details = "\n".join(errors[:20])
            if len(errors) > 20:
                details += f"\n…and {len(errors) - 20} more."
            await update.message.reply_text(
                "Roster rejected. Correct these issues and upload again:\n\n" + details
            )
            return BULK_UPLOAD

        ids = [row["student_id"] for row in valid]
        with db_cursor() as cur:
            cur.execute("SELECT student_id FROM students WHERE student_id = ANY(%s)", (ids,))
            existing = {row["student_id"] for row in cur.fetchall()}

        new_students = [row for row in valid if row["student_id"] not in existing]
        already_registered = len(valid) - len(new_students)
        if not new_students:
            await update.message.reply_text(
                f"All {already_registered} valid records already exist. No new accounts are needed."
            )
            return ConversationHandler.END

        context.user_data["bulk_roster"] = new_students
        context.user_data["bulk_file_name"] = filename
        context.user_data["bulk_preview"] = {
            "total": len(valid),
            "existing": already_registered,
            "new": len(new_students)
        }
        preview_lines = [
            f"• {row['student_id']} — {row['full_name']}"
            for row in new_students[:12]
        ]
        if len(new_students) > 12:
            preview_lines.append(f"…and {len(new_students) - 12} more new students.")
        summary = (
            "ROSTER VALIDATION COMPLETE\n\n"
            f"Total valid records: {len(valid)}\n"
            f"Already registered (will be skipped): {already_registered}\n"
            f"New accounts to create: {len(new_students)}\n\n"
            "New students:\n" + "\n".join(preview_lines) +
            "\n\nEach new student will receive a unique temporary password. "
            "A separate Excel credentials file will be sent to you after registration.\n\n"
            "Create these accounts?"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm registration", callback_data="bulkconfirm:yes"),
            InlineKeyboardButton("Cancel", callback_data="bulkconfirm:no")
        ]])
        await update.message.reply_text(summary, reply_markup=keyboard)
        return BULK_CONFIRM
    except ValueError as exc:
        await update.message.reply_text(f"Could not process roster: {exc}")
        return BULK_UPLOAD
    except Exception:
        logger.exception("Bulk roster validation failed")
        await update.message.reply_text("Roster validation failed. No accounts were created.")
        return BULK_UPLOAD


async def bulk_registration_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await admin_only(update, context):
        await query.edit_message_text("This action is restricted to the Super Admin.")
        return ConversationHandler.END

    if query.data == "bulkconfirm:no":
        context.user_data.pop("bulk_roster", None)
        context.user_data.pop("bulk_preview", None)
        context.user_data.pop("bulk_file_name", None)
        await query.edit_message_text("Bulk registration cancelled. No accounts were created.")
        return ConversationHandler.END

    roster = context.user_data.get("bulk_roster")
    if not roster:
        await query.edit_message_text("The roster session expired. Upload the file again.")
        return ConversationHandler.END

    # Generate credentials and workbook before committing, so the workbook is ready
    # if the database transaction succeeds.
    credentials = []
    workbook_buffer = io.BytesIO()
    try:
        from openpyxl import Workbook
        for row in roster:
            temporary_password = secrets.token_urlsafe(12)
            credentials.append({
                **row,
                "temporary_password": temporary_password,
                "password_hash": hash_password(temporary_password)
            })

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Student Credentials"
        sheet.append(["STUDENT ID NO", "FULL NAME", "TEMPORARY PASSWORD", "SECTION"])
        for item in credentials:
            sheet.append([
                item["student_id"], item["full_name"],
                item["temporary_password"], item["section"]
            ])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        workbook.save(workbook_buffer)
        workbook.close()
        workbook_buffer.seek(0)

        inserted = 0
        with db_cursor() as cur:
            for item in credentials:
                cur.execute("""
                    INSERT INTO students (student_id, full_name, password_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (student_id) DO NOTHING
                    RETURNING id
                """, (item["student_id"], item["full_name"], item["password_hash"]))
                if cur.fetchone():
                    inserted += 1
            cur.execute("""
                INSERT INTO audit_logs (actor_type, actor_id, action)
                VALUES (%s, %s, %s)
            """, (
                "admin", update.effective_user.id,
                f"Bulk student registration: {inserted} accounts created; "
                f"{len(roster) - inserted} skipped due to concurrent/existing records."
            ))

        # Credentials are sent only for accounts actually inserted.
        # In a rare concurrent-registration race, discard skipped passwords.
        inserted_ids = {item["student_id"] for item in credentials}
        if inserted != len(credentials):
            # Re-query the IDs is not sufficient to identify which insert raced;
            # use a second safe query and include only accounts whose stored hash
            # matches this batch's generated hash.
            with db_cursor() as cur:
                cur.execute(
                    "SELECT student_id, password_hash FROM students WHERE student_id = ANY(%s)",
                    ([item["student_id"] for item in credentials],)
                )
                stored = {r["student_id"]: r["password_hash"] for r in cur.fetchall()}
            credentials = [
                item for item in credentials
                if stored.get(item["student_id"]) == item["password_hash"]
            ]
            workbook_buffer = io.BytesIO()
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Student Credentials"
            sheet.append(["STUDENT ID NO", "FULL NAME", "TEMPORARY PASSWORD", "SECTION"])
            for item in credentials:
                sheet.append([
                    item["student_id"], item["full_name"],
                    item["temporary_password"], item["section"]
                ])
            workbook.save(workbook_buffer)
            workbook.close()
            workbook_buffer.seek(0)

        context.user_data.pop("bulk_roster", None)
        context.user_data.pop("bulk_preview", None)
        context.user_data.pop("bulk_file_name", None)

        await query.edit_message_text(
            f"Registration completed: {inserted} new student accounts created.\n"
            f"{len(roster) - inserted} records were skipped because they already existed.\n\n"
            "I will now send the credentials workbook. Distribute each password privately "
            "and delete the workbook after secure delivery."
        )
        if inserted:
            workbook_buffer.seek(0)
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(
                    workbook_buffer,
                    filename="Section_B_Student_Credentials.xlsx"
                ),
                caption=(
                    "Confidential student credentials. Send each student only their own "
                    "password. Do not share this file in the group."
                )
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="No new accounts were created; no credentials file is needed."
            )
        return ConversationHandler.END
    except Exception:
        logger.exception("Bulk student registration failed")
        await query.edit_message_text(
            "Bulk registration could not be completed. Check the database/logs before retrying."
        )
        return ConversationHandler.END
    finally:
        workbook_buffer.close()


# ---------------------------------------------------------------------------
# Reset all student credentials (Super Admin only)
# ---------------------------------------------------------------------------

RESET_CREDENTIALS_CONFIRM = 42


async def reset_all_credentials_start(update, context):
    """Ask for explicit confirmation before invalidating every student password."""
    if not await admin_only(update, context):
        return ConversationHandler.END

    try:
        with db_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM students")
            total = int(cur.fetchone()["total"])
    except Exception:
        logger.exception("Could not count student accounts before credential reset")
        await update.message.reply_text("Could not access student accounts. No passwords were changed.")
        return ConversationHandler.END

    if total == 0:
        await update.message.reply_text("There are no student accounts to reset.")
        return ConversationHandler.END

    context.user_data["reset_credentials_expected_count"] = total
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("YES — Reset all passwords", callback_data="resetcreds:yes"),
        InlineKeyboardButton("Cancel", callback_data="resetcreds:no")
    ]])
    await update.message.reply_text(
        "⚠️ CONFIRM FULL STUDENT CREDENTIAL RESET\n\n"
        f"Student accounts found: {total}\n\n"
        "If you continue:\n"
        "• Every existing student's password will be replaced.\n"
        "• All old passwords will stop working.\n"
        "• Student accounts, names, IDs, Telegram links and grades will remain unchanged.\n"
        "• A new confidential Excel file with the new passwords will be generated.\n\n"
        "This action cannot be undone. Continue?",
        reply_markup=keyboard
    )
    return RESET_CREDENTIALS_CONFIRM


async def reset_all_credentials_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not await admin_only(update, context):
        await query.edit_message_text("This action is restricted to the Super Admin.")
        return ConversationHandler.END

    if query.data == "resetcreds:no":
        context.user_data.pop("reset_credentials_expected_count", None)
        await query.edit_message_text("Credential reset cancelled. No passwords were changed.")
        return ConversationHandler.END

    expected_count = context.user_data.pop("reset_credentials_expected_count", None)
    credentials = []
    workbook_buffer = io.BytesIO()
    committed = False

    try:
        from openpyxl import Workbook

        # Include active and inactive accounts; do not modify account status or grades.
        with db_cursor() as cur:
            cur.execute("""
                SELECT id, student_id, full_name, is_active
                FROM students
                ORDER BY student_id
            """)
            students = cur.fetchall()

        if not students:
            await query.edit_message_text("No student accounts were found. Nothing was changed.")
            return ConversationHandler.END

        if expected_count is not None and len(students) != expected_count:
            await query.edit_message_text(
                "The number of student accounts changed after confirmation. "
                "For safety, no passwords were changed. Please start again."
            )
            return ConversationHandler.END

        for student in students:
            temporary_password = secrets.token_urlsafe(12)
            credentials.append({
                "id": student["id"],
                "student_id": student["student_id"],
                "full_name": student["full_name"],
                "is_active": student["is_active"],
                "temporary_password": temporary_password,
                "password_hash": hash_password(temporary_password)
            })

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "New Student Credentials"
        sheet.append(["STUDENT ID NO", "FULL NAME", "NEW TEMPORARY PASSWORD", "ACCOUNT STATUS"])
        for item in credentials:
            sheet.append([
                item["student_id"], item["full_name"], item["temporary_password"],
                "Active" if item["is_active"] else "Inactive"
            ])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column, width in {"A": 20, "B": 34, "C": 30, "D": 16}.items():
            sheet.column_dimensions[column].width = width
        workbook.save(workbook_buffer)
        workbook.close()
        workbook_buffer.seek(0)

        # Update all password hashes and write the audit event atomically.
        with db_cursor() as cur:
            for item in credentials:
                cur.execute(
                    "UPDATE students SET password_hash=%s WHERE id=%s",
                    (item["password_hash"], item["id"])
                )
                if cur.rowcount != 1:
                    raise RuntimeError("A student account changed during the reset.")
            cur.execute("""
                INSERT INTO audit_logs (actor_type, actor_id, action)
                VALUES (%s, %s, %s)
            """, (
                "admin", update.effective_user.id,
                f"Reset credentials for all {len(credentials)} student accounts."
            ))
        committed = True

        await query.edit_message_text(
            f"Credential reset completed for {len(credentials)} student accounts."
            + chr(10) * 2
            + "All previous passwords are now invalid. The new credentials workbook follows."
        )
        workbook_buffer.seek(0)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(
                workbook_buffer,
                filename="Section_B_NEW_Student_Credentials.xlsx"
            ),
            caption=(
                "CONFIDENTIAL: New credentials for all registered student accounts. "
                "Send each student only their own password. Do not post this workbook "
                "in a group or public channel. Store it securely and delete it after distribution."
            )
        )
        return ConversationHandler.END

    except Exception:
        logger.exception("Student credential reset operation failed")
        if committed:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"IMPORTANT: Passwords were reset for {len(credentials)} student accounts, "
                    "but the Excel file could not be delivered. Old passwords no longer work. "
                    "Do not repeat the reset blindly; contact the administrator for recovery."
                )
            )
        else:
            await query.edit_message_text(
                "Credential reset failed before the database transaction was committed. "
                "No passwords were changed. Check the server logs and try again."
            )
        return ConversationHandler.END
    finally:
        workbook_buffer.close()


# ---------------------------------------------------------------------------
# Instructor course and assessment workflows
# ---------------------------------------------------------------------------

ASSESSMENT_FLOW = 20
UPLOAD_FLOW = 21
UPLOAD_CONFIRM = 24


def instructor_courses(instructor_id):
    with db_cursor() as cur:
        cur.execute("""
            SELECT c.id, c.course_code, c.course_name
            FROM courses c
            JOIN instructor_courses ic ON ic.course_id=c.id
            WHERE ic.instructor_id=%s AND c.is_active=TRUE
            ORDER BY c.course_code
        """, (instructor_id,))
        return cur.fetchall()


async def my_courses(update, context):
    session = require_role(update, context, ["instructor", "student"])
    if not session:
        await update.message.reply_text("Please /login first.")
        return
    if session["role"] == "instructor":
        rows = instructor_courses(session["id"])
    else:
        with db_cursor() as cur:
            cur.execute("""
                SELECT DISTINCT c.id, c.course_code, c.course_name
                FROM courses c
                JOIN assessments a ON a.course_id=c.id
                JOIN results r ON r.assessment_id=a.id
                JOIN students s ON s.id=r.student_id
                WHERE s.id=%s AND r.published=TRUE
                ORDER BY c.course_code
            """, (session["id"],))
            rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("No courses are currently available.")
        return
    lines = [f"• {r['course_code']} — {r['course_name']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def create_assessment_start(update, context):
    session = require_role(update, context, ["instructor"])
    if not session:
        await update.message.reply_text("Only instructors can create assessments.")
        return ConversationHandler.END
    courses = instructor_courses(session["id"])
    if not courses:
        await update.message.reply_text("You have no assigned courses. Contact the Super Admin.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(
        f"{c['course_code']} — {c['course_name']}",
        callback_data=f"assesscourse:{c['id']}"
    )] for c in courses]
    await update.message.reply_text(
        "Select the course for this assessment:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ASSESSMENT_FLOW


async def assessment_course_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return ConversationHandler.END
    course_id = int(query.data.split(":")[1])
    allowed = {c["id"] for c in instructor_courses(session["id"])}
    if course_id not in allowed:
        await query.edit_message_text("You are not assigned to that course.")
        return ConversationHandler.END
    context.user_data["assessment_course_id"] = course_id
    await query.edit_message_text("Enter the assessment name (e.g. Quiz 1, Mid Exam):")
    return ASSESSMENT_FLOW + 1


async def assessment_name_step(update, context):
    name = update.message.text.strip()
    if not name or len(name) > 100:
        await update.message.reply_text("Enter a valid assessment name (1–100 characters):")
        return ASSESSMENT_FLOW + 1
    context.user_data["assessment_name"] = name
    await update.message.reply_text("Enter the maximum score (for example, 10 or 30):")
    return ASSESSMENT_FLOW + 2


async def assessment_max_step(update, context):
    try:
        maximum = Decimal(update.message.text.strip())
        if maximum <= 0 or maximum > Decimal("100000"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Enter a valid positive maximum score:")
        return ASSESSMENT_FLOW + 2
    session = require_role(update, context, ["instructor"])
    course_id = context.user_data.get("assessment_course_id")
    allowed = {c["id"] for c in instructor_courses(session["id"])} if session else set()
    if course_id not in allowed:
        await update.message.reply_text("Course authorization failed. Start again.")
        return ConversationHandler.END
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO assessments (course_id, assessment_name, max_score)
                VALUES (%s, %s, %s) RETURNING id
            """, (course_id, context.user_data["assessment_name"], maximum))
            assessment_id = cur.fetchone()["id"]
        audit("instructor", session["id"],
              f"Created assessment {context.user_data['assessment_name']} (ID {assessment_id})")
        await update.message.reply_text(
            f"Assessment created successfully.\nAssessment ID: {assessment_id}\n"
            "It is not visible to students until marks are uploaded and published."
        )
    except Exception:
        logger.exception("Assessment creation failed")
        await update.message.reply_text("Could not create the assessment.")
    for key in ("assessment_course_id", "assessment_name"):
        context.user_data.pop(key, None)
    await show_menu(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Grade file parsing and validation
# ---------------------------------------------------------------------------

def _normalize_grade_header(value):
    """Normalize a spreadsheet heading so punctuation/case do not matter."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _grade_column_indexes(headers):
    """Map recognized university and generic headings to column indexes."""
    aliases = {
        "student_id": {
            "studentidno", "studentidnumber", "studentid", "studentno",
            "studentnumber", "idno", "idnumber", "studentcode"
        },
        "full_name": {"fullname", "studentname", "name"},
        "gender": {"gender", "sex"},
        "section": {"section", "classsection", "group"},
        "score": {"score", "mark", "marks", "grade", "result", "total"}
    }
    indexes = {}
    for index, heading in enumerate(headers):
        normalized = _normalize_grade_header(heading)
        if not normalized:
            continue
        for field, accepted in aliases.items():
            if normalized in accepted and field not in indexes:
                indexes[field] = index
    if "student_id" not in indexes or "score" not in indexes:
        raise ValueError(
            "Could not identify the required columns. Include headings such as "
            "'STUDENT ID NO' and 'Score' (or 'student_id' and 'score')."
        )
    return indexes


def _cell_text(value):
    if value is None:
        return ""
    # Excel may represent an integer-valued numeric ID as 6881.0. Do not
    # silently rewrite identifiers with leading zeros or embedded separators.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_grade_file(filename, raw_bytes):
    """Parse the original Section B grade sheet or a simplified grade file.

    Required columns: Student ID and Score.
    Recognized optional columns: Full Name, Gender and Section.
    Extra columns (for example No.) are ignored. Completely empty rows are
    skipped; a row with an ID but no score is retained for validation and
    rejected rather than silently treated as zero.
    """
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    rows = []

    if extension == "csv":
        text_data = None
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                text_data = raw_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text_data is None:
            raise ValueError("CSV encoding is not supported. Save it as UTF-8 CSV.")

        reader = csv.reader(io.StringIO(text_data))
        headers = next(reader, None)
        if not headers:
            raise ValueError("CSV file is empty or has no header row.")
        indexes = _grade_column_indexes(headers)
        for line_number, values in enumerate(reader, start=2):
            if not any(_cell_text(value) for value in values):
                continue
            def get_value(field):
                index = indexes.get(field)
                return _cell_text(values[index]) if index is not None and index < len(values) else ""
            rows.append({
                "line": line_number,
                "student_id": get_value("student_id"),
                "full_name": get_value("full_name"),
                "gender": get_value("gender"),
                "section": get_value("section"),
                "score": get_value("score")
            })

    elif extension in ("xlsx", "xlsm"):
        if load_workbook is None:
            raise ValueError("Excel support is not installed. Add openpyxl to requirements.txt.")
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        try:
            sheet = workbook.active
            iterator = sheet.iter_rows(values_only=True)
            headers = next(iterator, None)
            if not headers:
                raise ValueError("Excel file is empty.")
            indexes = _grade_column_indexes(headers)
            for line_number, values in enumerate(iterator, start=2):
                if not any(_cell_text(value) for value in values):
                    continue
                def get_value(field):
                    index = indexes.get(field)
                    return _cell_text(values[index]) if index is not None and index < len(values) else ""
                rows.append({
                    "line": line_number,
                    "student_id": get_value("student_id"),
                    "full_name": get_value("full_name"),
                    "gender": get_value("gender"),
                    "section": get_value("section"),
                    "score": get_value("score")
                })
        finally:
            workbook.close()

    elif extension == "xls":
        if xlrd is None:
            raise ValueError("Legacy Excel .xls support is not installed. Add xlrd to requirements.txt.")
        try:
            workbook = xlrd.open_workbook(file_contents=raw_bytes)
            sheet = workbook.sheet_by_index(0)
        except Exception as exc:
            raise ValueError("Could not read this .xls file. Make sure it is a valid Excel 97-2003 workbook.") from exc
        if sheet.nrows < 1:
            raise ValueError("Excel file is empty.")
        headers = sheet.row_values(0)
        indexes = _grade_column_indexes(headers)
        for row_index in range(1, sheet.nrows):
            values = sheet.row_values(row_index)
            if not any(_cell_text(value) for value in values):
                continue
            def get_value(field):
                index = indexes.get(field)
                return _cell_text(values[index]) if index is not None and index < len(values) else ""
            rows.append({
                "line": row_index + 1,
                "student_id": get_value("student_id"),
                "full_name": get_value("full_name"),
                "gender": get_value("gender"),
                "section": get_value("section"),
                "score": get_value("score")
            })
    else:
        raise ValueError("Upload a .csv, .xls, .xlsx or .xlsm file.")

    if not rows:
        raise ValueError("No grade rows were found below the header row.")
    return rows


async def upload_grades_start(update, context):
    context.user_data.pop("edit_published_mode", None)
    context.user_data.pop("pending_published_edits", None)
    session = require_role(update, context, ["instructor"])
    if not session:
        await update.message.reply_text("Only instructors can upload grades.")
        return ConversationHandler.END
    courses = instructor_courses(session["id"])
    if not courses:
        await update.message.reply_text("You have no assigned courses.")
        return ConversationHandler.END

    buttons = []
    for course in courses:
        buttons.append([InlineKeyboardButton(
            f"{course['course_code']} — {course['course_name']}",
            callback_data=f"uploadcourse:{course['id']}"
        )])
    await update.message.reply_text(
        "Select the course whose marks you want to upload:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return UPLOAD_FLOW


async def upload_course_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return ConversationHandler.END
    course_id = int(query.data.split(":")[1])
    allowed = {c["id"] for c in instructor_courses(session["id"])}
    if course_id not in allowed:
        await query.edit_message_text("You are not assigned to that course.")
        return ConversationHandler.END
    with db_cursor() as cur:
        cur.execute("""
            SELECT id, assessment_name, max_score
            FROM assessments WHERE course_id=%s ORDER BY created_at DESC
        """, (course_id,))
        assessments = cur.fetchall()
    if not assessments:
        await query.edit_message_text(
            "No assessments exist for this course. Create an assessment first."
        )
        return ConversationHandler.END
    context.user_data["upload_course_id"] = course_id
    buttons = [[InlineKeyboardButton(
        f"{a['assessment_name']} (max {a['max_score']})",
        callback_data=f"uploadassessment:{a['id']}"
    )] for a in assessments]
    await query.edit_message_text(
        "Select the assessment to receive the uploaded scores:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return UPLOAD_FLOW + 1


async def upload_assessment_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return ConversationHandler.END
    assessment_id = int(query.data.split(":")[1])
    course_id = context.user_data.get("upload_course_id")
    allowed = {c["id"] for c in instructor_courses(session["id"])} if course_id else set()
    with db_cursor() as cur:
        cur.execute("""
            SELECT id, assessment_name, max_score
            FROM assessments WHERE id=%s AND course_id=%s
        """, (assessment_id, course_id))
        assessment = cur.fetchone()
    if not assessment or course_id not in allowed:
        await query.edit_message_text("Assessment authorization failed.")
        return ConversationHandler.END
    context.user_data["upload_assessment_id"] = assessment_id
    context.user_data["upload_max_score"] = str(assessment["max_score"])
    await query.edit_message_text(
        f"Selected: {assessment['assessment_name']}\n"
        f"Maximum score: {assessment['max_score']}\n\n"
        "Now upload the original university grade sheet as CSV, XLS or XLSX.\n\n"
        "Accepted headings include:\n"
        "STUDENT ID NO | FULL NAME | GENDER | SECTION | Score\n\n"
        "You may also use the simpler headings student_id and score.\n"
        "Student ID and Score are required; extra columns are allowed."
    )
    return UPLOAD_FLOW + 2


async def edit_published_grades_start(update, context):
    session = require_role(update, context, ["instructor"])
    if not session:
        await update.message.reply_text("Only instructors can edit published grades.")
        return ConversationHandler.END
    courses = instructor_courses(session["id"])
    if not courses:
        await update.message.reply_text("You have no assigned courses.")
        return ConversationHandler.END
    context.user_data["edit_published_mode"] = True
    buttons = [[InlineKeyboardButton(
        f"{c['course_code']} — {c['course_name']}",
        callback_data=f"editpubcourse:{c['id']}"
    )] for c in courses]
    await update.message.reply_text(
        "Select the course containing the published grades you want to correct:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return UPLOAD_FLOW


async def edit_published_course_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return ConversationHandler.END
    course_id = int(query.data.split(":")[1])
    allowed = {c["id"] for c in instructor_courses(session["id"])}
    if course_id not in allowed:
        await query.edit_message_text("You are not assigned to that course.")
        return ConversationHandler.END
    with db_cursor() as cur:
        cur.execute("""
            SELECT a.id, a.assessment_name, a.max_score, COUNT(r.id) AS published_count
            FROM assessments a JOIN results r ON r.assessment_id=a.id
            WHERE a.course_id=%s AND r.published=TRUE
            GROUP BY a.id ORDER BY a.created_at DESC
        """, (course_id,))
        assessments = cur.fetchall()
    if not assessments:
        await query.edit_message_text("There are no published assessments to edit in this course.")
        return ConversationHandler.END
    context.user_data["upload_course_id"] = course_id
    buttons = [[InlineKeyboardButton(
        f"{a['assessment_name']} ({a['published_count']} published marks)",
        callback_data=f"editpubassessment:{a['id']}"
    )] for a in assessments]
    await query.edit_message_text(
        "Choose the published assessment to correct:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return UPLOAD_FLOW + 1


async def edit_published_assessment_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return ConversationHandler.END
    assessment_id = int(query.data.split(":")[1])
    course_id = context.user_data.get("upload_course_id")
    allowed = {c["id"] for c in instructor_courses(session["id"])}
    if course_id not in allowed:
        await query.edit_message_text("You are not assigned to that course.")
        return ConversationHandler.END
    with db_cursor() as cur:
        cur.execute("""
            SELECT a.id, a.assessment_name, a.max_score
            FROM assessments a
            WHERE a.id=%s AND a.course_id=%s
              AND EXISTS (SELECT 1 FROM results r
                          WHERE r.assessment_id=a.id AND r.published=TRUE)
        """, (assessment_id, course_id))
        assessment = cur.fetchone()
    if not assessment:
        await query.edit_message_text("This assessment is not published or does not belong to your course.")
        return ConversationHandler.END
    context.user_data["upload_assessment_id"] = assessment_id
    context.user_data["upload_max_score"] = str(assessment["max_score"])
    await query.edit_message_text(
        f"Editing published assessment: {assessment['assessment_name']}\n"
        f"Maximum score: {assessment['max_score']}\n\n"
        "Upload the corrected CSV, XLS, XLSX or XLSM grade sheet.\n"
        "Only student IDs included in the uploaded file will be changed."
    )
    return UPLOAD_FLOW + 2


async def confirm_published_grade_edits(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return ConversationHandler.END
    if query.data == "editpub:no":
        context.user_data.pop("pending_published_edits", None)
        context.user_data.pop("edit_published_mode", None)
        await query.edit_message_text("Correction cancelled. Published grades remain unchanged.")
        return ConversationHandler.END
    pending = context.user_data.get("pending_published_edits")
    if not pending:
        await query.edit_message_text("No pending correction was found. Please start again.")
        return ConversationHandler.END
    assessment_id = pending["assessment_id"]
    course_id = pending["course_id"]
    allowed = {c["id"] for c in instructor_courses(session["id"])}
    if course_id not in allowed:
        await query.edit_message_text("Authorization failed. No grades were changed.")
        return ConversationHandler.END
    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT id FROM assessments
                WHERE id=%s AND course_id=%s
                  AND EXISTS (SELECT 1 FROM results
                              WHERE assessment_id=%s AND published=TRUE)
                FOR UPDATE
            """, (assessment_id, course_id, assessment_id))
            if not cur.fetchone():
                await query.edit_message_text("Assessment is no longer published. No grades were changed.")
                return ConversationHandler.END
            for student_db_id, score, sid in pending["clean"]:
                cur.execute("""
                    UPDATE results SET score=%s, published=FALSE, uploaded_at=NOW()
                    WHERE student_id=%s AND assessment_id=%s AND published=TRUE
                """, (score, student_db_id, assessment_id))
        audit("instructor", session["id"],
              f"Corrected {len(pending['clean'])} published marks for assessment {assessment_id}; reset to unpublished")
        context.user_data.pop("pending_published_edits", None)
        context.user_data.pop("edit_published_mode", None)
        await query.edit_message_text(
            f"{len(pending['clean'])} marks corrected successfully.\n"
            "The corrected marks are now UNPUBLISHED and hidden from students. "
            "Review them, then use Publish grades to make them visible again."
        )
    except Exception:
        logger.exception("Published grade correction failed")
        await query.edit_message_text("Correction failed. No partial changes should have been committed. Please try again.")
    return ConversationHandler.END


async def receive_grade_file(update, context):
    session = require_role(update, context, ["instructor"])
    assessment_id = context.user_data.get("upload_assessment_id")
    if not session or not assessment_id:
        await update.message.reply_text("Start the upload process from the instructor menu.")
        return ConversationHandler.END

    document = update.message.document
    if not document:
        await update.message.reply_text("Please send the grade sheet as a document file.")
        return UPLOAD_FLOW + 2
    if document.file_size and document.file_size > 8 * 1024 * 1024:
        await update.message.reply_text("File is too large. Maximum supported size is 8 MB.")
        return UPLOAD_FLOW + 2

    filename = document.file_name or "grades.csv"
    try:
        tg_file = await context.bot.get_file(document.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        parsed = parse_grade_file(filename, data)
        maximum = Decimal(context.user_data["upload_max_score"])
        seen = set()
        clean = []
        errors = []

        with db_cursor() as cur:
            for row in parsed:
                sid = row["student_id"]
                if not sid:
                    errors.append(f"Row {row['line']}: missing Student ID")
                    continue
                section = (row.get("section") or "").strip()
                if section and section.casefold() not in {"b", "section b"}:
                    errors.append(
                        f"Row {row['line']}: section is '{section}', but this bot accepts Section B only"
                    )
                    continue
                if sid in seen:
                    errors.append(f"Row {row['line']}: duplicate Student ID {sid}")
                    continue
                seen.add(sid)
                try:
                    score = Decimal(row["score"])
                    if not score.is_finite() or score < 0 or score > maximum:
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    errors.append(
                        f"Row {row['line']}: missing or invalid score for {sid}; "
                        f"must be between 0 and {maximum}"
                    )
                    continue
                cur.execute(
                    "SELECT id FROM students WHERE student_id=%s AND is_active=TRUE",
                    (sid,)
                )
                student = cur.fetchone()
                if not student:
                    errors.append(f"Row {row['line']}: unknown/inactive Student ID {sid}")
                    continue
                if context.user_data.get("edit_published_mode"):
                    cur.execute("""
                        SELECT id FROM results
                        WHERE student_id=%s AND assessment_id=%s AND published=TRUE
                    """, (student["id"], assessment_id))
                    if not cur.fetchone():
                        errors.append(
                            f"Row {row['line']}: {sid} has no currently published mark for this assessment"
                        )
                        continue
                clean.append((student["id"], score, sid))

        if errors:
            preview = "\n".join(errors[:20])
            extra = f"\n…and {len(errors)-20} more errors." if len(errors) > 20 else ""
            await update.message.reply_text(
                "Upload rejected. No marks were saved because validation found errors:\n\n"
                + preview + extra
            )
            return ConversationHandler.END
        if not clean:
            await update.message.reply_text("No valid marks found. Nothing was saved.")
            return ConversationHandler.END

        if context.user_data.get("edit_published_mode"):
            context.user_data["pending_published_edits"] = {
                "assessment_id": assessment_id,
                "course_id": context.user_data.get("upload_course_id"),
                "clean": clean,
            }
            preview = "\n".join(f"{sid}: {score}" for _, score, sid in clean[:15])
            more = f"\n…and {len(clean)-15} more." if len(clean) > 15 else ""
            await update.message.reply_text(
                f"Correction preview: {len(clean)} student marks\n\n{preview}{more}\n\n"
                "Confirm to replace these published marks. After confirmation, the corrected "
                "marks will be hidden until you publish them again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Confirm corrections", callback_data="editpub:yes"),
                    InlineKeyboardButton("Cancel", callback_data="editpub:no")
                ]])
            )
            return UPLOAD_CONFIRM

        with db_cursor() as cur:
            for student_db_id, score, sid in clean:
                cur.execute("""
                    INSERT INTO results (student_id, assessment_id, score, published)
                    VALUES (%s, %s, %s, FALSE)
                    ON CONFLICT (student_id, assessment_id)
                    DO UPDATE SET score=EXCLUDED.score,
                                  published=FALSE,
                                  uploaded_at=NOW()
                """, (student_db_id, assessment_id, score))
        audit("instructor", session["id"],
              f"Uploaded {len(clean)} marks for assessment {assessment_id}")
        await update.message.reply_text(
            f"Upload successful: {len(clean)} student marks saved.\n"
            "The marks are currently UNPUBLISHED and students cannot see them.\n"
            "Use Publish grades when you have verified the marks."
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
    except Exception:
        logger.exception("Grade upload failed")
        await update.message.reply_text(
            "The upload failed. No partial upload should have been committed. "
            "Check the file and try again."
        )
    finally:
        for key in ("upload_course_id", "upload_assessment_id", "upload_max_score"):
            context.user_data.pop(key, None)
    await show_menu(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Publishing and student grade viewing
# ---------------------------------------------------------------------------

async def publish_grades_start(update, context):
    session = require_role(update, context, ["instructor"])
    if not session:
        await update.message.reply_text("Only instructors can publish grades.")
        return
    courses = instructor_courses(session["id"])
    buttons = []
    for course in courses:
        buttons.append([InlineKeyboardButton(
            f"{course['course_code']} — {course['course_name']}",
            callback_data=f"pubcourse:{course['id']}"
        )])
    if not buttons:
        await update.message.reply_text("You have no assigned courses.")
        return
    await update.message.reply_text(
        "Select a course to publish its uploaded assessments:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def publish_course_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return
    course_id = int(query.data.split(":")[1])
    allowed = {c["id"] for c in instructor_courses(session["id"])}
    if course_id not in allowed:
        await query.edit_message_text("You are not assigned to that course.")
        return
    with db_cursor() as cur:
        cur.execute("""
            SELECT a.id, a.assessment_name,
                   COUNT(r.id) AS total,
                   COUNT(r.id) FILTER (WHERE r.published=FALSE) AS unpublished
            FROM assessments a
            LEFT JOIN results r ON r.assessment_id=a.id
            WHERE a.course_id=%s
            GROUP BY a.id
            ORDER BY a.created_at DESC
        """, (course_id,))
        assessments = cur.fetchall()
    buttons = []
    for a in assessments:
        if a["total"] and a["unpublished"]:
            buttons.append([InlineKeyboardButton(
                f"Publish {a['assessment_name']} ({a['unpublished']} marks)",
                callback_data=f"publish:{a['id']}"
            )])
    if not buttons:
        await query.edit_message_text(
            "There are no unpublished marks for this course."
        )
        return
    await query.edit_message_text(
        "Choose the assessment to publish:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def publish_assessment_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["instructor"])
    if not session:
        await query.edit_message_text("Please /login as an instructor.")
        return
    assessment_id = int(query.data.split(":")[1])
    with db_cursor() as cur:
        cur.execute("""
            UPDATE results r SET published=TRUE
            FROM assessments a, instructor_courses ic
            WHERE r.assessment_id=a.id
              AND a.id=%s
              AND ic.course_id=a.course_id
              AND ic.instructor_id=%s
              AND r.published=FALSE
        """, (assessment_id, session["id"]))
        changed = cur.rowcount
    if changed:
        audit("instructor", session["id"],
              f"Published {changed} marks for assessment {assessment_id}")
        await query.edit_message_text(
            f"Published successfully. {changed} student marks are now visible."
        )
    else:
        await query.edit_message_text(
            "No marks were published. Verify that this assessment belongs to your course."
        )


async def student_courses_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["student"])
    if not session:
        await query.edit_message_text("Please /login as a student.")
        return
    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT c.id, c.course_code, c.course_name
            FROM courses c
            JOIN assessments a ON a.course_id=c.id
            JOIN results r ON r.assessment_id=a.id
            WHERE r.student_id=%s AND r.published=TRUE
            ORDER BY c.course_code
        """, (session["id"],))
        courses = cur.fetchall()
    if not courses:
        await query.edit_message_text("No published grades are available yet.")
        return
    buttons = [[InlineKeyboardButton(
        f"{c['course_code']} — {c['course_name']}",
        callback_data=f"studentcourse:{c['id']}"
    )] for c in courses]
    await query.edit_message_text(
        "Select a course to view your published grades:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def student_view_grades(update, context):
    session = require_role(update, context, ["student"])
    if not session:
        await update.message.reply_text("Please /login first.")
        return
    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT c.id, c.course_code, c.course_name
            FROM courses c
            JOIN assessments a ON a.course_id=c.id
            JOIN results r ON r.assessment_id=a.id
            WHERE r.student_id=%s AND r.published=TRUE
            ORDER BY c.course_code
        """, (session["id"],))
        courses = cur.fetchall()
    if not courses:
        await update.message.reply_text("No published grades are available yet.")
        return
    buttons = [[InlineKeyboardButton(
        f"{c['course_code']} — {c['course_name']}",
        callback_data=f"studentcourse:{c['id']}"
    )] for c in courses]
    await update.message.reply_text(
        "Select a course to view your published grades:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def student_course_callback(update, context):
    query = update.callback_query
    await query.answer()
    session = require_role(update, context, ["student"])
    if not session:
        await query.edit_message_text("Please /login as a student.")
        return
    course_id = int(query.data.split(":")[1])
    with db_cursor() as cur:
        cur.execute("""
            SELECT c.course_code, c.course_name,
                   a.assessment_name, a.max_score, r.score, r.uploaded_at
            FROM results r
            JOIN assessments a ON a.id=r.assessment_id
            JOIN courses c ON c.id=a.course_id
            WHERE r.student_id=%s AND c.id=%s AND r.published=TRUE
            ORDER BY a.created_at, a.id
        """, (session["id"], course_id))
        rows = cur.fetchall()
    if not rows:
        await query.edit_message_text("No published grades found for this course.")
        return
    header = f"{rows[0]['course_code']} — {rows[0]['course_name']}\n\n"
    lines = [
        f"• {r['assessment_name']}: {r['score']} / {r['max_score']}"
        for r in rows
    ]
    total_score = sum((Decimal(str(r["score"])) for r in rows), Decimal("0"))
    total_max = sum((Decimal(str(r["max_score"])) for r in rows), Decimal("0"))
    text = header + "\n".join(lines)
    text += f"\n\nPublished assessment total: {total_score} / {total_max}"
    await query.edit_message_text(text)


# ---------------------------------------------------------------------------
# Text menu routing
# ---------------------------------------------------------------------------

async def text_menu_router(update, context):
    text_value = update.message.text.strip()
    user_id = update.effective_user.id
    session = session_for(update, context)

    if text_value == "Logout":
        await logout(update, context)
    elif text_value == "Add student":
        await admin_add_student_start(update, context)
    elif text_value == "Add instructor":
        await admin_add_instructor_start(update, context)
    elif text_value == "Add course":
        await admin_add_course_start(update, context)
    elif text_value == "Assign course":
        await assign_course_start(update, context)
    elif text_value == "List courses":
        await list_courses(update, context)
    elif text_value == "My courses":
        await my_courses(update, context)
    elif text_value == "Create assessment":
        await create_assessment_start(update, context)
    elif text_value == "Upload grades":
        await upload_grades_start(update, context)
    elif text_value == "Edit Published Grades":
        await edit_published_grades_start(update, context)
    elif text_value == "Publish grades":
        await publish_grades_start(update, context)
    elif text_value == "View my grades":
        await student_view_grades(update, context)
    else:
        if not session:
            await update.message.reply_text("Please send /login to sign in.")
        else:
            await update.message.reply_text("Use the buttons in your menu or /help.")


# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

def build_application():
    application = Application.builder().token(TOKEN).concurrent_updates(False).build()

    login_conversation = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            LOGIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_get_id)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_get_password)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    admin_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Add student$"), admin_add_student_start),
            MessageHandler(filters.Regex("^Add instructor$"), admin_add_instructor_start),
            MessageHandler(filters.Regex("^Add course$"), admin_add_course_start),
            MessageHandler(filters.Regex("^Assign course$"), assign_course_start)
        ],
        states={
            ADMIN_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_flow_step)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    bulk_student_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Bulk Add Students$"), bulk_add_students_start)
        ],
        states={
            BULK_UPLOAD: [MessageHandler(filters.Document.ALL, receive_student_roster)],
            BULK_CONFIRM: [CallbackQueryHandler(
                bulk_registration_callback, pattern=r"^bulkconfirm:(yes|no)$"
            )]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    student_password_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Change Password$"), student_change_password_start)
        ],
        states={
            CHANGE_PASSWORD_CURRENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_change_password_current)],
            CHANGE_PASSWORD_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_change_password_new)],
            CHANGE_PASSWORD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_change_password_confirm)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    reset_credentials_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^Reset All Student Credentials$"),
                reset_all_credentials_start
            )
        ],
        states={
            RESET_CREDENTIALS_CONFIRM: [CallbackQueryHandler(
                reset_all_credentials_callback, pattern=r"^resetcreds:(yes|no)$"
            )]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    assessment_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Create assessment$"), create_assessment_start)
        ],
        states={
            ASSESSMENT_FLOW: [CallbackQueryHandler(
                assessment_course_callback, pattern=r"^assesscourse:\d+$"
            )],
            ASSESSMENT_FLOW + 1: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, assessment_name_step)
            ],
            ASSESSMENT_FLOW + 2: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, assessment_max_step)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    upload_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Upload grades$"), upload_grades_start)
        ],
        states={
            UPLOAD_FLOW: [CallbackQueryHandler(
                upload_course_callback, pattern=r"^uploadcourse:\d+$"
            )],
            UPLOAD_FLOW + 1: [CallbackQueryHandler(
                upload_assessment_callback, pattern=r"^uploadassessment:\d+$"
            )],
            UPLOAD_FLOW + 2: [
                MessageHandler(filters.Document.ALL, receive_grade_file)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    edit_published_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Edit Published Grades$"), edit_published_grades_start)],
        states={
            UPLOAD_FLOW: [CallbackQueryHandler(edit_published_course_callback, pattern=r"^editpubcourse:\d+$")],
            UPLOAD_FLOW + 1: [CallbackQueryHandler(edit_published_assessment_callback, pattern=r"^editpubassessment:\d+$")],
            UPLOAD_FLOW + 2: [MessageHandler(filters.Document.ALL, receive_grade_file)],
            UPLOAD_CONFIRM: [CallbackQueryHandler(confirm_published_grade_edits, pattern=r"^editpub:(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(login_conversation)
    application.add_handler(admin_conversation)
    application.add_handler(bulk_student_conversation)
    application.add_handler(student_password_conversation)
    application.add_handler(reset_credentials_conversation)
    application.add_handler(assessment_conversation)
    application.add_handler(upload_conversation)
    application.add_handler(edit_published_conversation)

    application.add_handler(CallbackQueryHandler(
        publish_course_callback, pattern=r"^pubcourse:\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        publish_assessment_callback, pattern=r"^publish:\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        student_course_callback, pattern=r"^studentcourse:\d+$"
    ))

    application.add_handler(MessageHandler(
        filters.Regex("^(My courses|View my grades|Publish grades|Logout|"
                      "Add student|Add instructor|Add course|Assign course|"
                      "List courses|Create assessment|Upload grades|Edit Published Grades)$"),
        text_menu_router
    ))

    return application


def main():
    logger.info("Checking Section B grade-system database schema...")
    check_database_schema()
    logger.info("Database schema verified. Starting Telegram bot.")
    application = build_application()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
