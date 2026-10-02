# Oracle: what to upload now (two files)

**This round changes two files.** In GitHub, unzip the new `oracle-repo.zip` and
**Add file → Upload files** with:

| file | action |
|---|---|
| `index.html` | **replace** — the download button now saves a **zip with one `.lua` per script** instead of one huge text file, and big answers are pulled in pieces |
| `api/decompile.py` | **replace** — retries compiled-only scripts, writes a machine-readable label per script, and raises the answer cap from 24 MB to 64 MB |
| `vercel.json`, icons, docs | unchanged (safe to upload anyway) |

Then commit; Vercel rebuilds by itself. Check
`https://oracle-decompiler.vercel.app/api/decompile` → `{"ok": true, ...}` and
drop the game in again: the editor fills with the code, and **Download** gives
you `yourgame_oracle_scripts.zip` with the whole game's scripts in folders.

---

# (background) One file away — what had happened before

## What the logs are telling us

I read your repo and the live domain, so this isn't guesswork:

1. **Your new repo `oracle-website` has the site at the top** — `index.html`, the icons,
   `vercel.json`, `README-FIRST.txt`. That part is right now. ✅
2. **But there is no `api/` folder in it.** So Vercel had nothing to build as a function.
3. **`vercel.json` declared that function anyway**, and an unmatched pattern is a *hard*
   build error:

   ```
   Error: The pattern "api/decompile.py" defined in `functions` doesn't match any
   Serverless Functions inside the api directory.
   Learn More: https://vercel.link/unmatched-function-pattern
   ```

   That's the line hiding just below the "Cloning completed" you pasted.
4. **And right now your domain is returning `DEPLOYMENT_NOT_FOUND`** — because no
   deployment on that project has ever succeeded, there's nothing being served at
   `https://oracle-decompiler.vercel.app/` at all right now. It comes straight back the
   moment a build succeeds.

I've removed the thing that can fail: **`vercel.json` no longer declares the function**
(it now only sets cache/CORS headers). From here on, a missing file can never kill the
build again — worst case the site goes live and shows the red "server is missing" bar.

## The step left: put three files in the repo

The page itself has been updated as well (the old copy can leave a dropped file
sitting on `-- Oracle decompiled code will appear here` and never ask for it
again), so upload the new `index.html` too. From **`oracle-repo.zip`**:

| file | action |
|---|---|
| `index.html` | **replace** the one in the repo (it is the fixed page) |
| `api/decompile.py` | **add** (this is the server) |
| `vercel.json` | **replace** if yours still has a `functions` block |

The easiest way is: unzip `oracle-repo.zip`, then in GitHub
**Add file → Upload files**, and drag **`index.html`, `vercel.json` and the `api`
folder** in one go → Commit. GitHub keeps folders, so `api/decompile.py` lands
where it belongs.

If you would rather add the server by hand:

**In the browser (can't go wrong):**

1. GitHub → your repo → **Add file → Create new file**
2. Filename box, type exactly:
   ```
   api/decompile.py
   ```
   The `/` is what creates the `api` folder — the box will show `api / decompile.py`.
3. Open **`vercel-deploy/api/decompile.py`** in your workspace viewer → **Ctrl+A** → **Ctrl+C**
4. Click GitHub's text box → **Ctrl+V**
5. **Commit changes**

**Or by upload:** unzip **`oracle-repo.zip`** first (dragging files straight out of a
zip preview is what dropped the folder last time), then **Add file → Upload files**
and drag `index.html`, `vercel.json` and the `api` folder onto the page → Commit.

After that the repo root holds `index.html`, the icons, `vercel.json`, the docs and
an `api/` folder holding exactly one file, `decompile.py`.

## Then

- Vercel rebuilds by itself on the commit (~1 min).
- Check `https://oracle-decompiler.vercel.app/api/decompile` →
  `{"ok": true, "service": "Oracle decompiler endpoint", ...}` = the server is live.
- Open the site: the red bar is gone, and dropping your game decompiles it.

If the build still ends in the `unmatched-function-pattern` error, your repo is still
holding the **old** `vercel.json` — replace it with the one from `oracle-repo.zip`
(no `functions` block in it).

## Quick sanity table

| In the repo | Result |
|---|---|
| new `index.html` + `api/decompile.py` + new `vercel.json` | ✅ site + working server |
| `index.html` + new `vercel.json`, no `api/` | 🟡 site loads, red bar says the server is missing |
| `index.html` + old `vercel.json` (with `functions` block), no `api/` | ❌ build fails: unmatched function pattern |
