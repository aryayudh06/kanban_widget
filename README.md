# Floating Kanban Widget

A borderless, always-on-top-capable Kanban board for Windows that syncs
in real time across every device signed into the same account, backed by
Supabase (free tier) for auth/storage/realtime, with a local SQLite cache
so it keeps working offline.

## 1. Set up Supabase

1. Create a free project at https://supabase.com.
2. Go to **SQL Editor** and run the contents of `supabase_setup.sql`.
   This creates the `boards` / `columns` / `cards` tables, enables Row
   Level Security so each user can only ever see their own rows, and
   turns on Realtime broadcasting for `columns` and `cards`.
3. Run `supabase_migration_002_deadlines_and_status.sql` next, in the same
   SQL Editor. This adds the optional `deadline` and `is_done` columns
   used by the deadline warnings, the per-card "done" checkbox, and the
   analytics panel. It's additive only (`ADD COLUMN IF NOT EXISTS`), so
   it's safe to run even on a project that already has data in it. If
   you're setting up a brand-new project, just run both scripts in order
   (`supabase_setup.sql`, then this migration) before first launch.
4. Go to **Project Settings -> API** and copy your **Project URL** and
   **anon public key**.
4. Either edit `config.py` directly, or (recommended) set environment
   variables before launching:

   ```powershell
   setx SUPABASE_URL "https://YOUR-PROJECT-REF.supabase.co"
   setx SUPABASE_ANON_KEY "YOUR-ANON-PUBLIC-KEY"
   ```

   The anon key is safe to embed in a desktop client — it has no special
   privileges on its own. RLS is what actually restricts access to a
   user's own data.

## 2. Install dependencies

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Run it

```powershell
python main.py
```

* First run: a small sign-in/sign-up dialog appears. Create an account —
  Supabase will email a confirmation link if you have email confirmation
  enabled in Auth settings (you can turn that off in the Supabase
  dashboard for faster local testing).
* Once signed in, the floating board appears. Drag the header to
  reposition it, click the pin icon to keep it always-on-top, and use
  the tray icon to show/hide, toggle "run at Windows startup", check
  sync status, log out, or exit.
* Default global hotkey to show/hide from anywhere: **Ctrl+Alt+T**
  (change `GLOBAL_HOTKEY` in `config.py`).
* Sign in with the same account on a second machine and edits sync
  within a second or two over Supabase Realtime; anything done offline
  is queued locally and pushed automatically once connectivity returns.

## Signing in on another device / logging out

* On sign-in the app first **pulls your boards from Supabase** and shows
  your oldest board; it only creates a new board if your account has none.
  (Earlier builds invented a fresh empty board on every new device, which
  is why data from another machine didn't appear. Those extra empty
  "My Board" rows may still exist in your `boards` table - they're
  harmless and can be deleted in the Supabase Table Editor.)
* **Log out** is in the widget header (fullscreen view) and in the tray
  menu. It signs out **this device only** - your other devices stay signed
  in - and clears this device's local cache and saved credentials. If
  edits haven't synced yet, the app tries to push them first and warns you
  if any would be lost. Signing back in restores everything from the cloud.
* The local cache is tied to the account that created it, so signing in as
  a different user never shows the previous user's data.

## Task deadlines, warnings, view modes, analytics, and checklists

* **Deadlines are optional.** In the card editor, tick "Set a deadline"
  to attach a date/time; leave it unticked for a deadline-free card.
* **Warnings are automatic.** A card's border and a small badge turn
  amber ("Due soon") inside `DEADLINE_DUE_SOON_HOURS` (24h by default,
  in `config.py`) of its deadline, and red ("Overdue") once it's passed.
  These re-check every minute while the board is open, so a card visibly
  changes color even if you don't touch it.
* **Checklist-style completion.** Every card has a checkbox; ticking it
  marks the task done (strikethrough title, dimmed card, excluded from
  "overdue/due soon" warnings) and each column header shows a
  `(done/total)` progress count.
* **Two view modes**, toggled with the ⤢ / ⛶ button in the header:
  - **Fullscreen (expanded)** - the full editable board plus the
    **Analytics** panel (total/done/overdue/due-soon counts and a
    per-column completion chart). This is where you add columns, add or
    edit cards, and drag cards between columns.
  - **Compact (minimized)** - a small, read-only floating summary: a
    one-line count per column and the single nearest upcoming deadline.
    Meant to sit unobtrusively on your desktop; double-click it to jump
    back to fullscreen.
* **Resizable.** Drag the grip in the bottom-right corner in either view
  mode; the fullscreen board/analytics split can also be dragged via the
  divider between them.

## 4. Packaging as a distributable .exe (optional)

```powershell
pip install pyinstaller
pyinstaller --noconsole --onefile --name FloatingKanban main.py
```

The "run at Windows startup" toggle points the registry entry at
`sys.executable`, so for a clean startup entry, enable that toggle
*after* running the packaged `.exe`, not the raw `python main.py`.

## Project layout

| File               | Responsibility                                              |
|--------------------|--------------------------------------------------------------|
| `config.py`        | Supabase URL/key, keyring service names, app constants        |
| `supabase_setup.sql` | Tables, RLS policies, Realtime publication (run once in Supabase) |
| `supabase_migration_002_deadlines_and_status.sql` | Adds `deadline`/`is_done` columns to `cards` (run once, after `supabase_setup.sql`) |
| `security.py`      | Token storage via `keyring`; PyQt6 sign-in/sign-up dialog      |
| `db_local.py`      | SQLite cache + offline sync queue (no Qt/network deps)        |
| `sync_manager.py`  | Supabase auth/CRUD/Realtime, thread-safe bridge into Qt signals |
| `ui_widget.py`     | Frameless floating window, columns, cards, drag-and-drop       |
| `os_integration.py`| Windows startup registry, system tray, global hotkey           |
| `main.py`          | Wires everything together; application entry point            |

## Security notes

* **RLS is the real boundary.** Every table policy is
  `USING (auth.uid() = user_id)`, so even if the anon key leaked, a
  request without a valid user JWT for that row returns nothing.
* **Tokens live in the OS credential store**, not a plaintext file —
  `keyring` on Windows uses the Credential Manager (DPAPI-encrypted per
  Windows user account).
* **Never embed a `service_role` key** in this app. That key bypasses
  RLS entirely and must never leave a trusted server.
* Consider enabling **email confirmation** and a reasonable password
  policy in Supabase Auth settings before using this beyond local testing.
