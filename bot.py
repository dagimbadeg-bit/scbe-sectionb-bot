import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Real election duration
ELECTION_DURATION_HOURS = 13

# Special test/simulation voter.
# This voter is intentionally allowed to cast multiple ballots.
SPECIAL_TEST_STUDENT_ID = "UGR/6094/17"

# Admin Telegram IDs
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

# Candidate Telegram IDs
CANDIDATE_TELEGRAM_IDS = {
    int(x.strip())
    for x in os.getenv("CANDIDATE_TELEGRAM_IDS", "").split(",")
    if x.strip()
}


# ============================================================
# ELECTION STATES
# ============================================================

ELECTION_READY = "READY"
ELECTION_OPEN = "OPEN"
ELECTION_FINAL = "FINAL"


# ============================================================
# CANDIDATES
# ============================================================

CANDIDATES = {
    1: "Biruktawit Zelalem",
    2: "Mihretab Kushe",
    3: "Dagim Badeg",
}


# ============================================================
# ELIGIBLE SECTION B VOTERS
# ============================================================

VOTER_DATA = {
    """
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
}


# ============================================================
# CONVERSATION STATES
# ============================================================

SELECTING_CANDIDATE, ENTERING_STUDENT_ID = range(2)


# ============================================================
# FLASK KEEP-ALIVE SERVER
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Section B Election Bot is running."


@app.route("/health")
def health():
    return "OK"


def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


# ============================================================
# DATABASE
# ============================================================

def db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=15,
    )


def init_database():
    """
    Prepare the database for the final election system.

    IMPORTANT:
    This does NOT automatically delete the old votes table.
    The old votes table may contain simulation data.
    """

    with db() as conn:
        with conn.cursor() as cur:

            # ------------------------------------------------
            # Election state columns
            # ------------------------------------------------

            cur.execute("""
                ALTER TABLE election
                ADD COLUMN IF NOT EXISTS status TEXT
            """)

            cur.execute("""
                ALTER TABLE election
                ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ
            """)

            # ------------------------------------------------
            # Create election row if it doesn't exist
            # ------------------------------------------------

            cur.execute("""
                INSERT INTO election
                    (id, started_at, ends_at, active, status)
                VALUES
                    (1, NULL, NULL, FALSE, %s)
                ON CONFLICT (id) DO NOTHING
            """, (ELECTION_READY,))

            # ------------------------------------------------
            # Repair old rows that don't have a status
            # ------------------------------------------------

            cur.execute("""
                UPDATE election
                SET status =
                    CASE
                        WHEN active = TRUE THEN %s
                        ELSE %s
                    END
                WHERE id = 1
                  AND status IS NULL
            """, (ELECTION_OPEN, ELECTION_READY))

            # ------------------------------------------------
            # Anonymous ballot table
            #
            # IMPORTANT:
            # There is deliberately NO:
            #   student_id
            #   telegram_id
            #
            # Only the candidate choice is stored.
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ballots (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    candidate_id INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)

            # ------------------------------------------------
            # Remove candidate_id from attempts if it exists.
            #
            # Attempts can identify a voter for eligibility
            # auditing, but must NOT reveal their candidate.
            # ------------------------------------------------

            cur.execute("""
                ALTER TABLE attempts
                DROP COLUMN IF EXISTS candidate_id
            """)

            # ------------------------------------------------
            # Ensure voters table exists
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS voters (
                    student_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    telegram_id BIGINT,
                    has_voted BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)

            # ------------------------------------------------
            # Ensure attempts table exists
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS attempts (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    student_id TEXT,
                    telegram_id BIGINT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)

            # ------------------------------------------------
            # Insert eligible voters
            # ------------------------------------------------

            for student_id, name in VOTER_DATA.items():
                cur.execute("""
                    INSERT INTO voters
                        (student_id, name)
                    VALUES
                        (%s, %s)
                    ON CONFLICT (student_id)
                    DO UPDATE SET name = EXCLUDED.name
                """, (student_id, name))

        conn.commit()


# ============================================================
# ELECTION INFORMATION
# ============================================================

def get_election():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    started_at,
                    ends_at,
                    active,
                    status,
                    finalized_at
                FROM election
                WHERE id = 1
            """)
            return cur.fetchone()


def election_active():
    """
    Returns True only while the election is genuinely OPEN.

    If the 13-hour deadline has passed, this function permanently
    changes the election state to FINAL.
    """

    now = datetime.now(timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    status,
                    active,
                    ends_at
                FROM election
                WHERE id = 1
                FOR UPDATE
            """)

            election = cur.fetchone()

            if not election:
                return False

            status = election["status"]
            ends_at = election["ends_at"]

            # Already final
            if status == ELECTION_FINAL:
                return False

            # Not open
            if status != ELECTION_OPEN or not election["active"]:
                return False

            # Deadline reached
            if ends_at and now >= ends_at:

                cur.execute("""
                    UPDATE election
                    SET
                        active = FALSE,
                        status = %s,
                        finalized_at = COALESCE(finalized_at, %s)
                    WHERE id = 1
                """, (ELECTION_FINAL, now))

                conn.commit()

                return False

            return True


# ============================================================
# START ELECTION
# ============================================================

def start_election():
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(hours=ELECTION_DURATION_HOURS)

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    status,
                    active
                FROM election
                WHERE id = 1
                FOR UPDATE
            """)

            election = cur.fetchone()

            if not election:
                return False, "Election record does not exist."

            status = election["status"]

            # ------------------------------------------------
            # FINAL elections can NEVER be reopened.
            # ------------------------------------------------

            if status == ELECTION_FINAL:
                return (
                    False,
                    "The election is already FINAL and cannot be reopened."
                )

            # Already running
            if status == ELECTION_OPEN:
                return False, "The election is already open."

            # Start election
            cur.execute("""
                UPDATE election
                SET
                    started_at = %s,
                    ends_at = %s,
                    active = TRUE,
                    status = %s,
                    finalized_at = NULL
                WHERE id = 1
            """, (
                now,
                ends_at,
                ELECTION_OPEN,
            ))

        conn.commit()

    return True, ends_at


# ============================================================
# FINALIZE ELECTION
# ============================================================

def finalize_election():
    now = datetime.now(timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    status,
                    active
                FROM election
                WHERE id = 1
                FOR UPDATE
            """)

            election = cur.fetchone()

            if not election:
                return False, "Election record does not exist."

            status = election["status"]

            if status == ELECTION_FINAL:
                return False, "The election is already FINAL."

            if status != ELECTION_OPEN:
                return False, "The election is not currently open."

            cur.execute("""
                UPDATE election
                SET
                    active = FALSE,
                    status = %s,
                    finalized_at = %s
                WHERE id = 1
            """, (
                ELECTION_FINAL,
                now,
            ))

        conn.commit()

    return True, now


# ============================================================
# CANDIDATE RESULTS
# ============================================================

def get_candidate_votes():

    results = {
        candidate_id: 0
        for candidate_id in CANDIDATES
    }

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    candidate_id,
                    COUNT(*) AS vote_count
                FROM ballots
                GROUP BY candidate_id
            """)

            rows = cur.fetchall()

            for row in rows:
                candidate_id = row["candidate_id"]

                if candidate_id in results:
                    results[candidate_id] = row["vote_count"]

    return results


def get_total_ballots():

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM ballots
            """)

            row = cur.fetchone()

            return row["total"]


# ============================================================
# /START
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    election = get_election()

    status = election["status"]

    if status == ELECTION_OPEN:
        status_text = "🟢 OPEN"
    elif status == ELECTION_FINAL:
        status_text = "🔒 FINAL"
    else:
        status_text = "⚪ READY"

    text = (
        "🗳️ SECTION B ELECTION\n\n"
        f"Election status: {status_text}\n\n"

        "Candidates:\n"
        "1️⃣ Biruktawit Zelalem\n"
        "2️⃣ Mihretab Kushe\n"
        "3️⃣ Dagim Badeg\n\n"

        "📌 Rules:\n"
        "• Only eligible Section B students may vote.\n"
        "• Each normal voter may cast one valid ballot.\n"
        "• The designated test voter UGR/6094/17 is intentionally "
        "allowed multiple valid ballots.\n"
        "• Student ID is used for eligibility verification.\n"
        "• Candidate choices are stored separately from voter identity.\n"
        "• Invalid Student IDs do not create ballots.\n"
        f"• The election period is {ELECTION_DURATION_HOURS} hours.\n"
        "• When the election ends or is finalized, it becomes FINAL "
        "and cannot be reopened.\n\n"

        "Use /vote to cast your ballot."
    )

    await update.message.reply_text(text)


# ============================================================
# /VOTE
# ============================================================

async def start_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # Quick check for user feedback
    if not election_active():
        election = get_election()

        if election and election["status"] == ELECTION_FINAL:
            await update.message.reply_text(
                "🔒 The election is FINAL.\n\n"
                "Voting is closed and the election cannot be reopened."
            )
        else:
            await update.message.reply_text(
                "⏳ The election is not currently open."
            )

        return ConversationHandler.END

    keyboard = [
        [
            InlineKeyboardButton(
                "1️⃣ Biruktawit Zelalem",
                callback_data="candidate_1",
            )
        ],
        [
            InlineKeyboardButton(
                "2️⃣ Mihretab Kushe",
                callback_data="candidate_2",
            )
        ],
        [
            InlineKeyboardButton(
                "3️⃣ Dagim Badeg",
                callback_data="candidate_3",
            )
        ],
    ]

    await update.message.reply_text(
        "🗳️ Select your candidate:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    return SELECTING_CANDIDATE


# ============================================================
# CANDIDATE SELECTION
# ============================================================

async def select_candidate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    # Extract candidate ID
    candidate_id = int(query.data.split("_")[1])

    if candidate_id not in CANDIDATES:
        await query.edit_message_text(
            "❌ Invalid candidate selection."
        )
        return ConversationHandler.END

    context.user_data["pending_candidate"] = candidate_id

    await query.edit_message_text(
        "Please enter your Section B Student ID.\n\n"
        "Example:\n"
        "UGR/1234/17"
    )

    return ENTERING_STUDENT_ID


# ============================================================
# RECEIVE STUDENT ID
# ============================================================

async def receive_student_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    student_id = update.message.text.strip()
    telegram_id = update.effective_user.id

    candidate_id = context.user_data.get("pending_candidate")

    if candidate_id not in CANDIDATES:
        await update.message.reply_text(
            "❌ Your voting session expired. Please use /vote again."
        )

        context.user_data.clear()

        return ConversationHandler.END

    now = datetime.now(timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:

            # =================================================
            # AUTHORITATIVE ELECTION CHECK
            #
            # Lock election row so that a ballot cannot be
            # inserted after the election becomes FINAL.
            # =================================================

            cur.execute("""
                SELECT
                    status,
                    active,
                    ends_at
                FROM election
                WHERE id = 1
                FOR UPDATE
            """)

            election = cur.fetchone()

            if not election:
                await update.message.reply_text(
                    "❌ Election information is unavailable."
                )
                return ConversationHandler.END

            status = election["status"]
            ends_at = election["ends_at"]

            # -------------------------------------------------
            # Already final
            # -------------------------------------------------

            if status == ELECTION_FINAL:

                conn.rollback()

                await update.message.reply_text(
                    "🔒 The election is FINAL.\n\n"
                    "Your ballot was not recorded."
                )

                context.user_data.clear()

                return ConversationHandler.END

            # -------------------------------------------------
            # Election not open
            # -------------------------------------------------

            if status != ELECTION_OPEN or not election["active"]:

                conn.rollback()

                await update.message.reply_text(
                    "⏳ The election is not currently open."
                )

                context.user_data.clear()

                return ConversationHandler.END

            # -------------------------------------------------
            # 13-hour deadline reached
            # -------------------------------------------------

            if ends_at and now >= ends_at:

                cur.execute("""
                    UPDATE election
                    SET
                        active = FALSE,
                        status = %s,
                        finalized_at = COALESCE(finalized_at, %s)
                    WHERE id = 1
                """, (
                    ELECTION_FINAL,
                    now,
                ))

                conn.commit()

                await update.message.reply_text(
                    "🔒 The 13-hour election period has ended.\n\n"
                    "The election is now FINAL and your ballot "
                    "was not recorded."
                )

                context.user_data.clear()

                return ConversationHandler.END

            # =================================================
            # VALIDATE STUDENT ID
            # =================================================

            cur.execute("""
                SELECT
                    student_id,
                    name,
                    has_voted
                FROM voters
                WHERE student_id = %s
                FOR UPDATE
            """, (student_id,))

            voter = cur.fetchone()

            # -------------------------------------------------
            # INVALID STUDENT ID
            # -------------------------------------------------

            if not voter:

                cur.execute("""
                    INSERT INTO attempts (
                        student_id,
                        telegram_id,
                        status,
                        reason,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    student_id,
                    telegram_id,
                    "REJECTED",
                    "Invalid Student ID",
                    now,
                ))

                conn.commit()

                await update.message.reply_text(
                    "❌ Invalid Student ID.\n\n"
                    "No ballot was recorded."
                )

                context.user_data.clear()

                return ConversationHandler.END

            # =================================================
            # SPECIAL TEST VOTER
            #
            # This voter is intentionally allowed multiple
            # successful ballots.
            # =================================================

            if student_id == SPECIAL_TEST_STUDENT_ID:

                # ---------------------------------------------
                # IMPORTANT:
                # The ballot contains ONLY candidate_id.
                #
                # No Student ID.
                # No Telegram ID.
                # ---------------------------------------------

                cur.execute("""
                    INSERT INTO ballots (
                        candidate_id,
                        created_at
                    )
                    VALUES (%s, %s)
                """, (
                    candidate_id,
                    now,
                ))

                # Store Telegram ID only for voter/audit
                # purposes. It is NOT connected to the ballot.
                cur.execute("""
                    UPDATE voters
                    SET telegram_id = %s
                    WHERE student_id = %s
                """, (
                    telegram_id,
                    student_id,
                ))

                cur.execute("""
                    INSERT INTO attempts (
                        student_id,
                        telegram_id,
                        status,
                        reason,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    student_id,
                    telegram_id,
                    "VALID_TEST_VOTE",
                    "Special test voter - multiple ballots allowed",
                    now,
                ))

                conn.commit()

                context.user_data.clear()

                await update.message.reply_text(
                    "✅ VOTE SUCCESSFULLY RECORDED.\n\n"
                    "Your ballot has been recorded anonymously."
                )

                return ConversationHandler.END

            # =================================================
            # NORMAL VOTER — ALREADY VOTED
            # =================================================

            if voter["has_voted"]:

                cur.execute("""
                    INSERT INTO attempts (
                        student_id,
                        telegram_id,
                        status,
                        reason,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    student_id,
                    telegram_id,
                    "REJECTED",
                    "Voter has already cast a ballot",
                    now,
                ))

                conn.commit()

                await update.message.reply_text(
                    "⚠️ A ballot has already been recorded for "
                    "this Student ID.\n\n"
                    "No additional ballot was recorded."
                )

                context.user_data.clear()

                return ConversationHandler.END

            # =================================================
            # NORMAL VALID VOTE
            # =================================================

            # Anonymous ballot:
            # NO student_id
            # NO telegram_id
            cur.execute("""
                INSERT INTO ballots (
                    candidate_id,
                    created_at
                )
                VALUES (%s, %s)
            """, (
                candidate_id,
                now,
            ))

            # Mark voter as having voted.
            # This does NOT contain candidate information.
            cur.execute("""
                UPDATE voters
                SET
                    has_voted = TRUE,
                    telegram_id = %s
                WHERE student_id = %s
            """, (
                telegram_id,
                student_id,
            ))

            # Audit attempt WITHOUT candidate information.
            cur.execute("""
                INSERT INTO attempts (
                    student_id,
                    telegram_id,
                    status,
                    reason,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (
                student_id,
                telegram_id,
                "VALID_VOTE",
                "Ballot recorded anonymously",
                now,
            ))

        conn.commit()

    context.user_data.clear()

    await update.message.reply_text(
        "✅ VOTE SUCCESSFULLY RECORDED.\n\n"
        "Your ballot has been recorded anonymously."
    )

    return ConversationHandler.END


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# /START_ELECTION
# ============================================================

async def start_election_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    success, result = start_election()

    if not success:
        await update.message.reply_text(
            f"❌ {result}"
        )
        return

    ends_at = result

    await update.message.reply_text(
        "🟢 SECTION B ELECTION STARTED\n\n"
        f"Duration: {ELECTION_DURATION_HOURS} hours\n"
        f"Ends at: {ends_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "The election will automatically become FINAL "
        "when the 13-hour period ends.\n\n"
        "Once FINAL, it cannot be reopened."
    )


# ============================================================
# /FINALIZE_ELECTION
# /STOP_ELECTION
# ============================================================

async def finalize_election_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    success, result = finalize_election()

    if not success:
        await update.message.reply_text(
            f"❌ {result}"
        )
        return

    finalized_at = result

    await update.message.reply_text(
        "🔒 ELECTION FINALIZED\n\n"
        f"Finalized at: "
        f"{finalized_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "The election can no longer accept votes "
        "and cannot be reopened."
    )


# ============================================================
# /RESULTS
# ============================================================

async def results_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    # Automatically finalize if the deadline has passed.
    election_active()

    election = get_election()
    results = get_candidate_votes()
    total = get_total_ballots()

    status = election["status"]

    if status == ELECTION_FINAL:
        status_text = "🔒 FINAL"
    elif status == ELECTION_OPEN:
        status_text = "🟢 OPEN"
    else:
        status_text = "⚪ READY"

    text = (
        "🗳️ SECTION B ELECTION RESULTS\n\n"
        f"Status: {status_text}\n\n"
    )

    for candidate_id, name in CANDIDATES.items():
        text += (
            f"{candidate_id}️⃣ {name}: "
            f"{results.get(candidate_id, 0)}\n"
        )

    text += (
        f"\nTotal anonymous ballots: {total}\n"
    )

    if status == ELECTION_FINAL:
        text += (
            "\n🔒 These are the final results."
        )

    await update.message.reply_text(text)


# ============================================================
# /NONVOTERS
# ============================================================

async def nonvoters_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    student_id,
                    name
                FROM voters
                WHERE has_voted = FALSE
                ORDER BY student_id
            """)

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "✅ All eligible normal voters have voted."
        )
        return

    text = "📋 NON-VOTERS\n\n"

    for row in rows:
        text += (
            f"{row['student_id']} — "
            f"{row['name']}\n"
        )

    await update.message.reply_text(text)


# ============================================================
# /ATTEMPTS
# ============================================================

async def attempts_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    student_id,
                    telegram_id,
                    status,
                    reason,
                    created_at
                FROM attempts
                ORDER BY created_at DESC
            """)

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "No voting attempts recorded."
        )
        return

    text = "📋 VOTING ATTEMPTS\n\n"

    for row in rows:

        student_id = row["student_id"] or "N/A"
        telegram_id = row["telegram_id"] or "N/A"

        text += (
            f"Student ID: {student_id}\n"
            f"Telegram ID: {telegram_id}\n"
            f"Status: {row['status']}\n"
            f"Reason: {row['reason'] or 'N/A'}\n"
            f"Time: {row['created_at']}\n"
            "──────────────\n"
        )

    await update.message.reply_text(text)


# ============================================================
# /LOGS
# ============================================================

async def logs_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Admin only."
        )
        return

    total = get_total_ballots()

    await update.message.reply_text(
        "🗳️ BALLOT AUDIT\n\n"
        "Ballots are stored anonymously.\n\n"
        "The system intentionally does not provide "
        "a voter → candidate mapping.\n\n"
        f"Total anonymous ballots: {total}\n\n"
        "Use /results for aggregate candidate totals."
    )


# ============================================================
# /MYSCORE
# ============================================================

async def myscore_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    telegram_id = update.effective_user.id

    candidate_id = None

    for cid, candidate_name in CANDIDATES.items():

        # Candidate Telegram IDs are optional.
        # If configured, candidate can see aggregate score.
        if telegram_id in CANDIDATE_TELEGRAM_IDS:
            candidate_id = cid
            break

    if candidate_id is None:

        await update.message.reply_text(
            "❌ This command is available only to configured candidates."
        )

        return

    results = get_candidate_votes()

    await update.message.reply_text(
        f"📊 YOUR CURRENT SCORE\n\n"
        f"{CANDIDATES[candidate_id]}\n"
        f"Votes: {results.get(candidate_id, 0)}"
    )


# ============================================================
# /MYID
# ============================================================

async def myid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        f"Your Telegram ID is:\n\n"
        f"{update.effective_user.id}"
    )


# ============================================================
# /HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "ℹ️ SECTION B ELECTION BOT\n\n"

        "/start — Election information\n"
        "/vote — Cast your ballot\n"
        "/myid — Show your Telegram ID\n"
        "/help — Show this help\n\n"

        "Admin commands:\n"
        "/start_election — Start the 13-hour election\n"
        "/finalize_election — Permanently finalize election\n"
        "/stop_election — Same as finalize_election\n"
        "/results — Aggregate results\n"
        "/nonvoters — Eligible voters who have not voted\n"
        "/attempts — Voting validation attempts\n"
        "/logs — Anonymous ballot audit\n\n"

        "Candidate command:\n"
        "/myscore — Show aggregate candidate score\n\n"

        "🔒 Once the election becomes FINAL, "
        "it cannot be reopened."
    )

    await update.message.reply_text(text)


# ============================================================
# CANCEL
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Voting cancelled."
    )

    return ConversationHandler.END


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:
        raise RuntimeError("TOKEN environment variable is missing.")

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing."
        )

    # Initialize database
    init_database()

    # Start Flask keep-alive server
    threading.Thread(
        target=run_flask,
        daemon=True,
    ).start()

    print("🚀 Section B Election Bot is running...")

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
            CommandHandler("vote", start_vote),
        ],

        states={

            SELECTING_CANDIDATE: [
                CallbackQueryHandler(
                    select_candidate,
                    pattern=r"^candidate_[1-3]$",
                )
            ],

            ENTERING_STUDENT_ID: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_student_id,
                )
            ],
        },

        fallbacks=[
            CommandHandler("cancel", cancel_command),
        ],

        per_message=False,
    )

    application.add_handler(vote_conversation)

    # --------------------------------------------------------
    # General commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler("start", start_command)
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
            "finalize_election",
            finalize_election_command,
        )
    )

    # Backward-compatible alias
    application.add_handler(
        CommandHandler(
            "stop_election",
            finalize_election_command,
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
    # Start polling
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