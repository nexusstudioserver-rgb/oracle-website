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
UPDATE - YOUR SITE IS ONE FILE AWAY
----------------------------------------------------------------

Your new repo (oracle-website) has the site files at the top -
good. What is still missing is the api folder, so:

  * Vercel had nothing to build as a function, and
  * vercel.json declared a function that did not exist, which
    is a HARD build error:
      The pattern "api/decompile.py" defined in `functions`
      doesn't match any Serverless Functions.

Both are handled now:
  * vercel.json no longer declares that function (nothing left
    in it that can fail if a file is missing), and
  * if the server file is absent the site still builds - it just
    shows the red bar saying the server is missing.

SO: you only need to add ONE file to the repo.

  In GitHub: Add file -> Create new file
  Filename:  api/decompile.py      (typing the / makes the folder)
  Then:      open api/decompile.py in your workspace viewer,
             Ctrl+A, Ctrl+C, click GitHub's text box, Ctrl+V
  Then:      Commit changes

That is the whole fix. Vercel rebuilds automatically.

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
ABOUT requirements.txt (READ THIS ONCE)
----------------------------------------------------------------

There is no requirements.txt here on purpose, and none is
needed. Both kinds of Roblox compression are handled by
decompile.py itself, in plain Python:

  * LZ4  - the older saves
  * ZSTD - the newer saves (a ZSTD decoder is built into the
           file, so the website needs no extra package)

So the website decompiles ZSTD places by itself, with no
requirements.txt, no pip install and no local engine. Adding a
requirements.txt back would only re-break the build.
