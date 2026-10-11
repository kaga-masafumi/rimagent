"""Small local dashboard for human-directed commands; polling never invokes an LLM."""
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


class Instruction(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    request_id: str = Field(min_length=1, max_length=128)
    permissions: list[str] = Field(default_factory=list)
    answer_to: str | None = None


class Control(BaseModel):
    action: str
    speed: int | None = None


def create_app(controller):
    @asynccontextmanager
    async def lifespan(app):
        async def poll():
            while not controller.stop_event.is_set():
                try:
                    st = await asyncio.to_thread(controller.bridge.status, timeout_ms=2000)
                    with controller.lock:
                        controller.game = st
                except Exception:
                    with controller.lock:
                        controller.game = None
                await asyncio.sleep(2)
        task = asyncio.create_task(poll())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "同じ画面から操作してください。"}, status_code=403)
        return await call_next(request)

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            from .bridge import BridgeError
            from .usage import LLMBlocked
            message = str(exc) if isinstance(exc, (RuntimeError, BridgeError, LLMBlocked)) else type(exc).__name__
            raise HTTPException(409, message) from exc

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML

    @app.get("/api/state")
    def state():
        return controller.snapshot()

    @app.get("/api/usage")
    def usage():
        return invoke(controller.usage)

    @app.post("/api/commands")
    def command(body: Instruction):
        return invoke(controller.submit, body.text, body.request_id, body.permissions, answer_to=body.answer_to)

    @app.post("/api/control")
    def control(body: Control):
        return invoke(controller.control, body.action, body.speed)

    @app.get("/screenshot.png")
    def screenshot():
        from fastapi.responses import Response
        return Response(invoke(controller.bridge.screenshot), media_type="image/png")

    return app


HTML = r"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RimWorld 操作アシスト</title><style>
body{font:16px system-ui;background:#17202b;color:#edf2f8;max-width:1000px;margin:auto;padding:24px}
section{background:#243242;padding:18px;border-radius:12px;margin:16px 0}button,textarea,select{font:inherit;padding:10px;border-radius:6px;border:1px solid #64788d}
button{cursor:pointer;margin:4px;background:#d7e8ff;color:#102035}button:disabled{opacity:.45;cursor:default}textarea{box-sizing:border-box;width:100%;background:#14202d;color:white}
pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#bfd0e2}#err{color:#ffb6a4}img{max-width:100%}label{margin-right:12px}summary{cursor:pointer}
</style><h1>RimWorld 操作アシスト</h1><p>あなたが判断し、LLMが指示を設定します。作業中・待機中のAPI要求はありません。</p>
<section><strong id="phase">接続中…</strong><p id="game"></p><button onclick="control('pause')">停止して指示</button>
<select id="speed"><option value="1">1倍速</option><option value="2">2倍速</option><option value="3">3倍速</option></select>
<button id="resume" onclick="control('resume')">再開</button><button onclick="control('cancel')">今回の指示を中断</button><button onclick="control('kill')">終了</button><p id="err"></p></section>
<section><p id="question"></p><textarea id="text" rows="4" placeholder="例：3人分のベッドを、この拠点内に木材で用意して"></textarea>
<p><small>今回の指示で許可する継続的な自動処理（通常は選択不要）</small></p>
<label><input type="checkbox" value="scorer" class="permission">仕事優先度</label><label><input type="checkbox" value="orders" class="permission">Standing orders</label><label><input type="checkbox" value="stock" class="permission">在庫処理</label>
<p><button id="send" onclick="send()">指示を送る</button><small>API上限20回・操作上限40回。正常に設定できたら自動再開します。</small></p><pre id="report"></pre></section>
<section><h2>使用量</h2><p id="usage"></p><p id="command-cost"></p></section>
<section><details><summary>有効な自動処理</summary><pre id="steward"></pre></details><p id="notice"></p>
<details><summary>今回の操作履歴</summary><pre id="actions"></pre></details><details><summary>指示履歴</summary><pre id="history"></pre></details></section>
<section><button onclick="document.getElementById('map').src='/screenshot.png?t='+Date.now()">マップ画像を更新（LLM送信なし）</button><img id="map" alt="マップ画像"></section>
<script>
let current=null,pending=null;const $=id=>document.getElementById(id);const labels={paused:'停止・指示待ち',executing:'指示を設定中',running:'ゲーム進行中・LLM待機',question:'回答待ち',stopped:'終了'};
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw Error(j.detail||'操作失敗');return j}
async function control(action){try{await post('/api/control',{action,speed:action==='resume'?Number($('speed').value):null});$('err').textContent='';await refresh()}catch(e){$('err').textContent=e.message}}
async function send(){const text=$('text').value.trim();if(!text)return;const permissions=[...document.querySelectorAll('.permission:checked')].map(e=>e.value);
if(!pending||pending.text!==text)pending={request_id:crypto.randomUUID(),text,permissions,answer_to:current&&current.status==='question'?current.id:null};$('send').disabled=true;
try{await post('/api/commands',pending);pending=null;$('text').value='';document.querySelectorAll('.permission').forEach(e=>e.checked=false);$('err').textContent=''}catch(e){$('err').textContent=e.message}await refresh()}
async function refresh(){try{const s=await(await fetch('/api/state')).json();current=s.current;$('phase').textContent=labels[s.phase]||s.phase;
const g=s.game;$('game').textContent=g?`${g.date||''} 入植者 ${g.colonists??'?'}人 / ゲーム ${g.paused?'停止':'進行'}`:'ゲーム状態を取得できません。バックグラウンド実行と接続を確認してください。';
$('send').disabled=s.busy||s.phase==='stopped';$('resume').disabled=s.busy||s.phase==='question'||s.phase==='stopped';$('send').textContent=current&&current.status==='question'?'回答を送る':'指示を送る';
$('question').textContent=current?.question||'';$('report').textContent=current?.report||'';$('actions').textContent=JSON.stringify(current?.actions||[],null,2);$('steward').textContent=JSON.stringify(s.steward,null,2);$('notice').textContent=s.notice;
$('history').textContent=s.history.map(c=>`${c.status}: ${c.text||''}\n${c.report||''}`).join('\n\n');if(s.error)$('err').textContent=s.error;
const u=await(await fetch('/api/usage')).json();$('usage').textContent=(u.priced?'':'現在のモデルは料金未設定。以下は単価設定済み要求のみ。 ')+`日次 $${u.daily_usd} / $${u.daily_limit_usd}、月次 $${u.monthly_usd} / $${u.monthly_limit_usd}、予約 $${u.reserved_usd}`+(u.hold_reason?' / 停止理由: '+u.hold_reason:'');
const cost=current&&u.commands?.[current.id];$('command-cost').textContent=current?`今回 API ${current.api_calls}回 / 操作 ${current.operations}回 / 推定 ${cost&&cost.cost_usd===null?'料金未設定':'$'+(cost?.cost_usd||'0')} / 予約 $${cost?.reserved_usd||'0'}`:'';
}catch(e){$('err').textContent='画面との接続が終了しました。'+e.message}}
refresh();setInterval(refresh,2000);
</script></html>"""
