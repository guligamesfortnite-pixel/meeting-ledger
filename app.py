import os
import secrets
import base64
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import database as db
from ai_service import summarize_transcript
from email_service import send_reset_email, send_reminder_email, is_configured as email_is_configured, base_url as email_base_url
from export_service import build_meeting_pdf, build_meeting_docx
from translations import t, is_rtl, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE

app = Flask(__name__)
# In production, set SECRET_KEY as an environment variable instead of
# generating a fresh one on every restart (that would log everyone out
# on each deploy). A random fallback is fine for local development.
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Shared secret a live-notes bot integration (e.g. Recall.ai) would send
# back on its webhook call, so random people on the internet can't post
# fake meetings into someone's account. Set this as a real env var once
# you wire up a bot provider.
LIVE_NOTES_WEBHOOK_SECRET = os.environ.get("LIVE_NOTES_WEBHOOK_SECRET")

# Shared secret for the reminder-check endpoint, so it can be triggered
# safely by an external cron job in production. See README.
REMINDER_CHECK_SECRET = os.environ.get("REMINDER_CHECK_SECRET")

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024  # 5 MB per file, keeps things reasonable stored in the database
REMINDER_WINDOW_MINUTES = 60  # ask "is this still happening?" 1 hour before start

# Protects the /admin/codes page where redemption codes get created.
# Set this as a real env var once deployed - without it, the admin page
# refuses to unlock for anyone.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")

LOGIN_RATE_LIMIT = 8          # max attempts...
LOGIN_RATE_LIMIT_MINUTES = 15  # ...within this many minutes, per email+IP

db.init_db()


# ---------------- auth helpers ----------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    if "user_id" not in session:
        return None
    return db.get_user_by_id(session["user_id"])


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def _try_redeem_pending_code(user_id):
    """If the person tried to redeem a code before logging in, it's
    parked in session['pending_code'] - apply it now that we know who
    they are, then clear it either way so it doesn't linger."""
    code = session.pop("pending_code", None)
    if not code:
        return
    code_row = db.get_redeemable_code(code)
    if code_row:
        db.redeem_code_for_user(code_row["id"], user_id)
        flash(tr("redeem_success"))
    else:
        flash(tr("redeem_invalid"))


def check_csrf():
    token = request.form.get("csrf_token", "")
    if not token or token != session.get("csrf_token"):
        abort(400, "Invalid or missing CSRF token. Please refresh the page and try again.")


# ---------------- language helpers ----------------

def current_language():
    """Priority: logged-in user's saved preference, then a language they
    picked before logging in (stored in the session), then their
    browser's preferred language, then English."""
    user = current_user()
    if user is not None and user["language"] in SUPPORTED_LANGUAGES:
        return user["language"]
    if session.get("lang") in SUPPORTED_LANGUAGES:
        return session["lang"]
    best = request.accept_languages.best_match(SUPPORTED_LANGUAGES.keys())
    return best or DEFAULT_LANGUAGE


def tr(key):
    return t(key, current_language())


app.jinja_env.globals["csrf_token"] = get_csrf_token
app.jinja_env.globals["t"] = tr
app.jinja_env.globals["SUPPORTED_LANGUAGES"] = SUPPORTED_LANGUAGES


@app.context_processor
def inject_globals():
    lang = current_language()
    user = current_user()
    pending_notifications = db.list_pending_notifications_for_user(user["id"]) if user else []
    return {
        "user": user,
        "current_lang": lang,
        "text_dir": "rtl" if is_rtl(lang) else "ltr",
        "pending_notifications": pending_notifications,
    }


# ---------------- language switching ----------------

@app.route("/set-language/<lang>")
def set_language(lang):
    if lang not in SUPPORTED_LANGUAGES:
        abort(404)
    if "user_id" in session:
        db.update_user_language(session["user_id"], lang)
    else:
        session["lang"] = lang
    return redirect(request.referrer or url_for("index"))


# ---------------- auth routes ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        check_csrf()

        identifier = f"register:{request.remote_addr}"
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_RATE_LIMIT_MINUTES)).isoformat()
        if db.count_recent_login_attempts(identifier, cutoff) >= LOGIN_RATE_LIMIT:
            flash(tr("too_many_attempts"))
            return render_template("register.html")
        db.record_login_attempt(identifier)

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        access_code = request.form.get("access_code", "").strip()

        if not name or not email or not password:
            flash("Fill in your name, email, and password.")
            return render_template("register.html")
        if not request.form.get("agree_terms"):
            flash("Please agree to the Terms and Privacy Policy to continue.")
            return render_template("register.html")
        if len(password) < 8:
            flash("Password needs to be at least 8 characters.")
            return render_template("register.html")
        if db.get_user_by_email(email):
            flash("An account with that email already exists.")
            return render_template("register.html")

        user_id = db.create_user(name, email, generate_password_hash(password), current_language())
        pending_code = session.get("pending_code")
        session.clear()
        session["user_id"] = user_id

        if access_code:
            code_row = db.get_redeemable_code(access_code)
            if code_row:
                db.redeem_code_for_user(code_row["id"], user_id)
                flash(tr("redeem_success"))
            else:
                flash(tr("redeem_invalid"))
        elif pending_code:
            session["pending_code"] = pending_code
            _try_redeem_pending_code(user_id)

        return redirect(url_for("dashboard"))

    return render_template(
        "register.html", prefill_code=request.args.get("code", "") or session.get("pending_code", "")
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        identifier = f"login:{email}:{request.remote_addr}"
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_RATE_LIMIT_MINUTES)).isoformat()
        if db.count_recent_login_attempts(identifier, cutoff) >= LOGIN_RATE_LIMIT:
            flash(tr("too_many_attempts"))
            return render_template("login.html")
        db.record_login_attempt(identifier)

        user = db.get_user_by_email(email)

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.")
            return render_template("login.html")

        pending_code = session.get("pending_code")
        session.clear()
        session["user_id"] = user["id"]
        if pending_code:
            session["pending_code"] = pending_code
        _try_redeem_pending_code(user["id"])
        return redirect(request.args.get("next") or url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        user = db.get_user_by_email(email)

        if user is not None and email_is_configured():
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.create_password_reset(user["id"], token, expires_at)
            reset_url = f"{email_base_url()}{url_for('reset_password', token=token)}"
            send_reset_email(email, reset_url)
        elif user is not None and not email_is_configured():
            flash(tr("reset_email_not_configured"))
            return render_template("forgot_password.html")

        # Same message whether or not the account exists - otherwise this
        # page could be used to check which emails have accounts.
        flash(tr("reset_sent"))
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    now_iso_value = datetime.now(timezone.utc).isoformat()
    reset_row = db.get_valid_password_reset(token, now_iso_value)

    if reset_row is None:
        flash(tr("reset_invalid"))
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        check_csrf()
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Password needs to be at least 8 characters.")
            return render_template("reset_password.html", token=token)

        db.update_user_password(reset_row["user_id"], generate_password_hash(password))
        db.mark_password_reset_used(reset_row["id"])
        flash(tr("reset_success"))
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = current_user()

    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")

        if action == "update_profile":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            if not name or not email:
                flash("Name and email can't be empty.")
            else:
                existing = db.get_user_by_email(email)
                if existing and existing["id"] != user["id"]:
                    flash("Another account already uses that email.")
                else:
                    db.update_user_name(user["id"], name)
                    db.update_user_email(user["id"], email)
                    flash("Profile updated.")

        elif action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            if not check_password_hash(user["password_hash"], current_password):
                flash("Current password is incorrect.")
            elif len(new_password) < 8:
                flash("New password needs to be at least 8 characters.")
            else:
                db.update_user_password(user["id"], generate_password_hash(new_password))
                flash("Password updated.")

        return redirect(url_for("account"))

    return render_template("account.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# ---------------- app routes ----------------

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    query = request.args.get("q", "").strip()
    if query:
        meetings = db.search_meetings_for_user(session["user_id"], query)
    else:
        meetings = db.list_meetings_with_stats_for_user(session["user_id"])
    return render_template("dashboard.html", meetings=meetings, query=query)


def _create_meeting_with_ai(user_id, title, meeting_date, attendees, transcript, language):
    summary, action_items = summarize_transcript(transcript, title, language)
    meeting_id = db.create_meeting(user_id, title, meeting_date, attendees, transcript, summary)
    for item in action_items:
        db.add_action_item(meeting_id, item.get("description", ""), item.get("owner", "unassigned"))
    return meeting_id


@app.route("/meetings/new", methods=["GET", "POST"])
@login_required
def new_meeting():
    if request.method == "POST":
        check_csrf()
        title = request.form.get("title", "").strip()
        meeting_date = request.form.get("meeting_date", "").strip()
        attendees = request.form.get("attendees", "").strip()
        transcript = request.form.get("transcript", "").strip()

        if not title or not meeting_date:
            flash("A title and date are required.")
            return render_template("add_meeting.html", prefill_title=title, prefill_attendees=attendees)

        meeting_id = _create_meeting_with_ai(
            session["user_id"], title, meeting_date, attendees, transcript, current_language()
        )
        return redirect(url_for("meeting_detail", meeting_id=meeting_id))

    return render_template(
        "add_meeting.html",
        prefill_title=request.args.get("title", ""),
        prefill_attendees=request.args.get("attendees", ""),
    )


@app.route("/meetings/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        # Either it doesn't exist, or it belongs to someone else's
        # account - either way, this user doesn't get to see it.
        abort(404)
    action_items = db.list_action_items_for_meeting(meeting_id)
    return render_template("meeting_detail.html", meeting=meeting, action_items=action_items)


@app.route("/meetings/<int:meeting_id>/export.pdf")
@login_required
def export_meeting_pdf(meeting_id):
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    action_items = db.list_action_items_for_meeting(meeting_id)
    pdf_bytes = build_meeting_pdf(meeting, action_items)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{meeting["title"]}.pdf"'},
    )


@app.route("/meetings/<int:meeting_id>/export.docx")
@login_required
def export_meeting_docx(meeting_id):
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    action_items = db.list_action_items_for_meeting(meeting_id)
    docx_bytes = build_meeting_docx(meeting, action_items)
    return Response(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{meeting["title"]}.docx"'},
    )


@app.route("/my-tasks")
@login_required
def my_tasks():
    items = db.list_open_action_items_for_user(session["user_id"])
    return render_template("my_tasks.html", items=items)


@app.route("/meetings/<int:meeting_id>/action-items/<int:action_item_id>/toggle", methods=["POST"])
@login_required
def toggle_action_item(meeting_id, action_item_id):
    check_csrf()
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    db.toggle_action_item(action_item_id, meeting_id)
    if request.form.get("redirect_to") == "my_tasks":
        return redirect(url_for("my_tasks"))
    return redirect(url_for("meeting_detail", meeting_id=meeting_id))


@app.route("/meetings/<int:meeting_id>/action-items/<int:action_item_id>/delete", methods=["POST"])
@login_required
def delete_action_item_route(meeting_id, action_item_id):
    check_csrf()
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    db.delete_action_item(action_item_id, meeting_id)
    return redirect(url_for("meeting_detail", meeting_id=meeting_id))


@app.route("/meetings/<int:meeting_id>/action-items/new", methods=["POST"])
@login_required
def add_action_item_route(meeting_id):
    check_csrf()
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    description = request.form.get("description", "").strip()
    owner = request.form.get("owner", "").strip() or "unassigned"
    if description:
        db.add_action_item(meeting_id, description, owner)
    return redirect(url_for("meeting_detail", meeting_id=meeting_id))


@app.route("/meetings/<int:meeting_id>/edit", methods=["GET", "POST"])
@login_required
def edit_meeting(meeting_id):
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)

    if request.method == "POST":
        check_csrf()
        title = request.form.get("title", "").strip()
        meeting_date = request.form.get("meeting_date", "").strip()
        attendees = request.form.get("attendees", "").strip()
        transcript = request.form.get("transcript", "").strip()
        summary = request.form.get("summary", "").strip()

        if not title or not meeting_date:
            flash("A title and date are required.")
            return render_template("edit_meeting.html", meeting=meeting)

        if request.form.get("regenerate_summary"):
            summary, _ = summarize_transcript(transcript, title, current_language())

        db.update_meeting(meeting_id, session["user_id"], title, meeting_date, attendees, transcript, summary)
        return redirect(url_for("meeting_detail", meeting_id=meeting_id))

    return render_template("edit_meeting.html", meeting=meeting)


@app.route("/meetings/<int:meeting_id>/delete", methods=["POST"])
@login_required
def delete_meeting_route(meeting_id):
    check_csrf()
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    db.delete_meeting(meeting_id, session["user_id"])
    return redirect(url_for("dashboard"))


# ---------------- prep ----------------

def _split_names(text):
    return {name.strip().lower() for name in text.split(",") if name.strip()}


def _scheduled_utc_datetime(scheduled_row):
    """scheduled_at is stored as the naive local date/time exactly as the
    person typed it (e.g. '2026-08-04T14:00'), plus a separate timezone
    offset captured from their browser at scheduling time. This combines
    the two into a real UTC-aware datetime for comparisons and calendar
    export - naive string comparison alone can't account for timezone."""
    naive = datetime.strptime(scheduled_row["scheduled_at"][:16], "%Y-%m-%dT%H:%M")
    offset_minutes = scheduled_row["timezone_offset_minutes"] or 0
    return (naive + timedelta(minutes=offset_minutes)).replace(tzinfo=timezone.utc)


def _add_months(naive_dt, months):
    """timedelta doesn't support months (they're not a fixed length), so
    this does simple calendar-aware month arithmetic - clamping the day
    if the target month is shorter (e.g. Jan 31 + 1 month -> Feb 28)."""
    month_index = naive_dt.month - 1 + months
    year = naive_dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(naive_dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                             31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return naive_dt.replace(year=year, month=month, day=day)


def _next_occurrence(scheduled_at_str, repeat, n):
    """Returns the naive local datetime string for the nth future
    occurrence (n=1 is the first repeat after the original)."""
    naive = datetime.strptime(scheduled_at_str[:16], "%Y-%m-%dT%H:%M")
    if repeat == "weekly":
        result = naive + timedelta(weeks=n)
    else:  # monthly
        result = _add_months(naive, n)
    return result.strftime("%Y-%m-%dT%H:%M")


@app.route("/prep", methods=["GET", "POST"])
@login_required
def prep():
    context = None
    entered_attendees = ""

    if request.method == "POST":
        check_csrf()
        entered_attendees = request.form.get("attendees", "").strip()
        wanted_names = _split_names(entered_attendees)

        if wanted_names:
            all_meetings = db.list_meetings_for_user(session["user_id"])
            match = None
            for m in all_meetings:
                meeting_names = _split_names(m["attendees"] or "")
                if meeting_names & wanted_names:
                    match = m
                    break  # already sorted newest-first by the query

            if match is not None:
                open_items = [
                    item for item in db.list_action_items_for_meeting(match["id"])
                    if not item["done"]
                ]
                context = {"meeting": match, "open_items": open_items}

    return render_template("prep.html", context=context, entered_attendees=entered_attendees)


# ---------------- pricing & redemption codes ----------------

PRICING_TIERS = [
    {"key": "plan_base", "price": 49},
    {"key": "plan_5", "price": 169},
    {"key": "plan_10", "price": 279},
]


@app.route("/pricing")
def pricing():
    return render_template("pricing.html", tiers=PRICING_TIERS)


@app.route("/redeem", methods=["POST"])
def redeem():
    check_csrf()
    code = request.form.get("code", "").strip()

    if "user_id" not in session:
        session["pending_code"] = code
        flash(tr("redeem_login_required"))
        return redirect(url_for("login", next=url_for("pricing")))

    code_row = db.get_redeemable_code(code)
    if code_row is None:
        flash(tr("redeem_invalid"))
    else:
        db.redeem_code_for_user(code_row["id"], session["user_id"])
        flash(tr("redeem_success"))

    return redirect(url_for("pricing"))


@app.route("/admin/codes", methods=["GET", "POST"])
def admin_codes():
    if not ADMIN_SECRET:
        abort(404)

    if session.get("is_admin") is not True:
        if request.method == "POST" and request.form.get("admin_secret"):
            check_csrf()
            if request.form.get("admin_secret") == ADMIN_SECRET:
                session["is_admin"] = True
                return redirect(url_for("admin_codes"))
            flash("Incorrect secret.")
        return render_template("admin_unlock.html")

    if request.method == "POST":
        check_csrf()
        new_code = request.form.get("new_code", "").strip() or secrets.token_hex(4).upper()
        description = request.form.get("description", "").strip()
        max_uses = request.form.get("max_uses", "1").strip()
        try:
            max_uses_int = max(1, int(max_uses))
        except ValueError:
            max_uses_int = 1
        db.create_redemption_code(new_code, description, max_uses_int)
        return redirect(url_for("admin_codes"))

    codes = db.list_redemption_codes()
    return render_template("admin_codes.html", codes=codes)


# ---------------- scheduled meetings, documents, and reminders ----------------

def _ensure_calendar_token(user):
    if user["calendar_token"]:
        return user["calendar_token"]
    token = secrets.token_urlsafe(24)
    db.set_calendar_token(user["id"], token)
    return token


@app.route("/upcoming")
@login_required
def upcoming():
    scheduled = db.list_upcoming_scheduled_meetings_for_user(session["user_id"])
    token = _ensure_calendar_token(current_user())
    calendar_url = url_for("calendar_feed", token=token, _external=True)
    return render_template("upcoming.html", scheduled=scheduled, calendar_url=calendar_url)


def _ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


@app.route("/calendar/<token>.ics")
def calendar_feed(token):
    user = db.get_user_by_calendar_token(token)
    if user is None:
        abort(404)

    meetings = db.list_upcoming_scheduled_meetings_for_user(user["id"])
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Meeting Ledger//EN", "CALSCALE:GREGORIAN"]

    for m in meetings:
        try:
            start = _scheduled_utc_datetime(m)
        except ValueError:
            continue
        end = start + timedelta(hours=1)  # meetings don't currently record a duration, so this is a placeholder
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        description = f"Attendees: {m['attendees']}" if m["attendees"] else ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:meeting-ledger-{m['id']}@meeting-ledger",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{_ics_escape(m['title'])}",
            f"DESCRIPTION:{_ics_escape(description)}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines), mimetype="text/calendar")


@app.route("/scheduled/new", methods=["GET", "POST"])
@login_required
def schedule_meeting():
    if request.method == "POST":
        check_csrf()
        title = request.form.get("title", "").strip()
        scheduled_at = request.form.get("scheduled_at", "").strip()
        attendees = request.form.get("attendees", "").strip()
        repeat = request.form.get("repeat", "none")
        try:
            tz_offset = int(request.form.get("timezone_offset_minutes", "0"))
        except ValueError:
            tz_offset = 0
        try:
            repeat_count = max(1, min(52, int(request.form.get("repeat_count", "1"))))
        except ValueError:
            repeat_count = 1

        if not title or not scheduled_at:
            flash("A title and a date & time are required.")
            return render_template("schedule_meeting.html", prefill_title=title, prefill_attendees=attendees)

        # Read every uploaded file into memory once, so the same set can
        # be attached to every occurrence of a repeating meeting without
        # re-reading the upload stream (which only works once).
        documents = []
        for file in request.files.getlist("documents"):
            if not file or not file.filename:
                continue
            content = file.read()
            if len(content) > MAX_DOCUMENT_BYTES:
                flash(f"Skipped {file.filename} - files must be under 5 MB.")
                continue
            documents.append((secure_filename(file.filename), file.content_type, base64.b64encode(content).decode("ascii")))

        recurrence_group = secrets.token_hex(8) if repeat != "none" and repeat_count > 1 else None
        occurrence_count = repeat_count if repeat != "none" else 1

        first_id = None
        for n in range(occurrence_count):
            occurrence_at = scheduled_at if n == 0 else _next_occurrence(scheduled_at, repeat, n)
            scheduled_id = db.create_scheduled_meeting(
                session["user_id"], title, occurrence_at, attendees, tz_offset, recurrence_group
            )
            if first_id is None:
                first_id = scheduled_id
            for filename, content_type, content_b64 in documents:
                db.add_scheduled_document(scheduled_id, filename, content_type, content_b64)

        return redirect(url_for("upcoming"))

    return render_template("schedule_meeting.html", prefill_title="", prefill_attendees="")


@app.route("/scheduled/<int:scheduled_id>")
@login_required
def scheduled_detail(scheduled_id):
    scheduled = db.get_scheduled_meeting_for_user(scheduled_id, session["user_id"])
    if scheduled is None:
        abort(404)
    documents = db.list_documents_for_scheduled_meeting(scheduled_id)
    return render_template("scheduled_detail.html", scheduled=scheduled, documents=documents)


@app.route("/scheduled/<int:scheduled_id>/ready")
@login_required
def scheduled_ready(scheduled_id):
    """Shown once the person has confirmed the meeting is really
    happening: the attached documents plus a quick catch-up, in one
    place - the "pull those documents for you" moment."""
    scheduled = db.get_scheduled_meeting_for_user(scheduled_id, session["user_id"])
    if scheduled is None:
        abort(404)
    documents = db.list_documents_for_scheduled_meeting(scheduled_id)

    prep_context = None
    wanted_names = _split_names(scheduled["attendees"] or "")
    if wanted_names:
        for m in db.list_meetings_for_user(session["user_id"]):
            if _split_names(m["attendees"] or "") & wanted_names:
                open_items = [i for i in db.list_action_items_for_meeting(m["id"]) if not i["done"]]
                prep_context = {"meeting": m, "open_items": open_items}
                break

    return render_template(
        "scheduled_ready.html", scheduled=scheduled, documents=documents, prep_context=prep_context
    )


@app.route("/scheduled/documents/<int:document_id>")
@login_required
def download_document(document_id):
    doc = db.get_document_for_user(document_id, session["user_id"])
    if doc is None:
        abort(404)
    content = base64.b64decode(doc["content_b64"])
    return Response(
        content,
        mimetype=doc["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


def _apply_notification_response(notification_id, scheduled_id, answer):
    db.mark_notification_answered(notification_id)
    if scheduled_id and answer == "yes":
        db.set_scheduled_meeting_status(scheduled_id, "confirmed")
    elif scheduled_id:
        db.set_scheduled_meeting_status(scheduled_id, "cancelled")


@app.route("/notifications/<int:notification_id>/respond", methods=["POST"])
@login_required
def respond_notification(notification_id):
    check_csrf()
    notification = db.get_notification_for_user(notification_id, session["user_id"])
    if notification is None:
        abort(404)

    answer = request.form.get("answer")
    scheduled_id = notification["scheduled_meeting_id"]
    _apply_notification_response(notification_id, scheduled_id, answer)

    if scheduled_id and answer == "yes":
        return redirect(url_for("scheduled_ready", scheduled_id=scheduled_id))
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/n/<token>/<answer>")
def respond_notification_by_token(token, answer):
    """The one-click link from a reminder email - works without being
    logged in, since the token itself proves it's a legitimate response
    to that specific reminder. Shows a simple standalone confirmation
    rather than the full app, since the person may not be signed in on
    this device."""
    notification = db.get_pending_notification_by_token(token)
    if notification is None or answer not in ("yes", "no"):
        abort(404)

    scheduled_id = notification["scheduled_meeting_id"]
    _apply_notification_response(notification["id"], scheduled_id, answer)

    user = db.get_user_by_id(notification["user_id"])
    lang = user["language"]
    ready_url = url_for("scheduled_ready", scheduled_id=scheduled_id) if answer == "yes" and scheduled_id else None
    return render_template(
        "notification_response.html",
        heading=t("notif_response_yes_heading" if answer == "yes" else "notif_response_no_heading", lang),
        body=t("notif_response_yes_body" if answer == "yes" else "notif_response_no_body", lang),
        ready_url=ready_url,
        ready_label=t("btn_view_documents", lang),
        rtl=is_rtl(lang),
    )


def _run_reminder_check():
    """Finds every scheduled meeting (across all accounts) starting within
    the reminder window and creates the "is this still happening?"
    notification for it - both in-app and, if email is configured, by
    email with one-click links (since an in-app banner is useless if
    nobody has the app open when it fires). Safe to call repeatedly -
    reminder_sent stops it from firing twice for the same meeting."""
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=REMINDER_WINDOW_MINUTES)

    created = 0
    for meeting in db.find_scheduled_reminder_candidates():
        try:
            meeting_time = _scheduled_utc_datetime(meeting)
        except ValueError:
            continue  # malformed/legacy row - skip rather than crash the whole check
        if now <= meeting_time <= window_end:
            user = db.get_user_by_id(meeting["user_id"])
            language = user["language"]
            message = t("notif_question", language).format(title=meeting["title"])
            token = secrets.token_urlsafe(24)
            db.create_notification(meeting["user_id"], meeting["id"], message, token)
            db.mark_reminder_sent(meeting["id"])
            created += 1

            if email_is_configured():
                yes_url = f"{email_base_url()}{url_for('respond_notification_by_token', token=token, answer='yes')}"
                no_url = f"{email_base_url()}{url_for('respond_notification_by_token', token=token, answer='no')}"
                subject = t("email_reminder_subject", language).format(title=meeting["title"])
                body = (
                    f"{message}\n\n"
                    f"{t('email_reminder_yes_text', language)}: {yes_url}\n\n"
                    f"{t('email_reminder_no_text', language)}: {no_url}"
                )
                send_reminder_email(user["email"], subject, body)
    return created


@app.route("/tasks/check-reminders", methods=["POST"])
def check_reminders():
    """Meant to be called every minute or so by an external scheduler
    (e.g. a Render Cron Job) in production - see README. Requires
    REMINDER_CHECK_SECRET to be set so randoms on the internet can't spam
    it, though the check itself is idempotent either way."""
    if not REMINDER_CHECK_SECRET:
        return jsonify({"error": "Reminder checking is not configured on this server."}), 501
    if request.headers.get("X-Task-Secret", "") != REMINDER_CHECK_SECRET:
        return jsonify({"error": "Invalid task secret."}), 401
    count = _run_reminder_check()
    return jsonify({"reminders_created": count}), 200


def _background_reminder_loop():
    """Only used for local development convenience (python3 app.py) so
    reminders work out of the box without setting up a cron job. Guarded
    so it never starts under gunicorn - see the bottom of this file."""
    while True:
        try:
            _run_reminder_check()
        except Exception:
            pass
        time.sleep(30)


# ---------------- live notes webhook (for a bot integration you connect later) ----------------

@app.route("/webhooks/live-notes", methods=["POST"])
def live_notes_webhook():
    """Receiving end for an automatic meeting-bot integration (e.g.
    Recall.ai finishing a recording and posting the transcript back).
    Nothing calls this yet on its own - it's the piece a real bot
    integration would POST to once you've set one up and configured
    LIVE_NOTES_WEBHOOK_SECRET. See README for what's still needed."""
    if not LIVE_NOTES_WEBHOOK_SECRET:
        return jsonify({"error": "Live notes integration is not configured on this server."}), 501

    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if provided_secret != LIVE_NOTES_WEBHOOK_SECRET:
        return jsonify({"error": "Invalid webhook secret."}), 401

    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    user = db.get_user_by_email(email)
    if user is None:
        return jsonify({"error": f"No account found for {email}"}), 404

    title = payload.get("title") or "Untitled meeting"
    meeting_date = payload.get("meeting_date") or db.now_iso()[:10]
    attendees = payload.get("attendees", "")
    transcript = payload.get("transcript", "")

    meeting_id = _create_meeting_with_ai(user["id"], title, meeting_date, attendees, transcript, user["language"])
    return jsonify({"status": "created", "meeting_id": meeting_id}), 201


if __name__ == "__main__":
    # The debug reloader re-runs this file in a parent "watcher" process
    # too - WERKZEUG_RUN_MAIN is only set in the actual serving process,
    # so this check keeps the reminder loop from starting twice.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        threading.Thread(target=_background_reminder_loop, daemon=True).start()
    app.run(debug=True, port=5000)
