# Add the server to your GitHub repo (so Vercel stops 404-ing)

Your domain works except for one thing: the **server file** never made it into the
repo. GitHub can absolutely take folders — the trick is *how* you drop them.
Here are three ways, easiest first. Any one of them fixes it.

The goal is simply this: in your repo, next to `index.html` —

```
your-repo/
  index.html          ← already there ✅
  api/                ← must be a FOLDER
    decompile.py      ← the server (the missing piece) ❌ → add this
```

---

## THE SHORTEST FIX - one file, one paste, no folders to create

Your repo already has `index.html`. The **only** thing missing is one text file.
Vercel needs it to live inside a folder called `api` - and GitHub makes that
folder for you the moment you type the name. You never click "new folder".

1. In your repo, click **Add file** -> **Create new file**
2. In the file-name box at the top, type exactly this (the `/` makes the folder):

   ```
   api/decompile.py
   ```

   Watch the box - it turns into `api / decompile.py`. That IS the folder.
3. Open **`vercel-deploy/api/decompile.py`** in your workspace viewer,
   press **Ctrl + A**, then **Ctrl + C**.
4. Click into the big text box on GitHub and press **Ctrl + V**.
5. Scroll down -> **Commit changes**.

That's the whole fix. Vercel redeploys on its own in about a minute.

**Check it:** open `https://oracle-decompiler.vercel.app/api/decompile`
- `{"ok": true, "service": "Oracle decompiler endpoint", ...}` -> done, drop a game in
- `The page could not be found` -> the file is not inside an `api` folder; redo step 2

Everything else in this folder is optional (see the file table below). Nothing
needs to be unzipped for this route, and nothing needs to be uploaded.

---

## Way 1 — drag the whole thing (fastest, if you have the unzipped folder)

1. Unzip **`Oracle-website.zip`** (it's in your workspace viewer).
2. Open the unzipped folder **`oracle-website`**.
3. Press **Ctrl + A** to select everything *inside* it
   (`index.html`, the **api** folder, `vercel.json`, `requirements.txt`, the icons).
   ⚠️ Select the items **inside** the folder — don't drag the folder itself, or
   GitHub will put your site one level too deep.
4. In your repo on GitHub: **Add file → Upload files**.
5. Drag the selected items onto the big drop area.
   GitHub keeps the structure, so you'll see `api/decompile.py` in the list.
6. Scroll down → **Commit changes**.

Vercel is watching the repo, so it redeploys by itself in about a minute.

---

## Way 2 — create the file in the browser (no dragging, no unzip)

1. In your repo: **Add file → Create new file**.
2. In the filename box, type exactly:

   ```
   api/decompile.py
   ```

   Typing the `/` is what creates the **api** folder — you'll see the box reformat
   itself around `api / decompile.py` once you type it.
3. Now paste the code. Open **`vercel-deploy/api/decompile.py`** in your workspace
   viewer, select all (**Ctrl + A**), copy (**Ctrl + C**), then click into
   GitHub's editor box and paste (**Ctrl + V**).
4. Scroll down → **Commit changes**.

While you're in there, also re-upload **`index.html`** from the zip (Add file →
Upload files → drop just that file → Commit). That's the newest build of the page:
it adds the red "missing server" warning bar, the 48 MB limit for whole games, and
clearer messages. The old one works too — this just makes it better.

---

## What about the other files? (what each one does, and which you actually need)

Only **two** files are load-bearing. Everything else is a bonus.

| File | Need it? | What it does | If you skip it |
|---|---|---|---|
| `index.html` | **YES** | the page itself | no site |
| `api/decompile.py` | **YES** | the server that does the decompiling | 404 on every upload |
| `vercel.json` | worth it | pins 2 GB memory / 300 s, caches the icons | Hobby already defaults to those limits, so mostly harmless to skip |
| `requirements.txt` | only if needed | installs `zstandard`, for newer places saved with ZSTD compression | those specific files come back saying they need one extra package |
| `favicon.ico` | yes - you wanted it | the Oracle icon in the Chrome tab | Chrome shows its default globe |
| `icon.png` | nice to have | bigger icon, phones | - |
| `apple-touch-icon.png` | nice to have | home-screen icon on phones | - |
| `README-FIRST.txt`, `DEPLOY.md`, `GITHUB-DEPLOY.md` | no | notes for you | nothing; they would just sit at `your-site/README-FIRST.txt` |

### One rule that saves you grief

- **Text files** can be made by hand with **Create new file** and paste:
  `api/decompile.py`, `vercel.json`, `requirements.txt`.
- **Files that aren't text** cannot be pasted - they have to be **uploaded**:
  `index.html` (6.8 MB), `favicon.ico`, `icon.png`, `apple-touch-icon.png`.
  Use **Add file -> Upload files** and drop them in.

So if you want everything at once, **Way 1 is still the fastest**: Ctrl+A inside the
unzipped folder and drag all of it - text and binary files go up in one commit.
Re-uploading `index.html` simply replaces the old copy; no need to delete it first.

---

## Way 3 — skip GitHub entirely

On <https://vercel.com/new> you can drop a folder straight onto the page. Unzip
`Oracle-website.zip`, then drag the **oracle-website** folder onto that page and
press **Deploy**. Vercel handles the whole project, `api/` included — no GitHub
step at all. (Your GitHub-connected project stays as it is; this just makes a new
one.)

---

## After it deploys — check it in 5 seconds

Open this in your browser:

```
https://oracle-decompiler.vercel.app/api/decompile
```

| What you see | Meaning |
|---|---|
| `{"ok": true, "service": "Oracle decompiler endpoint", ...}` | ✅ the server is there — drop a game and it decompiles |
| `The page could not be found` / `NOT_FOUND` | ❌ `api/decompile.py` still isn't in the repo (or it isn't *inside* an `api` folder) |

Also, the page tells you itself now: if the server is missing it shows a **red bar
across the top** saying *"This website is missing its server part"*. Once the
server is up, that bar disappears on its own.

---

## Common ways this goes wrong

- **`decompile.py` sitting loose next to `index.html`** — it must be *inside* a
  folder named `api`. Vercel only turns files in `api/` into server routes.
- **Everything inside an extra folder** (`oracle-website/index.html`) — then the
  repo root has no `index.html` and no `api/`, so nothing works. If that happened,
  the files need to move up one level.
- **Vercel project has a "Root Directory" set** (Settings → General) — if it points
  at a subfolder, `api/` has to go in *that* subfolder. Simplest: set it back to
  empty and keep `index.html` + `api/` at the repo root.
- **Uploaded `Oracle-website.zip` to GitHub** — GitHub stores a zip as-is; it does
  not unpack it, so that zip is invisible to Vercel. Unzip first, then upload.
