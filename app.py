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
REMINDER_WINDOW_MINUTES = 5

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
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Fill in your name, email, and password.")
            return render_template("register.html")
        if len(password) < 8:
            flash("Password needs to be at least 8 characters.")
            return render_template("register.html")
        if db.get_user_by_email(email):
            flash("An account with that email already exists.")
            return render_template("register.html")

        user_id = db.create_user(name, email, generate_password_hash(password), current_language())
        session.clear()
        session["user_id"] = user_id
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.get_user_by_email(email)

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        return redirect(request.args.get("next") or url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- app routes ----------------

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    meetings = db.list_meetings_with_stats_for_user(session["user_id"])
    return render_template("dashboard.html", meetings=meetings)


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


@app.route("/meetings/<int:meeting_id>/action-items/<int:action_item_id>/toggle", methods=["POST"])
@login_required
def toggle_action_item(meeting_id, action_item_id):
    check_csrf()
    meeting = db.get_meeting_for_user(meeting_id, session["user_id"])
    if meeting is None:
        abort(404)
    db.toggle_action_item(action_item_id, meeting_id)
    return redirect(url_for("meeting_detail", meeting_id=meeting_id))


# ---------------- prep ----------------

def _split_names(text):
    return {name.strip().lower() for name in text.split(",") if name.strip()}


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


# ---------------- scheduled meetings, documents, and reminders ----------------

@app.route("/upcoming")
@login_required
def upcoming():
    scheduled = db.list_upcoming_scheduled_meetings_for_user(session["user_id"])
    return render_template("upcoming.html", scheduled=scheduled)


@app.route("/scheduled/new", methods=["GET", "POST"])
@login_required
def schedule_meeting():
    if request.method == "POST":
        check_csrf()
        title = request.form.get("title", "").strip()
        scheduled_at = request.form.get("scheduled_at", "").strip()
        attendees = request.form.get("attendees", "").strip()

        if not title or not scheduled_at:
            flash("A title and a date & time are required.")
            return render_template("schedule_meeting.html", prefill_title=title, prefill_attendees=attendees)

        scheduled_id = db.create_scheduled_meeting(session["user_id"], title, scheduled_at, attendees)

        for file in request.files.getlist("documents"):
            if not file or not file.filename:
                continue
            content = file.read()
            if len(content) > MAX_DOCUMENT_BYTES:
                flash(f"Skipped {file.filename} - files must be under 5 MB.")
                continue
            db.add_scheduled_document(
                scheduled_id,
                secure_filename(file.filename),
                file.content_type,
                base64.b64encode(content).decode("ascii"),
            )

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


@app.route("/notifications/<int:notification_id>/respond", methods=["POST"])
@login_required
def respond_notification(notification_id):
    check_csrf()
    notification = db.get_notification_for_user(notification_id, session["user_id"])
    if notification is None:
        abort(404)

    answer = request.form.get("answer")
    db.mark_notification_answered(notification_id)

    scheduled_id = notification["scheduled_meeting_id"]
    if scheduled_id and answer == "yes":
        db.set_scheduled_meeting_status(scheduled_id, "confirmed")
        return redirect(url_for("scheduled_ready", scheduled_id=scheduled_id))
    elif scheduled_id:
        db.set_scheduled_meeting_status(scheduled_id, "cancelled")

    return redirect(request.referrer or url_for("dashboard"))


def _run_reminder_check():
    """Finds every scheduled meeting (across all accounts) starting within
    the reminder window and creates the "is this still happening?"
    notification for it. Safe to call repeatedly - reminder_sent stops it
    from firing twice for the same meeting."""
    now = datetime.now(timezone.utc)
    now_iso_value = now.isoformat()
    window_end = (now + timedelta(minutes=REMINDER_WINDOW_MINUTES)).isoformat()

    due = db.find_due_reminders(window_end, now_iso_value)
    for meeting in due:
        message = t("notif_question", db.get_user_by_id(meeting["user_id"])["language"]).format(
            title=meeting["title"]
        )
        db.create_notification(meeting["user_id"], meeting["id"], message)
        db.mark_reminder_sent(meeting["id"])
    return len(due)


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
