# Expense Tracker

A small **personal** Flask app for tracking your own expenses from bank/e-wallet
receipt screenshots. Data lives in Supabase; the app is deployed on Render.
The whole app requires a login (see below) — it's not meant to be public.

Each entry has: **date, recipient (name), amount (price), source (which
banking app), category, and optional details.** View a monthly/annual
summary, a chart view, and add, edit or delete any entry.

## Routes

| Route | What it does |
|---|---|
| `/` | Monthly/annual summary (text) |
| `/charts` | Same data as visual charts — monthly spend, by-category breakdown, annual totals |
| `/transactions` | Filterable list of all entries, tap one to edit |
| `/add` | Manual entry form |
| `/edit/<id>` | Edit or delete a single entry |
| `/upload-from-shortcut` | POST endpoint for the iOS Shortcut (see below) |

## One-time setup

### 1. Add the `details` column

Existing Supabase project already has the `expenses` table (from `migrate.py`).
Run `supabase_add_details_column.sql` once in the Supabase SQL Editor
(Project → SQL Editor → New query → paste → Run) to add the new `details`
column used by the forms and the Shortcut endpoint.

### 2. Environment variables (Render → your service → Environment)

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | yes | Your Supabase project URL |
| `SUPABASE_KEY` | yes | Your Supabase anon key |
| `APP_USERNAME` | yes | Username for logging into the app in a browser |
| `APP_PASSWORD` | yes | Password for logging into the app in a browser |
| `SHORTCUT_SECRET` | yes | A separate password only your Shortcut knows, for `/upload-from-shortcut` |

All four are now **required** — this app is personal, not public. Without
`APP_USERNAME`/`APP_PASSWORD` set, every page refuses to load; without
`SHORTCUT_SECRET`, the Shortcut endpoint refuses uploads. Pick any random
string for `SHORTCUT_SECRET` and `APP_PASSWORD`, e.g. generate one with
`python -c "import secrets; print(secrets.token_urlsafe(24))"`.

Visiting the app in a browser will prompt for `APP_USERNAME`/`APP_PASSWORD`
(standard HTTP Basic Auth — your browser remembers it after the first login).
This is separate from `SHORTCUT_SECRET`, which only guards the upload
endpoint the Shortcut calls.

## iOS Shortcut — upload a receipt screenshot

OCR runs **on-device** using Shortcuts' own "Extract Text from Image" action —
the server only ever receives already-extracted text, never an image.

1. Open the **Shortcuts** app → **+** to create a new shortcut.
2. Add action **Extract Text from Image**. Leave "Input" as the shortcut's
   input — this lets you trigger the shortcut from the Share Sheet on a
   screenshot in Photos.
3. Add action **Get Contents of URL**:
   - URL: `https://<your-app>.onrender.com/upload-from-shortcut`
   - Method: `POST`
   - Request Body: **Form**
     - Field `text`, value = the *Extracted Text* variable from step 2
     - Field `secret`, value = your `SHORTCUT_SECRET`
4. (Optional) Add **Show Notification**, with the "Get Contents of URL" result
   as the text, so you get an instant confirmation of what was parsed.
5. Rename the shortcut (e.g. "Log Receipt"), tap the settings icon, and turn
   on **Show in Share Sheet**, restricted to Images.
6. To use it: screenshot a bank/e-wallet payment confirmation → tap Share →
   pick "Log Receipt". It appears in the app within a few seconds.

The endpoint responds with the parsed `date`, `recipient`, `amount`,
`category` and `source` as JSON so step 4's notification can show them.
Whatever comes out imperfect (OCR/parsing isn't always exact) can be fixed
afterwards in the app under **History** → tap the entry → edit.

## Local development

```
pip install -r requirements.txt
export SUPABASE_URL=...
export SUPABASE_KEY=...
export APP_USERNAME=...
export APP_PASSWORD=...
export SHORTCUT_SECRET=...
python app.py
```
