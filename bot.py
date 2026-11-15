"""
Section B Grade Management Telegram Bot
Python 3.10+
Uses the existing Supabase PostgreSQL schema created for the grade system.

Required environment variables:
  TOKEN         Telegram bot token
  DATABASE_URL  Supabase PostgreSQL connection string
  ADMIN_IDS     Comma-separated Telegram numeric IDs for Super Admins

Supported grade-file columns:
  student_id, score
Optional columns:
  full_name (ignored for matching; student_id is authoritative)

CSV and XLSX files are supported. An instructor must create an assessment
before uploading marks for it. Uploads are validated completely before
database changes are committed.
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
    InlineKeyboardMarkup, InlineKeyboardButton
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
    [KeyboardButton("Add student"), KeyboardButton("Add instructor")],
    [KeyboardButton("Add course"), KeyboardButton("Assign course")],
    [KeyboardButton("List courses"), KeyboardButton("Logout")]
]
INSTRUCTOR_MENU = [
    [KeyboardButton("My courses"), KeyboardButton("Create assessment")],
    [KeyboardButton("Upload grades"), KeyboardButton("Publish grades")],
    [KeyboardButton("Logout")]
]
STUDENT_MENU = [
    [KeyboardButton("My courses"), KeyboardButton("View my grades")],
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
# Instructor course and assessment workflows
# ---------------------------------------------------------------------------

ASSESSMENT_FLOW = 20
UPLOAD_FLOW = 21


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

def parse_grade_file(filename, raw_bytes):
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
        reader = csv.DictReader(io.StringIO(text_data))
        if not reader.fieldnames:
            raise ValueError("CSV file is empty or has no header row.")
        normalized = {h.strip().lower(): h for h in reader.fieldnames if h}
        if "student_id" not in normalized or "score" not in normalized:
            raise ValueError("Required CSV headers: student_id, score.")
        for line_number, row in enumerate(reader, start=2):
            rows.append({
                "line": line_number,
                "student_id": str(row.get(normalized["student_id"], "")).strip(),
                "score": str(row.get(normalized["score"], "")).strip()
            })
    elif extension in ("xlsx", "xlsm"):
        if load_workbook is None:
            raise ValueError("Excel support is not installed. Add openpyxl to requirements.txt.")
        workbook = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        headers = next(iterator, None)
        if not headers:
            raise ValueError("Excel file is empty.")
        normalized = {
            str(value).strip().lower(): index
            for index, value in enumerate(headers) if value is not None
        }
        if "student_id" not in normalized or "score" not in normalized:
            raise ValueError("Required Excel headers: student_id, score.")
        for line_number, values in enumerate(iterator, start=2):
            sid = values[normalized["student_id"]] if normalized["student_id"] < len(values) else ""
            score = values[normalized["score"]] if normalized["score"] < len(values) else ""
            rows.append({
                "line": line_number,
                "student_id": "" if sid is None else str(sid).strip(),
                "score": "" if score is None else str(score).strip()
            })
        workbook.close()
    else:
        raise ValueError("Upload a .csv or .xlsx file.")

    if not rows:
        raise ValueError("No grade rows were found.")
    return rows


async def upload_grades_start(update, context):
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
        "Now upload a CSV or XLSX file with these exact column headers:\n"
        "student_id, score\n\n"
        "Example:\n"
        "student_id,score\n"
        "SB2024001,18\n"
        "SB2024002,16.5"
    )
    return UPLOAD_FLOW + 2


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
                    errors.append(f"Row {row['line']}: missing student_id")
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
                        f"Row {row['line']}: invalid score for {sid}; "
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(login_conversation)
    application.add_handler(admin_conversation)
    application.add_handler(assessment_conversation)
    application.add_handler(upload_conversation)

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
                      "List courses|Create assessment|Upload grades)$"),
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
