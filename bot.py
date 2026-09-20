import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise ValueError("TOKEN is missing from Render Environment Variables.")

DB_FILE = "election.db"

ELECTION_DURATION_MINUTES = 60

# Special simulation/test voter
SPECIAL_TEST_STUDENT_ID = "UGR/6094/17"

# ------------------------------------------------------------
# ADMIN TELEGRAM IDS
#
# Render environment variable:
#
# ADMIN_IDS=123456789,987654321
# ------------------------------------------------------------

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# ------------------------------------------------------------
# CANDIDATES
# ------------------------------------------------------------

CANDIDATES = {
    1: "Biruktawit Zelalem",
    2: "Mihretab Kushe",
    3: "Dagim Badeg",
}

# Optional candidate Telegram IDs.
#
# Example Render variables:
#
# CANDIDATE_1_ID=123456789
# CANDIDATE_2_ID=987654321
# CANDIDATE_3_ID=555555555
#
# We can configure these later.

CANDIDATE_TELEGRAM_IDS = {}

for candidate_number in CANDIDATES:
    value = os.getenv(f"CANDIDATE_{candidate_number}_ID")

    if value and value.isdigit():
        CANDIDATE_TELEGRAM_IDS[candidate_number] = int(value)


# ============================================================
# ELIGIBLE VOTERS
# ============================================================

VOTER_DATA = """
UGR/6881/17|ABENET TILAHUN SOLOMON
UGR/0202/17|Abigiya Aklilu Wendafirash
UGR/7506/17|AMANUEL GETACHEW TEKA
UGR/2686/17|Amdiyon Mifta Ahmed
UGR/6329/17|Beimnet Tasew Tsegaye
UGR/0253/17|Bilise Motuma Begna
UGR/0349/17|Biruktawit Zelalem Nigatu
UGR/4198/17|Blen Gebrehiwet Areaya
UGR/5340/17|Dagim Badeg Banata
UGR/1314/17|DANIEl Getahun Feleke
UGR/9683/17|DENEKEW TADELE YIHUN
UGR/9872/16|Ebtisam Sultan Adam
UGR/2353/17|Edom Tadesse Berhanu
UGR/4938/17|Elsabeth Mahteme Kifle
UGR/3391/17|Ephrata Mebt Admass
UGR/7557/17|Ephrem Teshome Kebede
UGR/0996/17|Essey Kifleeyesus Kidane
UGR/7434/17|Eyasu Solomon Sando
UGR/5704/17|Eyosiyas Geremew Kebede
UGR/2958/17|Fares Girum Kebede
UGR/2135/17|Fitsum Girma Abadi
UGR/6622/17|Gadisa Takele Begna
UGR/3489/17|GIFTI TESFAYE GUDETA
UGR/0162/17|Girum Muluken Assefa
UGR/4727/17|Haileyesus Tadesse Birhanu
UGR/6318/17|Hemen Anteneh Chekol
UGR/7290/17|Hilina Tadesse Gebremeskel
UGR/3854/17|Hiluf Girmay Berhe
UGR/9900/17|Kalid Sultan Barsebo
UGR/2542/17|Kalkidan Getenet Tadesse
UGR/3435/17|Kirubel Belayneh Abat
UGR/0737/17|Lensa Tariku Oluma
UGR/6534/17|Leul Ephrem Bezu
UGR/2398/17|Mahlet Zerihun Tefera
UGR/9262/17|Mariamawit Nigussie Bikila
UGR/8716/17|Meklit Gebremichael Deres
UGR/4754/17|Meti Teshome Alemu
UGR/9905/17|Mihretab Kushe Kussa
UGR/6094/17|Nahom Lulu Mulugeta
UGR/2414/17|Nahom Zelalem Fisseha
UGR/6076/17|Nanati Abdissa Kumssa
UGR/5338/17|Nathanael Mehari Cherkos
UGR/4666/17|Nebiyu Esayas Bayisa
UGR/0507/17|Nigus Gebremedhin Tesfay
UGR/8472/17|Osman Mahmud Tesiso
UGR/7081/17|Peniel Mulugeta ALEMAYEHU
UGR/4559/17|RUMEYSUA ABDUSELAM ANWAR
UGR/2093/17|Salem Kassahun Gararo
UGR/6704/17|salsawit abera asrat
UGR/7152/17|Selamawit Fikru Shikur
UGR/3303/17|Solomon Mesfin Moges
UGR/9309/17|Teamir Mulye Tesfaye
UGR/5199/17|Tesnim Hussein Mohammed
UGR/7046/17|Tinsae Shemeles Tadesse
UGR/9202/17|Tsigereda Abebaw Balew
UGR/1574/17|Tsion Dereje Oda
UGR/7293/17|YIDIDIYA MERKINEH MENA
UGR/6785/17|Yodahe Samuel Desta
UGR/4908/17|Yonas Solomon Fikru
UGR/5150/17|Yonatan Hagos Woldegebriel
UGR/8278/17|Yosef Amdneh Ebsa
UGR/0393/17|Zekariyas Niguse Teka
"""

ELIGIBLE_VOTERS = {}

for line in VOTER_DATA.strip().splitlines():
    student_id, name = line.split("|", 1)
    ELIGIBLE_VOTERS[student_id.strip().upper()] = name.strip()


# ============================================================
# FLASK
# ============================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Section B Election Bot is alive."


def run_flask():
    web_app.run(host="0.0.0.0", port=8080)


threading.Thread(target=run_flask, daemon=True).start()


# ============================================================
# DATABASE
# ============================================================

def db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS election (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            started_at TEXT,
            ends_at TEXT,
            active INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS voters (
            student_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            telegram_id INTEGER,
            has_voted INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            telegram_id INTEGER NOT NULL,
            candidate_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            telegram_id INTEGER,
            candidate_id INTEGER,
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        INSERT OR IGNORE INTO election(id, active)
        VALUES(1, 0)
    """)

    for student_id, name in ELIGIBLE_VOTERS.items():
        conn.execute("""
            INSERT OR IGNORE INTO voters(student_id, name)
            VALUES(?, ?)
        """, (student_id, name))

    conn.commit()
    conn.close()


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def normalize_id(value):
    return value.strip().upper()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def election_active():
    conn = db()

    row = conn.execute("""
        SELECT active, ends_at
        FROM election
        WHERE id = 1
    """).fetchone()

    conn.close()

    if not row or row["active"] != 1 or not row["ends_at"]:
        return False

    end_time = datetime.fromisoformat(row["ends_at"])

    return now_utc() < end_time


def start_election():
    start = now_utc()
    end = start + timedelta(minutes=ELECTION_DURATION_MINUTES)

    conn = db()

    conn.execute("""
        UPDATE election
        SET started_at = ?,
            ends_at = ?,
            active = 1
        WHERE id = 1
    """, (
        start.isoformat(),
        end.isoformat(),
    ))

    conn.commit()
    conn.close()

    return start, end


def close_election():
    conn = db()

    conn.execute("""
        UPDATE election
        SET active = 0
        WHERE id = 1
    """)

    conn.commit()
    conn.close()


def candidate_name(candidate_id):
    return CANDIDATES.get(candidate_id, "Unknown")


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton(
            "📜 Rules & Regulations",
            callback_data="rules"
        )],
        [InlineKeyboardButton(
            "👥 Candidate List",
            callback_data="candidates"
        )],
        [InlineKeyboardButton(
            "🗳️ Vote Now",
            callback_data="vote"
        )],
    ]

    await update.message.reply_text(
        "🗳️ *SECTION B ELECTION*\n\n"
        "Welcome to the election system.\n\n"
        "Please review the rules before voting.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# RULES
# ============================================================

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(
            "👥 Candidate List",
            callback_data="candidates"
        )],
        [InlineKeyboardButton(
            "🗳️ Vote Now",
            callback_data="vote"
        )],
    ]

    await query.edit_message_text(
        "📜 *RULES & REGULATIONS*\n\n"
        "• The election is open for 60 minutes.\n"
        "• Only registered Section B voters are eligible.\n"
        "• Normal voters may cast one vote.\n"
        "• You first select a candidate.\n"
        "• Your selection is NOT counted immediately.\n"
        "• You must provide a valid Student ID.\n"
        "• The vote is counted only after successful validation.\n"
        "• Invalid Student IDs do not produce votes.\n"
        "• Attempts to vote again after voting are logged.\n"
        "• The candidate with the highest valid vote count wins.\n\n"
        "🧪 A designated simulation test account has special "
        "repeat-voting permission for testing.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# CANDIDATES
# ============================================================

async def candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "👥 *CANDIDATES*\n\n"

    for number, name in CANDIDATES.items():
        text += f"{number}. {name}\n"

    keyboard = [
        [InlineKeyboardButton(
            "🗳️ Vote Now",
            callback_data="vote"
        )],
        [InlineKeyboardButton(
            "📜 Rules",
            callback_data="rules"
        )],
    ]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# VOTE START
# ============================================================

async def vote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not election_active():
        await query.edit_message_text(
            "🔴 *Voting is currently closed.*\n\n"
            "Please wait until the administrator opens the election.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    keyboard = []

    for number, name in CANDIDATES.items():
        keyboard.append([
            InlineKeyboardButton(
                name,
                callback_data=f"candidate:{number}"
            )
        ])

    await query.edit_message_text(
        "🗳️ *SELECT YOUR CANDIDATE*\n\n"
        "Select one candidate.\n\n"
        "⚠️ Your vote will NOT be counted yet.\n"
        "You will be asked for your Student ID next.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return SELECTING_CANDIDATE


# ============================================================
# CANDIDATE SELECTED
# ============================================================

async def candidate_selected(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if not election_active():
        await query.edit_message_text("🔴 Voting is closed.")
        return ConversationHandler.END

    candidate_id = int(query.data.split(":")[1])

    if candidate_id not in CANDIDATES:
        await query.edit_message_text("❌ Invalid candidate.")
        return ConversationHandler.END

    # IMPORTANT:
    # Nothing is counted here.
    context.user_data["pending_candidate"] = candidate_id

    await query.edit_message_text(
        f"✅ Selected: *{candidate_name(candidate_id)}*\n\n"
        "🪪 Please enter your Student ID.\n\n"
        "Example:\n"
        "`UGR/6881/17`\n\n"
        "Your vote will be counted only after your ID "
        "is successfully validated.",
        parse_mode="Markdown",
    )

    return WAITING_FOR_ID


# ============================================================
# STUDENT ID
# ============================================================

async def receive_student_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not election_active():
        await update.message.reply_text(
            "🔴 The election has closed."
        )
        context.user_data.clear()
        return ConversationHandler.END

    candidate_id = context.user_data.get("pending_candidate")

    if candidate_id not in CANDIDATES:
        await update.message.reply_text(
            "❌ No pending candidate selection.\n"
            "Please use /start and begin again."
        )
        context.user_data.clear()
        return ConversationHandler.END

    telegram_id = update.effective_user.id
    student_id = normalize_id(update.message.text)
    timestamp = now_utc().isoformat()

    conn = db()

    # --------------------------------------------------------
    # 1. VALIDATE STUDENT ID
    # --------------------------------------------------------

    voter = conn.execute("""
        SELECT *
        FROM voters
        WHERE student_id = ?
    """, (student_id,)).fetchone()

    if not voter:

        conn.execute("""
            INSERT INTO attempts(
                student_id,
                telegram_id,
                candidate_id,
                status,
                reason,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
        """, (
            student_id,
            telegram_id,
            candidate_id,
            "REJECTED",
            "Invalid Student ID",
            timestamp,
        ))

        conn.commit()
        conn.close()

        await update.message.reply_text(
            "❌ *INVALID STUDENT ID*\n\n"
            "Your vote was NOT counted.\n"
            "The attempt has been logged.",
            parse_mode="Markdown",
        )

        context.user_data.clear()
        return ConversationHandler.END

    # --------------------------------------------------------
    # 2. SPECIAL TEST VOTER
    # --------------------------------------------------------

    if student_id == SPECIAL_TEST_STUDENT_ID:

        conn.execute("""
            INSERT INTO votes(
                student_id,
                telegram_id,
                candidate_id,
                created_at
            )
            VALUES(?, ?, ?, ?)
        """, (
            student_id,
            telegram_id,
            candidate_id,
            timestamp,
        ))

        conn.execute("""
            INSERT INTO attempts(
                student_id,
                telegram_id,
                candidate_id,
                status,
                reason,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
        """, (
            student_id,
            telegram_id,
            candidate_id,
            "VALID_TEST_VOTE",
            "Simulation test voter",
            timestamp,
        ))

        conn.commit()

        total = conn.execute("""
            SELECT COUNT(*)
            FROM votes
            WHERE student_id = ?
        """, (student_id,)).fetchone()[0]

        conn.close()

        await update.message.reply_text(
            "✅ *VOTE SUCCESSFULLY RECORDED*\n\n"
            f"Candidate: *{candidate_name(candidate_id)}*\n"
            f"Student ID: `{student_id}`\n"
            "Your vote has been counted.",
            parse_mode="Markdown",
        )

        context.user_data.clear()
        return ConversationHandler.END

    # --------------------------------------------------------
    # 3. NORMAL VOTER — ALREADY VOTED?
    # --------------------------------------------------------

    if voter["has_voted"] == 1:

        conn.execute("""
            INSERT INTO attempts(
                student_id,
                telegram_id,
                candidate_id,
                status,
                reason,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
        """, (
            student_id,
            telegram_id,
            candidate_id,
            "REJECTED",
            "Already voted",
            timestamp,
        ))

        attempt_count = conn.execute("""
            SELECT COUNT(*)
            FROM attempts
            WHERE student_id = ?
        """, (student_id,)).fetchone()[0]

        conn.commit()
        conn.close()

        warning = ""

        if attempt_count > 1:
            warning = (
                "\n\n⚠️ Multiple voting attempts detected.\n"
                f"Total attempts recorded: {attempt_count}"
            )

        await update.message.reply_text(
            "⚠️ *VOTE NOT COUNTED*\n\n"
            "This Student ID has already cast its vote."
            + warning,
            parse_mode="Markdown",
        )

        context.user_data.clear()
        return ConversationHandler.END

    # --------------------------------------------------------
    # 4. VALID VOTE
    #
    # ONLY HERE IS THE VOTE ACTUALLY COUNTED.
    # --------------------------------------------------------

    conn.execute("""
        INSERT INTO votes(
            student_id,
            telegram_id,
            candidate_id,
            created_at
        )
        VALUES(?, ?, ?, ?)
    """, (
        student_id,
        telegram_id,
        candidate_id,
        timestamp,
    ))

    conn.execute("""
        UPDATE voters
        SET telegram_id = ?,
            has_voted = 1
        WHERE student_id = ?
    """, (
        telegram_id,
        student_id,
    ))

    conn.execute("""
        INSERT INTO attempts(
            student_id,
            telegram_id,
            candidate_id,
            status,
            reason,
            created_at
        )
        VALUES(?, ?, ?, ?, ?, ?)
    """, (
        student_id,
        telegram_id,
        candidate_id,
        "VALID_VOTE",
        "Student ID validated successfully",
        timestamp,
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ *VOTE SUCCESSFULLY RECORDED*\n\n"
        f"Candidate: *{candidate_name(candidate_id)}*\n"
        f"Student ID: `{student_id}`\n\n"
        "Your vote has been counted.",
        parse_mode="Markdown",
    )

    context.user_data.clear()
    return ConversationHandler.END


# ============================================================
# ADMIN: START ELECTION
# ============================================================

async def start_election_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    if election_active():
        await update.message.reply_text(
            "⚠️ The election is already running."
        )
        return

    start_time, end_time = start_election()

    await update.message.reply_text(
        "🟢 *ELECTION STARTED*\n\n"
        "Duration: 60 minutes\n"
        f"Started: `{start_time.strftime('%H:%M:%S UTC')}`\n"
        f"Ends: `{end_time.strftime('%H:%M:%S UTC')}`\n\n"
        "Voting is now open.",
        parse_mode="Markdown",
    )


# ============================================================
# ADMIN: STOP ELECTION
# ============================================================

async def stop_election_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    close_election()

    await update.message.reply_text(
        "🔴 *ELECTION CLOSED*\n\n"
        "No further votes can be recorded.",
        parse_mode="Markdown",
    )


# ============================================================
# ADMIN: RESULTS
# ============================================================

async def admin_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    conn = db()

    total_votes = conn.execute("""
        SELECT COUNT(*)
        FROM votes
    """).fetchone()[0]

    voted_students = conn.execute("""
        SELECT COUNT(*)
        FROM voters
        WHERE has_voted = 1
    """).fetchone()[0]

    results = []

    for candidate_id, name in CANDIDATES.items():
        count = conn.execute("""
            SELECT COUNT(*)
            FROM votes
            WHERE candidate_id = ?
        """, (candidate_id,)).fetchone()[0]

        results.append((candidate_id, name, count))

    conn.close()

    status = "🟢 OPEN" if election_active() else "🔴 CLOSED"

    text = (
        "📊 *ELECTION DASHBOARD*\n\n"
        f"Status: {status}\n"
        f"Eligible voters: {len(ELIGIBLE_VOTERS)}\n"
        f"Students marked voted: {voted_students}\n"
        f"Total valid votes: {total_votes}\n\n"
    )

    for candidate_id, name, count in results:
        text += f"*{candidate_id}. {name}* — {count}\n"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# ============================================================
# ADMIN: NON-VOTERS
# ============================================================

async def admin_nonvoters(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    conn = db()

    rows = conn.execute("""
        SELECT student_id, name
        FROM voters
        WHERE has_voted = 0
        ORDER BY student_id
    """).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(
            "✅ All registered voters have voted."
        )
        return

    text = (
        f"👥 *NON-VOTERS*\n\n"
        f"Remaining: {len(rows)}\n\n"
    )

    for index, row in enumerate(rows, 1):
        text += f"{index}. `{row['student_id']}` — {row['name']}\n"

        # Telegram message size protection
        if len(text) > 3500:
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
            )
            text = ""

    if text:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
        )


# ============================================================
# ADMIN: MULTIPLE ATTEMPTS
# ============================================================

async def admin_attempts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    conn = db()

    rows = conn.execute("""
        SELECT
            student_id,
            COUNT(*) AS attempts
        FROM attempts
        GROUP BY student_id
        HAVING COUNT(*) > 1
        ORDER BY attempts DESC
    """).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(
            "✅ No repeated voting attempts detected."
        )
        return

    text = "⚠️ *MULTIPLE VOTING ATTEMPTS*\n\n"

    for row in rows:
        text += (
            f"`{row['student_id']}` — "
            f"{row['attempts']} attempts\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# ============================================================
# ADMIN: FULL ATTEMPT LOG
# ============================================================

async def admin_logs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    conn = db()

    rows = conn.execute("""
        SELECT
            student_id,
            candidate_id,
            status,
            reason,
            created_at
        FROM attempts
        ORDER BY id DESC
        LIMIT 30
    """).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(
            "No voting attempts recorded yet."
        )
        return

    text = "🧾 *RECENT VOTING ATTEMPTS*\n\n"

    for row in rows:
        candidate = candidate_name(row["candidate_id"])

        text += (
            f"`{row['student_id']}`\n"
            f"Candidate: {candidate}\n"
            f"Status: {row['status']}\n"
            f"Reason: {row['reason']}\n"
            f"Time: {row['created_at']}\n\n"
        )

        if len(text) > 3500:
            await update.message.reply_text(
                text,
                parse_mode="Markdown"
            )
            text = ""

    if text:
        await update.message.reply_text(
            text,
            parse_mode="Markdown"
        )


# ============================================================
# CANDIDATE SCORE
# ============================================================

async def my_score(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    telegram_id = update.effective_user.id

    candidate_id = None

    for number, candidate_telegram_id in CANDIDATE_TELEGRAM_IDS.items():
        if telegram_id == candidate_telegram_id:
            candidate_id = number
            break

    if candidate_id is None:
        await update.message.reply_text(
            "⛔ Candidate access is not configured for your account."
        )
        return

    conn = db()

    count = conn.execute("""
        SELECT COUNT(*)
        FROM votes
        WHERE candidate_id = ?
    """, (candidate_id,)).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        f"📊 *YOUR LIVE SCORE*\n\n"
        f"Candidate: *{candidate_name(candidate_id)}*\n"
        f"Valid votes: *{count}*",
        parse_mode="Markdown",
    )


# ============================================================
# USER ID
# ============================================================

async def my_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        f"Your Telegram user ID is:\n`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


# ============================================================
# MAIN
# ============================================================

SELECTING_CANDIDATE = 1
WAITING_FOR_ID = 2


def main():

    init_database()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # BASIC COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("myid", my_id)
    )

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start_election",
            start_election_command
        )
    )

    application.add_handler(
        CommandHandler(
            "stop_election",
            stop_election_command
        )
    )

    application.add_handler(
        CommandHandler(
            "results",
            admin_results
        )
    )

    application.add_handler(
        CommandHandler(
            "nonvoters",
            admin_nonvoters
        )
    )

    application.add_handler(
        CommandHandler(
            "attempts",
            admin_attempts
        )
    )

    application.add_handler(
        CommandHandler(
            "logs",
            admin_logs
        )
    )

    application.add_handler(
        CommandHandler(
            "myscore",
            my_score
        )
    )

    # --------------------------------------------------------
    # MAIN VOTING CONVERSATION
    # --------------------------------------------------------

    voting_conversation = ConversationHandler(

        entry_points=[
            CallbackQueryHandler(
                vote_start,
                pattern="^vote$"
            )
        ],

        states={

            SELECTING_CANDIDATE: [
                CallbackQueryHandler(
                    candidate_selected,
                    pattern=r"^candidate:\d+$"
                )
            ],

            WAITING_FOR_ID: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_student_id
                )
            ],
        },

        fallbacks=[
            CommandHandler("start", start)
        ],

        allow_reentry=True,
    )

    application.add_handler(voting_conversation)

    # --------------------------------------------------------
    # NON-CONVERSATION BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            rules,
            pattern="^rules$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            candidates,
            pattern="^candidates$"
        )
    )

    print("🚀 Section B Election Bot is running...")

    application.run_polling(
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )


if __name__ == "__main__":
    main()