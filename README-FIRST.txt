================================================================
 ORACLE DECOMPILER - fix the Vercel build (2 minutes)
================================================================

That build error happened for two reasons, both of them about
WHERE files are, not what they contain:

  1. Everything was uploaded INTO the api folder.
     Vercel only serves the website from the TOP of the repo.
     Files at api/index.html are invisible to the site builder.
     (Vercel's own detection says: api/decompile.py -> function,
     nothing else -> no website at all.)

  2. A requirements.txt at the TOP of the repo made Vercel
     treat the whole project as a Python APP. Apps need an
     entrypoint (app.py / main.py / server.py ...) and ours is
     a function, so the build stopped with
     "No python entrypoint found in default locations".
     There is no requirements.txt in this zip on purpose.

WHAT THE REPO MUST LOOK LIKE (exactly)

    (repo root)
      index.html              <- the site lives at the top
      favicon.ico
      icon.png
      apple-touch-icon.png
      vercel.json
      README-FIRST.txt        (this file - optional)
      api/
        decompile.py          <- ONLY this inside api

  No requirements.txt. No pyproject.toml. No Pipfile.

----------------------------------------------------------------
FIX IT - 4 steps
----------------------------------------------------------------

1. In GitHub, delete the wrong files:
     open the api folder -> tick each file that is NOT
     decompile.py (index.html, favicon.ico, icon.png,
     apple-touch-icon.png, vercel.json, requirements.txt,
     the docs) -> Delete -> Commit.
   Still inside api: decompile.py should be the only file left.

2. Unzip this file, open the folder, press Ctrl+A inside it
   (you get index.html + the api folder + the icons),
   then in GitHub: Add file -> Upload files -> drop them -> Commit.
   GitHub keeps folders, so api/decompile.py stays where it belongs.

3. In Vercel: Settings -> General
     Framework Preset : Other        <- NOT "Python"
     Root Directory   : (leave empty)
   Save it. If the preset is stuck on "Python" from the earlier
   attempt, this is the button that clears it.

4. Vercel -> Deployments -> Redeploy (or just wait; the commit
   in step 2 already starts a new deployment).

----------------------------------------------------------------
CHECK IT (5 seconds)
----------------------------------------------------------------

  Open:  https://oracle-decompiler.vercel.app/api/decompile

  {"ok": true, "service": "Oracle decompiler endpoint", ...}
        -> fixed. Drop your game on the site; it decompiles.

  The page could not be found / NOT_FOUND
        -> decompile.py is still not inside an "api" folder at
           the TOP of the repo.

  Build fails again
        -> Settings -> General: Framework Preset must be Other,
           and make sure no requirements.txt / pyproject.toml /
           Pipfile exists at the top of the repo.

----------------------------------------------------------------
ONE TRADE-OFF, SAID PLAINLY
----------------------------------------------------------------

Without requirements.txt the website cannot install one optional
package (zstandard). Roblox saves its maps with LZ4, which this
handles in pure Python - only places that use ZSTD compression
need that package, and those can be opened with the local
Oracle.html instead. Everything else is unaffected.
