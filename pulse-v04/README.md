# Pulse v0.4 — Social Platform

A Python + SQLite social website with real local accounts and a shared database.

## Start

1. Install Python 3.10 or newer.
2. Open a terminal in this folder.
3. Run `python server.py` (or `py server.py` on Windows).
4. Visit `http://localhost:8000`.
5. Register two accounts in separate browsers to test social interactions.

No pip installs needed. Data is stored in `pulse.db`; uploaded media in `uploads/`.

## Features

- Accounts and login (PBKDF2 password hashing, HttpOnly session cookies)
- Shared feed, follow, likes, comments, search, profiles
- Images and short videos (12 MB max)
- Direct messages with auto-refresh every 5 seconds while viewing messages
- Clips feed
- **New:** 24-hour stories, saved posts, notifications, communities, discussion posts
- Responsive dark interface

## Important limitations

This is a functional self-hosted **development starter**, not a production-hardened social network. Before public launch, add HTTPS, CSRF protection, abuse/rate limits, content reporting and moderation, secure media processing, email verification and password resets, database backups, and privacy policies. Stories expire from the feed after 24 hours but are not automatically purged from disk or the database. Message updates use polling, not WebSockets. Communities support text discussions; group DMs, live video, and verification badges are not included.

For LAN testing, use `http://YOUR-PC-LAN-IP:8000` from another device on the same network. Do not expose the development server directly to the public internet.
