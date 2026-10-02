import os, time, json, shlex, difflib, asyncio, zipfile, io
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict

import aiosqlite
import httpx
from loguru import logger
from nicegui import ui, app
from jose import jwt, JWTError

# ========== 0. ENV VALIDATION - BLOCK WEAK SECRETS ==========
SECRET_KEY = os.getenv("SECRET_KEY", "")
if len(SECRET_KEY) < 32 or "change" in SECRET_KEY.lower():
    raise SystemExit("❌ SECRET_KEY must be 32+ random chars. Set in.env")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")

# Logging with redaction
logger.add("logs/rem_ai.log", rotation="10 MB", retention="7 days", filter=lambda r: "sk-" not in r["message"])
DB_PATH = Path("./.rem_snapshots/memory.db")
WORKSPACE_ROOT = Path("./workspace").resolve()
SNAPSHOTS = Path("./.rem_snapshots").resolve()
WORKSPACE_ROOT.mkdir(exist_ok=True); SNAPSHOTS.mkdir(exist_ok=True); Path("logs").mkdir(exist_ok=True)

# ========== 1. SECURE FS WITH VERSIONING ==========
class AgentFSAudit:
    MAX_SIZE = 100 * 1024
    MAX_BACKUPS = 50

    def safe_write(self, user_id: str, rel_path: str, content: str) -> Dict:
        if len(content.encode()) > self.MAX_SIZE:
            raise ValueError(f"File too large >{self.MAX_SIZE//1024}KB")
        user_ws = (WORKSPACE_ROOT / user_id).resolve()
        user_ws.mkdir(exist_ok=True)
        target = (user_ws / rel_path).resolve()
        try:
            target.relative_to(user_ws)
        except ValueError:
            raise PermissionError("Path traversal blocked")

        old = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))

        if old and old!= content:
            (SNAPSHOTS / f"{user_id}_{target.name}.{int(time.time()*1000)}.bak").write_text(old)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.info(f"WRITE user={user_id} file={rel_path} size={len(content)}")
        return {"file": rel_path, "diff": diff or "No change", "timestamp": datetime.now().strftime("%H:%M:%S"), "size_kb": round(len(content)/1024,2)}

    def list_versions(self, user_id: str, filename: str):
        return sorted(SNAPSHOTS.glob(f"{user_id}_{filename}.*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]

# ========== 2. EXECUTOR + MODEL ROUTER ==========
class SecureExecutor:
    ALLOW = {"python", "pip", "ls"}
    def run(self, user_id: str, cmd: str):
        parts = shlex.split(cmd)
        if not parts or Path(parts[0]).name not in self.ALLOW:
            return {"success": False, "stderr": f"Blocked. Allowed: {self.ALLOW}"}
        import subprocess
        cwd = (WORKSPACE_ROOT / user_id).resolve()
        try:
            p = subprocess.run(parts, capture_output=True, text=True, cwd=cwd, timeout=15, shell=False)
            return {"success": p.returncode==0, "stdout": p.stdout[:5000], "stderr": p.stderr[:5000]}
        except subprocess.TimeoutExpired:
            return {"success": False, "stderr": "Timeout 15s"}

class ModelRouter:
    async def generate(self, prompt: str):
        if OPENAI_KEY:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post("https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                        json={"model":"gpt-4o-mini","messages":[{"role":"system","content":"You are senior python engineer. Output only code."},{"role":"user","content":prompt}],"temperature":0.2,"stream": False})
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                logger.error(f"OpenAI fail {e}")
        await asyncio.sleep(0.8)
        return f'from fastapi import FastAPI\napp=FastAPI()\n@app.get("/")\ndef root():\n return {{"status":"ok","task":"{prompt[:40]}"}}'

# ========== 3. DB ==========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, user_id TEXT, text TEXT, created REAL)")
        await db.commit()

async def add_fact(user_id: str, text: str):
    cutoff = time.time() - 7*24*3600
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM facts WHERE created <?", (cutoff,))
        await db.execute("INSERT INTO facts (user_id,text,created) VALUES (?,?,?)", (user_id, text, time.time()))
        await db.commit()

async def get_facts(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT text FROM facts WHERE user_id=? ORDER BY created DESC LIMIT 20",(user_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]

fs = AgentFSAudit()
executor = SecureExecutor()
router = ModelRouter()
deploy_lock = asyncio.Lock()
rate_limit = {} # user_id -> [timestamps]

def check_rate(user_id: str, limit=10):
    now = time.time()
    arr = [t for t in rate_limit.get(user_id, []) if now - t < 60]
    if len(arr) >= limit: return False
    arr.append(now); rate_limit[user_id]=arr; return True

# ========== 4. UI ==========
@ui.page('/health')
def health():
    return {"ok": True, "db": DB_PATH.exists(), "time": datetime.now().isoformat(), "version": "3.1-final"}

@ui.page('/')
def dashboard():
    # Auth
    if not app.storage.user.get('token'):
        with ui.card().classes('absolute-center w-96 p-6 bg-slate-900 border border-slate-700'):
            ui.label('REM AI v3.1 FINAL - LOGIN').classes('text-lg font-bold text-white')
            u = ui.input('Username').classes('w-full')
            p = ui.input('Password', password=True).classes('w-full')
            def login():
                if u.value==ADMIN_USER and p.value==ADMIN_PASS:
                    token = jwt.encode({"sub": u.value, "exp": datetime.utcnow()+timedelta(hours=12)}, SECRET_KEY, algorithm="HS256")
                    app.storage.user['token']=token; app.storage.user['user_id']=u.value
                    ui.navigate.to('/')
                else: ui.notify('Wrong', color='negative')
            ui.button('Login', on_click=login).classes('w-full bg-blue-600 mt-4')
        return

    try:
        payload = jwt.decode(app.storage.user['token'], SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
    except JWTError:
        app.storage.user.clear(); ui.navigate.to('/'); return

    with ui.header().classes('bg-slate-900 text-white p-3 flex justify-between'):
        ui.label(f'REM AI v3.1 FINAL | User: {user_id}').classes('font-bold')
        with ui.row():
            ui.button('Export ZIP', on_click=lambda: download_zip(user_id)).props('flat dense color=white')
            ui.button('Logout', on_click=lambda: (app.storage.user.clear(), ui.navigate.to('/'))).props('flat dense color=white')

    with ui.row().classes('w-full h-[calc(100vh-80px)] p-2 gap-2'):
        # Left
        with ui.column().classes('w-[20%] gap-2'):
            with ui.card().classes('w-full bg-slate-900 border border-slate-800'):
                ui.label('TASKS').classes('text-xs text-slate-400 font-bold')
                task_input = ui.textarea(placeholder='Build a taxi booking API...').classes('w-full text-xs')
                gen_btn = ui.button('GENERATE ▶').classes('w-full bg-blue-600')
                status = ui.label('Idle').classes('text-xs text-green-400')
            with ui.card().classes('w-full bg-slate-900 border border-slate-800'):
                ui.label('VERSION HISTORY').classes('text-xs text-slate-400 font-bold')
                version_list = ui.column().classes('w-full gap-1')
            with ui.card().classes('w-full bg-slate-900 border border-slate-800'):
                ui.label('MEMORY (7-day)').classes('text-xs text-slate-400 font-bold')
                mem_list = ui.column().classes('w-full gap-1 text-xs text-slate-300')

        # Center
        with ui.column().classes('w-[55%]'):
            editor = ui.codemirror('from fastapi import FastAPI\napp=FastAPI()\n@app.get("/")\ndef root():\n return {"status":"REM AI v3.1 Ready"}', language='python').classes('w-full h-[70vh] border border-slate-700')
            with ui.row().classes('w-full gap-2 mt-2'):
                cmd_in = ui.input(placeholder='python -m py_compile app.py').classes('flex-grow text-xs')
                run_cmd_btn = ui.button('RUN').classes('bg-slate-700')
            term = ui.log().classes('w-full h-40 bg-black text-green-400 p-2 text-xs font-mono mt-2')

        # Right
        with ui.column().classes('w-[20%]'):
            with ui.card().classes('w-full bg-slate-950 border border-slate-800'):
                ui.label('DIFF PREVIEW').classes('text-xs text-slate-400 font-bold')
                diff_view = ui.log().classes('w-full h-[60vh] text-blue-300 text-xs')

    def download_zip(uid: str):
        buf = io.BytesIO()
        ws = WORKSPACE_ROOT / uid
        with zipfile.ZipFile(buf, 'w') as z:
            for f in ws.rglob("*"):
                if f.is_file(): z.write(f, f.relative_to(ws))
        buf.seek(0)
        ui.download(buf.read(), f'{uid}-workspace.zip')

    async def refresh_all():
        facts = await get_facts(user_id)
        mem_list.clear()
        with mem_list:
            for f in facts: ui.label(f"• {f}").classes('bg-slate-800 p-1 rounded')
        vers = fs.list_versions(user_id, "app.py")
        version_list.clear()
        with version_list:
            for v in vers[:10]:
                def make_restore(path=v):
                    def do():
                        editor.value = path.read_text()
                        ui.notify(f"Restored {path.name}")
                    return do
                ui.button(v.name[-24:], on_click=make_restore).props('flat dense').classes('text-[10px] text-left w-full')

    async def do_generate():
        if not check_rate(user_id, 10):
            ui.notify('Rate limit 10/min', color='negative'); return
        async with deploy_lock:
            if not task_input.value:
                ui.notify('Enter task', color='warning'); return
            status.set_text('Generating...')
            gen_btn.props('disable')
            try:
                code = await router.generate(task_input.value)
                editor.value = code
                rec = fs.safe_write(user_id, "app.py", code)
                diff_view.clear(); diff_view.push(rec["diff"][:4000])
                term.push(f"[{rec['timestamp']}] Saved {rec['file']} {rec['size_kb']}KB")
                await add_fact(user_id, f"{task_input.value[:30]} -> app.py")
                await refresh_all()
                status.set_text('Done ✓')
            except Exception as e:
                term.push(f"ERROR: {e}"); status.set_text(f"Error: {e}"); logger.error(e)
            finally:
                gen_btn.props(remove='disable')

    gen_btn.on_click(do_generate)
    run_cmd_btn.on_click(lambda: term.push(str(executor.run(user_id, cmd_in.value or "python -m py_compile app.py"))))

    # initial load
    ui.timer(0.5, lambda: asyncio.create_task(refresh_all()), once=True)

@app.on_startup
async def startup():
    await init_db()
    logger.info("REM AI v3.1 FINAL started")

ui.run(title='REM AI v3.1 FINAL', port=8080, storage_secret=SECRET_KEY, reload=False, dark=True)
