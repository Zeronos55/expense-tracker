# Expense Tracker

A small Flask app for tracking personal expenses from bank/e-wallet receipt
screenshots. Data lives in Supabase; the app is deployed on Render.

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
| `/process`, `/process/run` | Scans a local folder for screenshots and OCRs them — only works when run on a machine with that folder (e.g. locally on Windows with iCloud Drive), not on Render |

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
| `SHORTCUT_SECRET` | recommended | A password only your Shortcut knows. When set, `/upload-from-shortcut` rejects any request that doesn't include it. Without it, the endpoint is open to anyone who has the URL. |

Pick any random string for `SHORTCUT_SECRET`, e.g. generate one with
`python -c "import secrets; print(secrets.token_urlsafe(24))"`.

## iOS Shortcut — upload a receipt screenshot

OCR runs **on-device** using Shortcuts' own "Extract Text from Image" action,
so the app doesn't need to run Tesseract for shortcut uploads (it still can,
as a fallback — see below).

1. Open the **Shortcuts** app → **+** to create a new shortcut.
2. Add action **Extract Text from Image**. Leave "Input" as the shortcut's
   input — this lets you trigger the shortcut from the Share Sheet on a
   screenshot in Photos.
3. Add action **Get Contents of URL**:
   - URL: `https://<your-app>.onrender.com/upload-from-shortcut`
   - Method: `POST`
   - Request Body: **Form**
     - Field `text`, value = the *Extracted Text* variable from step 2
     - Field `secret`, value = your `SHORTCUT_SECRET` (skip if you didn't set one)
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

### Fallback: server-side OCR

`/upload-from-shortcut` also still accepts a raw `image` file field (instead
of `text`) — in that case the server runs `pytesseract` on it, same as
before. Useful if a device can't run "Extract Text from Image" (e.g. an
Automation from a non-Apple source), but slower and depends on Render having
Tesseract installed (`render.yaml` already does this).

## Local development

```
pip install -r requirements.txt
export SUPABASE_URL=...
export SUPABASE_KEY=...
python app.py
```
