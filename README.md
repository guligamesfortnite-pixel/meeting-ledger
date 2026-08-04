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
  meeting actually happens. Meetings can also repeat weekly or monthly
  (up to 52 occurrences at once, each with the same documents attached)
  - useful for standups, ops syncs, or anything that happens on a
  regular cadence.
- **"Is this still happening?" reminders - in-app and by email**: 1 hour
  before a scheduled meeting's start time, a banner appears in the app
  asking whether it's still happening, and - if email is configured - the
  same question goes out by email too, with one-click "yes"/"no" links
  that work without logging in. This matters because an in-app banner
  alone is easy to miss if nobody has the app open at the right moment;
  the email is what makes the reminder actually reliable. Answering
  "yes" (either way) takes you to a page with the attached documents and
  a quick catch-up (same info as Prep) already pulled together.
  Answering "no" quietly cancels it. Locally, the check runs
  automatically in the background the moment you start the app - see
  "Reminders in production" below for what changes once it's hosted.
  Each meeting correctly accounts for whatever timezone it was scheduled
  in (captured automatically from the browser), so the reminder fires at
  the right real-world moment no matter where you or your customers are.
- **My Tasks**: one page showing every open action item across every
  meeting, not just per-meeting - so you can see everything you're on
  the hook for without hunting through individual meetings.
- **Export to PDF or Word**: any meeting's summary, action items, and
  transcript can be downloaded as a `.pdf` or `.docx` file - useful for
  sharing a meeting's outcome with someone who isn't a Meeting Ledger
  user (a client, a board member, anyone outside the system).
- **Calendar sync**: every account gets a private subscription link
  (on the Upcoming page) that works with Google Calendar, Outlook, Apple
  Calendar, or any calendar app that supports subscribing by URL - a
  universal open standard (iCalendar/.ics), so it covers "popular
  calendar apps" broadly instead of needing a separate integration per
  provider. It's one-way for now: scheduled meetings show up
  automatically in their calendar (refreshed every few hours, typical
  for these feeds), but it doesn't pull events *from* Google Calendar
  back in yet. That fuller two-way sync is possible later via a free
  Google Cloud OAuth app - a more involved one-time setup, worth doing
  once the one-way version proves useful.
- **Live notes - the receiving end**: `/webhooks/live-notes` is a ready-to-use
  endpoint that a meeting-bot service (see below) can call automatically
  once a call ends, creating the meeting for you with no manual paste step.
- **Editing and deleting**: any meeting can be edited (including manually
  rewriting the summary, or regenerating it from an edited transcript) or
  deleted entirely. Action items can be added by hand or deleted
  individually, not just generated by AI.
- **Search**: the dashboard has a search box that matches against title,
  attendees, and summary text.
- **Account settings**: `/account` lets someone update their name, email,
  or password without needing you to touch the database.
- **Terms of Service & Privacy Policy**: basic, honest starting pages at
  `/terms` and `/privacy`, linked from registration (which now requires
  agreeing to them) and the footer of every page. Written in plain
  English only, not translated - see the note below on why.
- **Password reset**: a real forgot-password flow, sent via email. Uses
  plain SMTP (works with a free Gmail app password, no paid service
  needed) - see "Setting up password reset emails" below.
- **Login protection**: repeated failed login or registration attempts
  from the same place get slowed down automatically (8 tries per 15
  minutes) to make it harder to brute-force a password.
- **Pricing page**: a public `/pricing` page showing your plan tiers
  ($49/$169/$279 + $25 per person beyond 10), plus a field to redeem an
  access code for free access - useful for letting testers, partners, or
  yourself in without paying, while the pricing itself stays real for
  everyone else. Nothing is actually gated behind payment yet (every
  account already has full access) - this lays the groundwork for when
  billing gets added, and the `plan` field on each account tracks who
  redeemed a code versus who's still on trial.
- **Managing access codes**: visit `/admin/codes` and enter the
  `ADMIN_SECRET` you configured to create and view codes. Each code can
  have a use limit (e.g. one code good for 10 signups) and stops working
  on its own once used up.

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

## Setting up password reset emails

Free option using a Gmail account you already have:

1. Turn on 2-Step Verification on that Google Account if it isn't already
2. Go to Google Account -> Security -> App passwords, and create one for
   "Mail"
3. Set these environment variables on your server:
   - `SMTP_HOST` = `smtp.gmail.com`
   - `SMTP_PORT` = `587`
   - `SMTP_USER` = the Gmail address
   - `SMTP_PASSWORD` = the app password you just generated (not your
     regular Gmail password)
   - `APP_BASE_URL` = your real site address, e.g.
     `https://meeting-ledger.onrender.com` (so reset links point to the
     right place instead of your own computer)

Without these set, forgot-password still works safely - it just tells
the person email isn't configured yet instead of silently failing.

## Setting up access codes

Set `ADMIN_SECRET` as an environment variable to any private value only
you know, then visit `/admin/codes` on your site and enter it to create
and view codes.

## Still worth doing before charging real customers

- A billing/subscription system (e.g. Stripe) to actually charge customers
  monthly - nothing here handles payments yet
- Data backups - the free-tier database isn't necessarily backed up
  automatically; fine for testing, not fine once real customer data matters
- Have an actual lawyer review the Terms/Privacy pages before relying on
  them - they're intentionally left in English only rather than
  translated into all 14 UI languages, since mistranslating legal text
  carries real risk and a lawyer reviewing 14 versions isn't practical
  for a starting template like this

## A decision worth making deliberately: personal vs. shared accounts

Right now, every person's meetings are private to them - even people
from the same paying office can't see each other's. That's a fine model
for a personal tool, but the pricing tiers (1 person / up to 5 / up to
10) assume a shared office workspace, which this doesn't currently
provide. Worth deciding intentionally before real customers sign up:
keep personal (simpler, harder to justify per-office pricing) or move to
shared team workspaces (matches the pricing, a bigger structural change
to the data model described earlier in this README).

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
3. That's it - the endpoint finds meetings starting in the next hour
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
