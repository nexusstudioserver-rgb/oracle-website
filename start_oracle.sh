#!/usr/bin/env sh
# Oracle - start the local decompiler engine (macOS / Linux)
cd "$(dirname "$0")" || exit 1

echo "============================================================"
echo " Oracle - starting the local decompiler engine"
echo "============================================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3 and try again."
  exit 1
fi

python3 oracle_server.py --port 8787 &
ENGINE_PID=$!
trap 'kill $ENGINE_PID 2>/dev/null' INT TERM

sleep 1
echo "Opening Oracle.html in your browser..."
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open ./Oracle.html >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  open ./Oracle.html
fi

echo
echo "Engine is running. Leave this window OPEN while you use the tool."
echo "Type ANY key on the login screen - no key is ever checked."
echo "Press Ctrl+C to stop."
echo
wait $ENGINE_PID
