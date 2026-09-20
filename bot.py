import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

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
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("TOKEN environment variable is missing.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is missing.")


ELECTION_DURATION_MINUTES = 60

# Special simulation/test voter.
# This account may cast multiple VALID votes.
SPECIAL_TEST_STUDENT_ID = "UGR/6094/17"


# ============================================================
# ADMIN IDS
# ============================================================

ADMIN_IDS = set()

admin_ids_raw = os.getenv("ADMIN_IDS", "")

for value in admin_ids_raw.split(","):
    value = value.strip()

    if value:
        try:
            ADMIN_IDS.add(int(value))
        except ValueError:
            pass


# ============================================================
# CANDIDATES
# ============================================================

CANDIDATES = {
    1: "Biruktawit Zelalem",
    2: "Mihretab Kushe",
    3: "Dagim Badeg",
}


# Optional candidate Telegram IDs.
# Set these in Render if candidates should be able to use /myscore.
CANDIDATE_TELEGRAM_IDS = {}

for candidate_id in CANDIDATES:
    env_name = f"CANDIDATE_{candidate_id}_ID"
    value = os.getenv(env_name)

    if value:
        try:
            CANDIDATE_TELEGRAM_IDS[candidate_id] = int(value)
        except ValueError:
            pass


# ============================================================
# ELIGIBLE SECTION B VOTERS
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
    ELIGIBLE_VOTERS[student_id.strip()] = name.strip()


# ============================================================
# FLASK KEEP-ALIVE SERVER
# ============================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Section B Election Bot is alive."


@web_app.route("/health")
def health():
    return "OK"


def run_web_server():
    port = int(os.getenv("PORT", 8080))

    web_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


# ============================================================
# DATABASE
# ============================================================

def db():
    """
    Create a PostgreSQL connection to Supabase.
    """
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=15,
    )


def init_database():
    """
    Prepare the existing Supabase database.

    Tables are already created in Supabase SQL Editor.
    This function only ensures that:
      1. election row exists
      2. all 62 eligible voters exist
    """

    connection = db()

    try:
        cursor = connection.cursor()

        # Make sure election row exists.
        cursor.execute(
            """
            INSERT INTO election (id, active)
            VALUES (1, FALSE)
            ON CONFLICT (id) DO NOTHING
            """
        )

        # Insert eligible voters.
        for student_id, name in ELIGIBLE_VOTERS.items():
            cursor.execute(
                """
                INSERT INTO voters
                    (student_id, name, telegram_id, has_voted)
                VALUES
                    (%s, %s, NULL, FALSE)
                ON CONFLICT (student_id) DO NOTHING
                """,
                (student_id, name),
            )

        connection.commit()

        print(
            f"Database initialized successfully. "
            f"{len(ELIGIBLE_VOTERS)} eligible voters loaded."
        )

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def normalize_id(student_id):
    """
    Normalize student IDs so that:
    ugr/5340/17
    UGR/5340/17
    UGR / 5340 / 17

    all become:

    UGR/5340/17
    """

    if not student_id:
        return ""

    student_id = student_id.strip().upper()

    student_id = student_id.replace(" ", "")

    return student_id


def is_admin(user_id):
    return user_id in ADMIN_IDS


def candidate_name(candidate_id):
    return CANDIDATES.get(candidate_id, "Unknown candidate")


def election_active():
    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT active, ends_at
            FROM election
            WHERE id = 1
            """
        )

        row = cursor.fetchone()

        if not row:
            return False

        if not row["active"]:
            return False

        ends_at = row["ends_at"]

        if ends_at and now_utc() >= ends_at:
            cursor.execute(
                """
                UPDATE election
                SET active = FALSE
                WHERE id = 1
                """
            )

            connection.commit()

            return False

        return True

    finally:
        connection.close()


def get_election():
    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM election
            WHERE id = 1
            """
        )

        return cursor.fetchone()

    finally:
        connection.close()


def get_candidate_votes(candidate_id):
    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM votes
            WHERE candidate_id = %s
            """,
            (candidate_id,),
        )

        row = cursor.fetchone()

        return row["count"] if row else 0

    finally:
        connection.close()


# ============================================================
# ELECTION CONTROL
# ============================================================

def start_election():
    connection = db()

    try:
        cursor = connection.cursor()

        started_at = now_utc()
        ends_at = started_at + timedelta(
            minutes=ELECTION_DURATION_MINUTES
        )

        cursor.execute(
            """
            UPDATE election
            SET
                started_at = %s,
                ends_at = %s,
                active = TRUE
            WHERE id = 1
            """,
            (started_at, ends_at),
        )

        connection.commit()

        return started_at, ends_at

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def close_election():
    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            UPDATE election
            SET active = FALSE
            WHERE id = 1
            """
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# START / RULES / CANDIDATES
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "📜 Rules & Regulations",
                callback_data="rules",
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Candidate List",
                callback_data="candidates",
            )
        ],
        [
            InlineKeyboardButton(
                "🗳️ Vote",
                callback_data="vote",
            )
        ],
    ]

    await update.message.reply_text(
        "🗳️ *Section B Simulated Election*\n\n"
        "Welcome to the Section B election simulation system.\n\n"
        "Please review the rules and candidates before voting.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def show_rules(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    text = (
        "📜 *RULES & REGULATIONS*\n\n"
        "1. Only registered Section B students may vote.\n\n"
        "2. Each normal voter may cast one valid vote.\n\n"
        "3. Your Student ID must match the registered voter list.\n\n"
        "4. Invalid Student IDs do not create votes.\n\n"
        "5. A normal voter who has already voted cannot vote again.\n\n"
        "6. The election lasts for 60 minutes after it is started.\n\n"
        "7. The candidate with the highest number of valid votes "
        "has the highest vote count.\n\n"
        "8. This is a simulated/laboratory election system."
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "👥 Candidate List",
                callback_data="candidates",
            )
        ],
        [
            InlineKeyboardButton(
                "🗳️ Vote",
                callback_data="vote",
            )
        ],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def show_candidates(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    text = "👥 *CANDIDATES*\n\n"

    for candidate_id, name in CANDIDATES.items():
        text += f"{candidate_id}. {name}\n"

    keyboard = [
        [
            InlineKeyboardButton(
                "🗳️ Vote Now",
                callback_data="vote",
            )
        ],
        [
            InlineKeyboardButton(
                "📜 Rules",
                callback_data="rules",
            )
        ],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ============================================================
# VOTING FLOW
# ============================================================

SELECT_CANDIDATE, ENTER_STUDENT_ID = range(2)


async def start_vote(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if not election_active():
        await query.edit_message_text(
            "🔴 *Voting is currently closed.*\n\n"
            "Please wait until the election is opened.",
            parse_mode="Markdown",
        )

        return ConversationHandler.END

    keyboard = []

    for candidate_id, name in CANDIDATES.items():
        keyboard.append(
            [
                InlineKeyboardButton(
                    name,
                    callback_data=f"candidate_{candidate_id}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="cancel_vote",
            )
        ]
    )

    await query.edit_message_text(
        "🗳️ *SELECT YOUR CANDIDATE*\n\n"
        "Choose the candidate you want to vote for.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    return SELECT_CANDIDATE


async def select_candidate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_vote":
        await query.edit_message_text(
            "❌ Voting cancelled."
        )

        return ConversationHandler.END

    try:
        candidate_id = int(
            query.data.replace("candidate_", "")
        )
    except ValueError:
        await query.edit_message_text(
            "❌ Invalid candidate selection."
        )

        return ConversationHandler.END

    if candidate_id not in CANDIDATES:
        await query.edit_message_text(
            "❌ Invalid candidate."
        )

        return ConversationHandler.END

    context.user_data["pending_candidate"] = candidate_id

    await query.edit_message_text(
        "🪪 *ENTER YOUR STUDENT ID*\n\n"
        "Please enter your registered Section B Student ID.\n\n"
        "Example:\n"
        "`UGR/5340/17`",
        parse_mode="Markdown",
    )

    return ENTER_STUDENT_ID


async def receive_student_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not election_active():
        await update.message.reply_text(
            "🔴 Voting has closed."
        )

        context.user_data.pop("pending_candidate", None)

        return ConversationHandler.END

    candidate_id = context.user_data.get(
        "pending_candidate"
    )

    if candidate_id not in CANDIDATES:
        await update.message.reply_text(
            "❌ Your voting session expired. "
            "Please press /start and try again."
        )

        return ConversationHandler.END

    raw_student_id = update.message.text
    student_id = normalize_id(raw_student_id)

    telegram_id = update.effective_user.id

    connection = db()

    try:
        cursor = connection.cursor()

        # ----------------------------------------------------
        # Check whether Student ID exists.
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT student_id, name, telegram_id, has_voted
            FROM voters
            WHERE student_id = %s
            """,
            (student_id,),
        )

        voter = cursor.fetchone()

        # ----------------------------------------------------
        # INVALID STUDENT ID
        # ----------------------------------------------------

        if not voter:
            cursor.execute(
                """
                INSERT INTO attempts
                    (
                        student_id,
                        telegram_id,
                        candidate_id,
                        status,
                        reason,
                        created_at
                    )
                VALUES
                    (%s, %s, %s, %s, %s, %s)
                """,
                (
                    student_id,
                    telegram_id,
                    candidate_id,
                    "REJECTED",
                    "Invalid Student ID",
                    now_utc(),
                ),
            )

            connection.commit()

            await update.message.reply_text(
                "❌ *INVALID STUDENT ID*\n\n"
                "No vote was recorded.\n\n"
                "Please check your Student ID and try again.",
                parse_mode="Markdown",
            )

            return ConversationHandler.END

        # ----------------------------------------------------
        # SPECIAL SIMULATION/TEST VOTER
        # ----------------------------------------------------

        if student_id == SPECIAL_TEST_STUDENT_ID:

            # Record Telegram account associated with the test ID.
            cursor.execute(
                """
                UPDATE voters
                SET telegram_id = %s
                WHERE student_id = %s
                """,
                (
                    telegram_id,
                    student_id,
                ),
            )

            # Record the actual valid vote.
            cursor.execute(
                """
                INSERT INTO votes
                    (
                        student_id,
                        telegram_id,
                        candidate_id,
                        created_at
                    )
                VALUES
                    (%s, %s, %s, %s)
                """,
                (
                    student_id,
                    telegram_id,
                    candidate_id,
                    now_utc(),
                ),
            )

            # Record successful attempt.
            cursor.execute(
                """
                INSERT INTO attempts
                    (
                        student_id,
                        telegram_id,
                        candidate_id,
                        status,
                        reason,
                        created_at
                    )
                VALUES
                    (%s, %s, %s, %s, %s, %s)
                """,
                (
                    student_id,
                    telegram_id,
                    candidate_id,
                    "VALID_TEST_VOTE",
                    "Special simulation/test account",
                    now_utc(),
                ),
            )

            # Count how many valid votes this special account has.
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM votes
                WHERE student_id = %s
                """,
                (student_id,),
            )

            total = cursor.fetchone()["count"]

            connection.commit()

            context.user_data.pop("pending_candidate", None)

            await update.message.reply_text(
                "✅ *VOTE SUCCESSFULLY RECORDED*\n\n"
                f"Candidate: *{candidate_name(candidate_id)}*\n"
                f"Student ID: `{student_id}`\n"
                "Your vote has been counted.",
                parse_mode="Markdown",
            )

            return ConversationHandler.END

        # ----------------------------------------------------
        # NORMAL VOTER ALREADY VOTED
        # ----------------------------------------------------

        if voter["has_voted"]:
            cursor.execute(
                """
                INSERT INTO attempts
                    (
                        student_id,
                        telegram_id,
                        candidate_id,
                        status,
                        reason,
                        created_at
                    )
                VALUES
                    (%s, %s, %s, %s, %s, %s)
                """,
                (
                    student_id,
                    telegram_id,
                    candidate_id,
                    "REJECTED",
                    "Voter has already voted",
                    now_utc(),
                ),
            )

            # Count attempts for this Student ID.
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM attempts
                WHERE student_id = %s
                """,
                (student_id,),
            )

            attempts_count = cursor.fetchone()["count"]

            connection.commit()

            if attempts_count > 1:
                await update.message.reply_text(
                    "⚠️ *VOTE NOT RECORDED*\n\n"
                    "This Student ID has already been used to cast "
                    "a valid vote.\n\n"
                    f"Attempt number: *{attempts_count}*\n"
                    "No additional vote was counted.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "⚠️ *VOTE NOT RECORDED*\n\n"
                    "This Student ID has already voted.\n\n"
                    "Only one valid vote is allowed for a normal voter.",
                    parse_mode="Markdown",
                )

            return ConversationHandler.END

        # ----------------------------------------------------
        # NORMAL VALID VOTE
        # ----------------------------------------------------

        cursor.execute(
            """
            INSERT INTO votes
                (
                    student_id,
                    telegram_id,
                    candidate_id,
                    created_at
                )
            VALUES
                (%s, %s, %s, %s)
            """,
            (
                student_id,
                telegram_id,
                candidate_id,
                now_utc(),
            ),
        )

        # Mark voter as having voted.
        cursor.execute(
            """
            UPDATE voters
            SET
                has_voted = TRUE,
                telegram_id = %s
            WHERE student_id = %s
            """,
            (
                telegram_id,
                student_id,
            ),
        )

        # Log successful vote.
        cursor.execute(
            """
            INSERT INTO attempts
                (
                    student_id,
                    telegram_id,
                    candidate_id,
                    status,
                    reason,
                    created_at
                )
            VALUES
                (%s, %s, %s, %s, %s, %s)
            """,
            (
                student_id,
                telegram_id,
                candidate_id,
                "VALID_VOTE",
                "Valid voter",
                now_utc(),
            ),
        )

        connection.commit()

        context.user_data.pop("pending_candidate", None)

        await update.message.reply_text(
            "✅ *VOTE SUCCESSFULLY RECORDED*\n\n"
            f"Candidate: *{candidate_name(candidate_id)}*\n"
            f"Student ID: `{student_id}`\n\n"
            "Your vote has been counted.",
            parse_mode="Markdown",
        )

        return ConversationHandler.END

    except Exception as error:
        connection.rollback()

        print(
            "Database error while processing vote:",
            repr(error),
        )

        await update.message.reply_text(
            "⚠️ A database error occurred.\n\n"
            "Your vote was not confirmed. "
            "Please try again later."
        )

        return ConversationHandler.END

    finally:
        connection.close()


async def cancel_vote(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.pop("pending_candidate", None)

    if update.message:
        await update.message.reply_text(
            "❌ Voting cancelled."
        )

    elif update.callback_query:
        await update.callback_query.answer()

        await update.callback_query.edit_message_text(
            "❌ Voting cancelled."
        )

    return ConversationHandler.END


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def start_election_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    if election_active():
        await update.message.reply_text(
            "⚠️ The election is already active."
        )

        return

    started_at, ends_at = start_election()

    await update.message.reply_text(
        "🟢 *ELECTION STARTED*\n\n"
        f"Duration: *{ELECTION_DURATION_MINUTES} minutes*\n"
        f"Started: `{started_at.isoformat()}`\n"
        f"Ends: `{ends_at.isoformat()}`",
        parse_mode="Markdown",
    )


async def stop_election_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    close_election()

    await update.message.reply_text(
        "🔴 *ELECTION CLOSED*\n\n"
        "Voting is no longer active.",
        parse_mode="Markdown",
    )


# ============================================================
# RESULTS
# ============================================================

async def results_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    # Automatically close expired election.
    active = election_active()

    election = get_election()

    connection = db()

    try:
        cursor = connection.cursor()

        # Total eligible voters.
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM voters
            """
        )

        eligible = cursor.fetchone()["count"]

        # Normal voters marked as voted.
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM voters
            WHERE has_voted = TRUE
            """
        )

        marked_voted = cursor.fetchone()["count"]

        # Total actual valid votes.
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM votes
            """
        )

        total_votes = cursor.fetchone()["count"]

        # Candidate results.
        candidate_results = {}

        for candidate_id in CANDIDATES:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM votes
                WHERE candidate_id = %s
                """,
                (candidate_id,),
            )

            candidate_results[candidate_id] = cursor.fetchone()["count"]

    finally:
        connection.close()

    status = "🟢 OPEN" if active else "🔴 CLOSED"

    text = (
        "📊 *ELECTION DASHBOARD*\n\n"
        f"Status: {status}\n"
        f"Eligible voters: *{eligible}*\n"
        f"Students marked voted: *{marked_voted}*\n"
        f"Total valid votes: *{total_votes}*\n\n"
    )

    for candidate_id, name in CANDIDATES.items():
        votes = candidate_results[candidate_id]

        text += (
            f"{candidate_id}. {name} — *{votes}*\n"
        )

    if election:
        text += "\n"

        if election["started_at"]:
            text += (
                f"Started: `{election['started_at'].isoformat()}`\n"
            )

        if election["ends_at"]:
            text += (
                f"Ends: `{election['ends_at'].isoformat()}`\n"
            )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# ============================================================
# NON-VOTERS
# ============================================================

async def nonvoters_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT student_id, name
            FROM voters
            WHERE has_voted = FALSE
            ORDER BY name
            """
        )

        rows = cursor.fetchall()

    finally:
        connection.close()

    if not rows:
        await update.message.reply_text(
            "✅ All normal voters have voted."
        )

        return

    text = (
        f"👤 *NON-VOTERS*\n\n"
        f"Remaining: *{len(rows)}*\n\n"
    )

    for index, row in enumerate(rows, start=1):
        text += (
            f"{index}. `{row['student_id']}` — "
            f"{row['name']}\n"
        )

    # Telegram message length protection.
    if len(text) <= 4000:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
        )
        return

    # Split long output.
    chunk = ""

    for line in text.splitlines(True):
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(
                chunk,
                parse_mode="Markdown",
            )

            chunk = ""

        chunk += line

    if chunk:
        await update.message.reply_text(
            chunk,
            parse_mode="Markdown",
        )


# ============================================================
# ATTEMPTS
# ============================================================

async def attempts_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT
                student_id,
                telegram_id,
                candidate_id,
                status,
                reason,
                created_at
            FROM attempts
            ORDER BY id DESC
            LIMIT 100
            """
        )

        rows = cursor.fetchall()

    finally:
        connection.close()

    if not rows:
        await update.message.reply_text(
            "📭 No voting attempts recorded."
        )

        return

    text = "📝 *RECENT VOTING ATTEMPTS*\n\n"

    for row in rows:
        candidate = (
            candidate_name(row["candidate_id"])
            if row["candidate_id"] in CANDIDATES
            else "N/A"
        )

        text += (
            f"ID: `{row['student_id'] or 'N/A'}`\n"
            f"Candidate: {candidate}\n"
            f"Status: *{row['status']}*\n"
            f"Reason: {row['reason'] or 'N/A'}\n"
            f"Time: `{row['created_at']}`\n"
            "──────────────\n"
        )

    if len(text) <= 4000:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
        )
        return

    chunk = ""

    for line in text.splitlines(True):
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(
                chunk,
                parse_mode="Markdown",
            )

            chunk = ""

        chunk += line

    if chunk:
        await update.message.reply_text(
            chunk,
            parse_mode="Markdown",
        )


# ============================================================
# LOGS
# ============================================================

async def logs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )

        return

    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT
                id,
                student_id,
                telegram_id,
                candidate_id,
                created_at
            FROM votes
            ORDER BY id DESC
            LIMIT 100
            """
        )

        rows = cursor.fetchall()

    finally:
        connection.close()

    if not rows:
        await update.message.reply_text(
            "📭 No valid votes recorded."
        )

        return

    text = "🗳️ *VALID VOTE LOG*\n\n"

    for row in rows:
        candidate = candidate_name(
            row["candidate_id"]
        )

        text += (
            f"Vote #{row['id']}\n"
            f"Student ID: `{row['student_id']}`\n"
            f"Telegram ID: `{row['telegram_id']}`\n"
            f"Candidate: *{candidate}*\n"
            f"Time: `{row['created_at']}`\n"
            "──────────────\n"
        )

    if len(text) <= 4000:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
        )
        return

    chunk = ""

    for line in text.splitlines(True):
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(
                chunk,
                parse_mode="Markdown",
            )

            chunk = ""

        chunk += line

    if chunk:
        await update.message.reply_text(
            chunk,
            parse_mode="Markdown",
        )


# ============================================================
# CANDIDATE SCORE
# ============================================================

async def myscore_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    telegram_id = update.effective_user.id

    candidate_id = None

    for cid, candidate_telegram_id in CANDIDATE_TELEGRAM_IDS.items():
        if telegram_id == candidate_telegram_id:
            candidate_id = cid
            break

    if candidate_id is None:
        await update.message.reply_text(
            "⛔ This Telegram account is not registered "
            "as a candidate account."
        )

        return

    score = get_candidate_votes(candidate_id)

    await update.message.reply_text(
        "📊 *YOUR LIVE SCORE*\n\n"
        f"Candidate: *{candidate_name(candidate_id)}*\n"
        f"Valid votes: *{score}*",
        parse_mode="Markdown",
    )


async def myid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        f"Your Telegram ID is:\n`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "ℹ️ *AVAILABLE COMMANDS*\n\n"
        "/start — Open election menu\n"
        "/help — Show help\n"
        "/myid — Show your Telegram ID\n"
        "/myscore — Candidate live score\n\n"
        "Admin commands:\n"
        "/start_election\n"
        "/stop_election\n"
        "/results\n"
        "/nonvoters\n"
        "/attempts\n"
        "/logs"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    print(
        "Telegram error:",
        repr(context.error),
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("Initializing Supabase PostgreSQL database...")

    init_database()

    # Start Flask keep-alive server.
    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True,
    )

    web_thread.start()

    print("Flask keep-alive server started.")

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Voting conversation
    # --------------------------------------------------------

    vote_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                start_vote,
                pattern="^vote$",
            )
        ],

        states={
            SELECT_CANDIDATE: [
                CallbackQueryHandler(
                    select_candidate,
                    pattern="^(candidate_[0-9]+|cancel_vote)$",
                )
            ],

            ENTER_STUDENT_ID: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_student_id,
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_vote,
            ),
            CallbackQueryHandler(
                cancel_vote,
                pattern="^cancel_vote$",
            ),
        ],

        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # --------------------------------------------------------
    # Basic commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("myid", myid_command)
    )

    application.add_handler(
        CommandHandler("myscore", myscore_command)
    )

    # --------------------------------------------------------
    # Admin commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start_election",
            start_election_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stop_election",
            stop_election_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "results",
            results_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "nonvoters",
            nonvoters_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "attempts",
            attempts_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "logs",
            logs_command,
        )
    )

    # --------------------------------------------------------
    # Menu callbacks
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            show_rules,
            pattern="^rules$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            show_candidates,
            pattern="^candidates$",
        )
    )

    # Voting conversation must be registered after menu
    # callbacks and before generic message handlers.
    application.add_handler(
        vote_conversation
    )

    # --------------------------------------------------------
    # Error handler
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    print("🚀 Section B Election Bot is running...")

    # --------------------------------------------------------
    # Telegram polling
    # --------------------------------------------------------

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