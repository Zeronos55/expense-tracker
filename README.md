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
| `/` | Home dashboard — month total vs last month, insight cards, category donut, spending-pace line, recent expenses |
| `/charts` | Trends — monthly spend by category, category totals, annual totals |
| `/transactions` | Filterable list of all entries (month, category chips, **Needs review**), tap one to edit |
| `/ai-status` | Is Gemini configured? Run a test receipt and see the raw result or error |
| `/recategorise` | POST — retry auto-categorisation for Uncategorized entries (button on History → Needs review) |
| `/add` | Manual entry form |
| `/edit/<id>` | Edit or delete a single entry |
| `/upload-from-shortcut` | POST endpoint for the iOS Shortcut (see below) |

## One-time setup

### 1. Add the `details` column

Existing Supabase project already has the `expenses` table (from `migrate.py`).
Run `supabase_add_details_column.sql` once in the Supabase SQL Editor
(Project → SQL Editor → New query → paste → Run) to add the new `details`
column used by the forms and the Shortcut endpoint.

Also run `supabase_add_merchant_rules.sql` (learned merchant → category
rules) and `supabase_add_raw_text.sql` (keeps each upload's OCR text so bad
entries can be re-read later).

### 2. Environment variables (Render → your service → Environment)

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | yes | Your Supabase project URL |
| `SUPABASE_KEY` | yes | Your Supabase anon key |
| `APP_USERNAME` | yes | Username for logging into the app in a browser |
| `APP_PASSWORD` | yes | Password for logging into the app in a browser |
| `SHORTCUT_SECRET` | yes | A separate password only your Shortcut knows, for `/upload-from-shortcut` |
| `GEMINI_API_KEY` | recommended | Free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Reads receipts the regex parsers can't and categorises unknown merchants |
| `GEMINI_MODEL` | no | Defaults to `gemini-2.5-flash` |

The first five are **required** — this app is personal, not public. Without
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

## Auto-categorisation

Each Shortcut upload goes through:

1. **Regex parsers** per banking app pull out date, payee and amount.
   Payees that are really field labels ("Wallet", "Transaction Type",
   "Reference No", …) are thrown away.
2. **Learned rules** (your past edits, exact or fuzzy match), then **keyword
   rules**, pick the category.
3. If the payee, amount or date is missing, or the category is still unknown, the
   **whole OCR text goes to Gemini**, which returns merchant, amount, date and
   category. Its answer fills the gaps and is remembered as a learned rule.
   Your own corrections always win over the AI.

If nothing seems to be categorised, open **`/ai-status`** (the ✨ AI button
on Home). It shows whether `GEMINI_API_KEY` is set and lets you run a test
receipt. Failures (bad key, quota, timeout) are shown there and logged to
Render's logs as `Gemini call failed: …`. The Shortcut's JSON response also
includes `method` (`llm`, `keyword`, `learned`, `fuzzy` or `none`) so you can
see which step categorised it.
