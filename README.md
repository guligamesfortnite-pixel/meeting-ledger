# Meeting Ledger — MVP starter

A working starting point for your AI meeting manager: accounts, login, and a
meeting log where each person only ever sees their own meetings. This is
Phase 1 from our plan — notes/summary + action items. Prep and scheduling
are the natural next phases to build on top of this.

## What's actually working right now

- **Registration & login** with hashed passwords (`werkzeug.security`,
  scrypt-based) and secure signed session cookies.
- **CSRF protection** on every form (a per-session token checked on POST).
- **Data isolation**: every meeting and action item is tied to a `user_id`.
  Every query is filtered by the logged-in user's id, and trying to open
  someone else's meeting by URL returns a 404, not their data. This is what
  keeps Office A's meetings invisible to Office B.
- **AI summary + action items**: paste a transcript, get a summary and a
  list of action items back. If you set an `ANTHROPIC_API_KEY` environment
  variable it calls the real Claude API. Without one, it falls back to a
  simple offline heuristic (picks up lines starting with `Action:`,
  `TODO:`, etc.) so the app is fully testable before you wire in a key.
- **Production-ready database**: uses SQLite automatically for local
  testing. Set a `DATABASE_URL` environment variable (most hosts provide
  this the moment you attach a Postgres database) and it switches to
  Postgres with no other changes needed - this matters because a hosted
  app's local disk gets wiped on restarts/redeploys, so SQLite alone isn't
  safe for real customer data once it's live.
- **Production server**: includes `gunicorn` (a real production web server)
  and a `Procfile`, instead of relying on Flask's built-in dev server.
- **Multiple languages**: English, Chinese, Hindi, Spanish, Arabic, French,
  Bengali, Portuguese, Indonesian, Urdu, Russian, German, Japanese, and
  Hebrew are built in - the 15 most-spoken languages worldwide by total
  speakers, minus two regional spoken dialects (Nigerian Pidgin, Egyptian
  Arabic) that real business software doesn't typically ship as separate
  written-UI languages, since the people who speak them are already
  covered by English and Standard Arabic respectively. Hebrew is included
  as a bonus beyond the top 15. Right-to-left layout works properly for
  Hebrew, Arabic, and Urdu. Each account remembers its own language
  choice, and AI summaries are generated directly in that language too.
  Adding another language is a single new entry in `translations.py` - no
  other file needs to change.
- **Prep**: enter who's attending an upcoming meeting and it surfaces the
  most recent past meeting with any of the same people, plus whatever
  action items from that meeting are still open - so you walk in already
  caught up.
- **Schedule meetings in advance, with documents attached**: plan a
  meeting for a future date and time, attach any files you want on hand
  (agendas, briefs, whatever), and they're waiting for you when the
  meeting actually happens.
- **"Is this still happening?" reminders**: 5 minutes before a scheduled
  meeting's start time, a banner appears asking whether it's still
  happening. Answering "yes" takes you straight to a page with the
  attached documents and a quick catch-up (same info as Prep) already
  pulled together. Answering "no" quietly cancels it. Locally, this runs
  automatically in the background the moment you start the app - see
  "Reminders in production" below for what changes once it's hosted.
- **Live notes - the receiving end**: `/webhooks/live-notes` is a ready-to-use
  endpoint that a meeting-bot service (see below) can call automatically
  once a call ends, creating the meeting for you with no manual paste step.

## Run it locally

```bash
pip install flask
python3 app.py
```

Then open http://127.0.0.1:5000 — register an account and log a meeting.

To use real AI summaries instead of the offline fallback:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 app.py
```

## Project layout

```
app.py           Routes: auth, dashboard, meeting CRUD
database.py      SQLite schema + all queries (every read is scoped to a user)
ai_service.py    The one file to edit for your AI provider - swap models,
                 prompts, or provider here
templates/       Jinja2 HTML pages
static/style.css All styling
```

## Current design decision worth knowing about

Right now, isolation is **per individual account** — if two people from the
same office both sign up, they each only see their own meetings, not each
other's. That matches exactly what you asked for. If you'd rather have
**shared office accounts** (everyone at an office sees the whole office's
meetings, but not other offices'), that's a small change: add an
`organizations` table, put `org_id` on `users` instead of / alongside
individual ownership, and scope meetings by `org_id`. Worth deciding before
you have real customers, since migrating data between the two models later
is more work than picking correctly now.

## Deploying so customers can reach it

This app is set up to deploy on Render (render.com) with minimal setup:
a `Procfile` tells it how to start, and the database auto-switches to
Postgres once one is attached. Broad strokes:

1. Put this code in a GitHub repository (can be done entirely through
   github.com's web upload - no command line needed)
2. Create a Render account, connect the GitHub repo, and let it build
   a Web Service from this folder
3. Add a Postgres database in Render and attach it - this sets
   `DATABASE_URL` automatically
4. Set two more environment variables in Render's dashboard: `SECRET_KEY`
   (any long random string) and `ANTHROPIC_API_KEY` if you want real AI
   summaries
5. Render gives you a working `https://yourapp.onrender.com` URL
   immediately - HTTPS included
6. Buy a domain (any registrar), then add it under Render's "Custom
   Domains" settings and update your DNS records as instructed there

## Still worth doing before charging real customers

- Rate limiting on `/login` and `/register` (to slow down brute-force
  login attempts)
- Password reset flow (currently there isn't one)
- A billing/subscription system (e.g. Stripe) to actually charge customers
  monthly - nothing here handles payments yet

## Reminders in production

Locally, running `python3 app.py` starts a background check every 30
seconds automatically - nothing to configure. Once this is deployed with
gunicorn (multiple worker processes), that in-process approach isn't
reliable, so instead:

1. Set `REMINDER_CHECK_SECRET` as an environment variable on your server
2. Set up a scheduled task that sends a POST request to
   `https://yourapp.com/tasks/check-reminders` with header
   `X-Task-Secret: <the same secret>` roughly once a minute - Render's
   "Cron Jobs" feature can do this, or any external scheduler
3. That's it - the endpoint finds meetings starting in the next 5 minutes
   and creates the notification for whoever owns them

## Making "live notes" fully automatic

Right now, getting a meeting into the app is a manual paste. The app is
ready to receive an automatically-generated transcript the moment one
arrives (`/webhooks/live-notes`), but making a bot actually join a call
and produce that transcript requires signing up for a separate service -
this genuinely can't be done from inside this project alone. The
straightforward path:

1. Create an account with a meeting-bot provider (Recall.ai is a common
   choice) - this needs its own paid API key
2. Configure it to send a Zoom/Meet/Teams meeting's finished transcript to
   `https://yourapp.com/webhooks/live-notes` when a call ends
3. Set `LIVE_NOTES_WEBHOOK_SECRET` as an environment variable on your
   server, and give that same value to the bot provider so it can prove
   the request is really from them
4. The webhook expects JSON shaped like:
   `{"email": "...", "title": "...", "meeting_date": "YYYY-MM-DD", "attendees": "...", "transcript": "..."}`
   matched to the account by email - the meeting then appears on that
   person's dashboard automatically, fully summarized, no paste required.
