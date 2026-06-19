#!/usr/bin/env bash
# Regenerate studio_pipe.py and push it into the RUNNING Open WebUI function, then reload.
#
# Why this exists: OWUI stores the pipe CODE in its own DB (the `function` table), NOT read from
# the file on disk. So editing build_studio_pipe.py + regenerating studio_pipe.py is NOT enough —
# the installed function stays on the old code (the "stale function" trap). This script:
#   1. builds studio_pipe.py
#   2. copies it into the open-webui container + UPDATEs the function row's `content`
#   3. restarts open-webui so it reloads the function (also refreshes the lane picker)
#
# Prereq: the Studio function must already be installed once (Admin → Functions → + → paste
# studio_pipe.py). After that, use this to push every update.
#
# Usage:
#   bash services/studio/push-pipe-to-owui.sh                 # build → push → reload
#   bash services/studio/push-pipe-to-owui.sh --no-reload     # build → push, but don't restart OWUI
#   bash services/studio/push-pipe-to-owui.sh <function_id> <owui_container>   # defaults: studio open-webui
# Env: OWUI_DB (default /app/backend/data/webui.db) · DOCKER (default "sudo docker") · OWUI_HEALTH_URL
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RELOAD=1
POS=()
for a in "$@"; do
    case "$a" in
        --no-reload) RELOAD=0 ;;
        *) POS+=("$a") ;;
    esac
done
FN_ID="${POS[0]:-studio}"
OWUI="${POS[1]:-open-webui}"
DB="${OWUI_DB:-/app/backend/data/webui.db}"
DOCKER="${DOCKER:-sudo docker}"
HEALTH_URL="${OWUI_HEALTH_URL:-http://localhost:8080/health}"

echo "[push] building studio_pipe.py …"
python3 "$HERE/build_studio_pipe.py"
PIPE="$HERE/studio_pipe.py"
[ -f "$PIPE" ] || { echo "[push] ERROR: $PIPE not found"; exit 1; }
echo "[push] pipe = $(wc -c < "$PIPE") bytes"

$DOCKER ps --format '{{.Names}}' | grep -qx "$OWUI" || { echo "[push] ERROR: container '$OWUI' is not running"; exit 1; }

echo "[push] copy → $OWUI : updating function '$FN_ID' in $DB …"
$DOCKER cp "$PIPE" "$OWUI:/tmp/_studio_pipe_push.py"
$DOCKER exec -e FN_ID="$FN_ID" -e DB="$DB" "$OWUI" python -c '
import os, sqlite3, time, sys
fn_id, db = os.environ["FN_ID"], os.environ["DB"]
new = open("/tmp/_studio_pipe_push.py").read()
c = sqlite3.connect(db); cur = c.cursor()
if not cur.execute("SELECT 1 FROM function WHERE id=?", (fn_id,)).fetchone():
    sys.exit("[push] ERROR: function id %r not in the DB — install it once via Admin -> Functions." % fn_id)
cur.execute("UPDATE function SET content=?, updated_at=? WHERE id=?", (new, int(time.time()), fn_id))
c.commit()
print("[push] updated row:", cur.execute("SELECT id,name,is_active,length(content) FROM function WHERE id=?", (fn_id,)).fetchone())
c.close()
'
$DOCKER exec "$OWUI" rm -f /tmp/_studio_pipe_push.py 2>/dev/null || true

if [ "$RELOAD" = 1 ]; then
    echo "[push] restarting $OWUI to reload the function …"
    $DOCKER restart "$OWUI" >/dev/null
    for i in $(seq 1 30); do
        curl -sf -m 3 "$HEALTH_URL" >/dev/null 2>&1 && { echo "[push] ✓ OWUI up — pipe is live"; exit 0; }
        sleep 3
    done
    echo "[push] WARN: OWUI didn't report healthy in time — check '$DOCKER logs $OWUI'"
else
    echo "[push] --no-reload: DB updated. Restart OWUI to make it live: $DOCKER restart $OWUI"
fi
