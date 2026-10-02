# Deploy Oracle to oracle-decompiler.vercel.app

This folder is a complete, self-contained Vercel project. Upload it and the
domain decompiles by itself — no local server needed.

> ⚠️ **The `api/` folder is the server. If it doesn't go up, nothing works.**
> Uploading `index.html` on its own gives you a site that looks fine until you
> drop a file — then every upload fails with a 404. The folder must keep this
> shape:
>
> ```
> oracle-website/
>   index.html
>   api/decompile.py      ← the server; without it every upload 404s
>   vercel.json
>   favicon.ico  icon.png  apple-touch-icon.png
> ```
>
> No `requirements.txt`, `pyproject.toml` or `Pipfile` at the top of the repo:
> a root dependency file makes Vercel treat the whole folder as a Python
> *app*, and the build stops with "No python entrypoint found in default
> locations". `decompile.py` already carries a ZSTD decoder written in plain
> Python, so newer ZSTD-compressed places work with nothing installed.
>
> The page now checks this itself: if the server is missing it shows a red bar
> across the top saying so, instead of a confusing transfer error.

```
vercel-deploy/
├── index.html              the app (6.5 MB, fully self-contained)
├── api/
│   └── decompile.py        the decompiler endpoint (Python function)
├── favicon.ico             the Oracle icon (multi-size, 16→256 px)
├── icon.png                the Oracle icon (472×472)
├── apple-touch-icon.png    for phones / home screen
└── vercel.json             CORS + cache headers (no function block)
```

---

## Deploy it (pick one)

### Option A — drag & drop (easiest, ~30 seconds)

1. Go to **https://vercel.com/new**
2. Drag the whole **`vercel-deploy` folder** onto the page
3. Framework preset: **Other** · leave build settings empty
4. Click **Deploy**

### Option B — Vercel CLI

```bash
npm i -g vercel
cd vercel-deploy
vercel --prod
```

### Option C — GitHub

Push this folder to a repo, then **vercel.com/new → Import** it. Leave the
framework preset as **Other** and don't set a build command.

---

## Point your domain at it

1. Vercel dashboard → your project → **Settings → Domains**
2. Add `oracle-decompiler.vercel.app` (or your own custom domain)
3. Vercel issues the HTTPS certificate automatically

---

## The Oracle icon in Chrome

`favicon.ico`, `icon.png` and `apple-touch-icon.png` ship in this folder, and
`index.html` links all of them. After deploying you should see the Oracle icon
in the tab.

**If Chrome still shows a blank/default icon**, it's cached. Any of these fixes it:

- Hard-refresh: **Ctrl + Shift + R** (Windows) / **Cmd + Shift + R** (Mac)
- Open a new tab and visit `https://your-domain.vercel.app/favicon.ico` once —
  if you see the Oracle logo, the file is fine and it's purely a cache issue
- Or check it in a private/incognito window (no cache)
- Chrome caches favicons per origin, sometimes for days; a `?v=2` trick is to
  re-deploy with any change to `favicon.ico`

---

## How it works

```
visitor's browser
      │
      │  one request, for files up to ~4 MB
      ├────────────────────────────────────────────▶  /api/decompile  (this project)
      │
      │  numbered pieces, up to 64 MB (and the answer
      │  comes back in pieces too)
      ├────────────────────────────────────────────▶  /api/decompile
      │
      │  only for game files over 64 MB
      └────────────────────────────────────────────▶  http://127.0.0.1:8787/decompile
                                                      (the local engine, if running)
```

* **Any** API key works. Nothing on this path validates keys, so none can be
  rejected or revoked.
* `api/decompile.py` handles single chunks and whole place/model files
  (`.rbxl .rbxlx .rbxm .rbxmx`) — it unpacks the file and returns every script.
* A single request or response body on Vercel may be **4.5 MB**. Bigger files
  travel as numbered pieces: the page slices the upload, the function stitches it
  back together in `/tmp` plus memory, and the extracted document is served back
  in pieces of its own. If a piece goes missing, the function asks for exactly
  that piece again — so a big game survives a hiccup instead of failing.
* The function runs with **2 GB memory / 300 s**, and scripts in a place are
  decompiled **8 at a time**, so a large world doesn't crawl.

---

## Optional: the local engine (for big places)

Copy `oracle_server.py` next to `start_oracle.bat` / `start_oracle.sh` and run it
whenever you want to open multi-megabyte places — it has no size limit:

```bash
python3 oracle_server.py            # 127.0.0.1:8787
```

The deployed page detects it automatically for oversized files.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Oracle could not start" panel | Leftover saved data. Click **Clear saved data & reload**. |
| `-- Error: Failed to fetch` | The API function didn't answer. Redeploy; check **Vercel → Deployments → Functions** logs. |
| `-- This file is bigger than the website takes (about 64 MB).` | Over the web limit — export just the script you need, or use the local `Oracle.html`. |
| `-- Could not finish transferring this file` | A piece did not get through (usually a dropped connection). Drop the file again. |
| Red bar: "This website is missing its server part" | `api/decompile.py` isn't in the deployment. Redeploy the whole folder — see the top of this file. |
| A big ZSTD place is slow | ZSTD is decoded in plain Python. Most games take seconds; a very large one can take a minute. |
| Icon missing | Cache — see the favicon section above. |

### Checking the deployment

- `https://your-domain/` → the login screen
- `https://your-domain/api/decompile` → `{"ok": true, ...}` (a plain file, no key needed)
- `https://your-domain/favicon.ico` → the Oracle logo
