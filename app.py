import os, io, sqlite3, secrets
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, request, session, redirect, g, jsonify, send_file, abort
from werkzeug.security import generate_password_hash as H, check_password_hash as C
from markupsafe import escape as E

TZ = ZoneInfo("Europe/Madrid")
DB = os.environ.get("DB", "fichapro.db")
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-en-produccion")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("HTTPS") == "1")
PLANS = {"basico": 10, "profesional": 50, "avanzado": 1000}  # máx. trabajadores
L = {"entrada": "ENTRADA", "inicio_descanso": "INICIO DE DESCANSO", "fin_descanso": "FIN DE DESCANSO", "salida": "SALIDA"}
ALLOWED = {None: ["entrada"], "salida": ["entrada"], "entrada": ["inicio_descanso", "salida"],
           "fin_descanso": ["inicio_descanso", "salida"], "inicio_descanso": ["fin_descanso"]}
FAILS = {}

SCHEMA = """
create table if not exists companies(id integer primary key,name text not null,plan text not null default 'basico',active int not null default 1,created text);
create table if not exists users(id integer primary key,company_id int references companies(id),role text not null,email text unique not null,pw text not null);
create table if not exists workers(id integer primary key,company_id int not null references companies(id),name text not null,dni text,position text,daily_hours real not null default 8,center text default '',alta text,baja text,token text unique not null,pin text not null);
create table if not exists punches(id integer primary key,company_id int not null,worker_id int not null references workers(id),type text not null,ts text not null);
create table if not exists rectifications(id integer primary key,company_id int not null,worker_id int not null,kind text not null,punch_id int,new_type text,new_ts text,reason text not null,user_id int,ts text not null);
create table if not exists audit(id integer primary key,company_id int,user_id int,action text,detail text,ts text);
create index if not exists ix_p on punches(company_id,worker_id,ts);
"""

def init():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    for t in ("punches", "rectifications", "audit"):  # registros inmutables
        for op in ("update", "delete"):
            c.execute(f"create trigger if not exists {t}_{op[0]} before {op} on {t} begin select raise(abort,'registro inmutable'); end")
    if not c.execute("select 1 from users where role='platform'").fetchone():
        pw = os.environ.get("PLATFORM_PASSWORD") or secrets.token_urlsafe(12)
        em = os.environ.get("PLATFORM_EMAIL", "admin@fichapro.local")
        c.execute("insert into users(role,email,pw) values('platform',?,?)", (em, H(pw)))
        print(f"ADMIN PLATAFORMA: {em} / {pw}")
    c.commit(); c.close()

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB, isolation_level=None); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(e):
    d = g.pop("db", None)
    if d: d.close()

def now(): return datetime.now(timezone.utc)
def iso(d): return d.astimezone(timezone.utc).isoformat()
def hm(s): s = int(s); return f"{s//3600}:{s%3600//60:02d}"
def aud(cid, action, detail=""):
    db().execute("insert into audit(company_id,user_id,action,detail,ts) values(?,?,?,?,?)",
                 (cid, session.get("uid"), action, detail, iso(now())))

def need(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if session.get("role") not in roles:
                return (jsonify(error="No autorizado"), 401) if request.path.startswith("/api") else redirect("/login")
            return f(*a, **k)
        return w
    return deco

# ---------- lógica de jornada ----------
def events(cid, wid, since=None):
    d = db()
    voided = {r[0] for r in d.execute("select punch_id from rectifications where kind='void' and worker_id=? and company_id=?", (wid, cid))}
    ev = [dict(ts=datetime.fromisoformat(r["ts"]), type=r["type"], id=r["id"]) for r in
          d.execute("select id,type,ts from punches where company_id=? and worker_id=?", (cid, wid)) if r["id"] not in voided]
    ev += [dict(ts=datetime.fromisoformat(r["new_ts"]), type=r["new_type"], id=None) for r in
           d.execute("select new_type,new_ts from rectifications where kind='add' and worker_id=? and company_id=?", (wid, cid))]
    ev.sort(key=lambda e: e["ts"])
    return [e for e in ev if not since or e["ts"] >= since]

def summarize(evs, dh, ref):
    days, cur = {}, None
    R = lambda d: days.setdefault(d, dict(s=[], brk=0, w=0, inc=[]))
    def close_(c, out):
        r = R(c["d"]); br = c["brk"]
        if c["bs"]: br += (out - c["bs"]).total_seconds(); r["inc"].append("Descanso sin cerrar")
        r["s"].append((c["i"], out)); r["brk"] += br; r["w"] += (out - c["i"]).total_seconds() - br
    for e in evs:
        t, ts = e["type"], e["ts"]; d = ts.astimezone(TZ).date()
        if t == "entrada":
            if cur: R(cur["d"])["inc"].append("Entrada sin salida previa")
            cur = dict(i=ts, d=d, brk=0, bs=None)
        elif not cur: R(d)["inc"].append("Fichaje sin entrada: " + L[t])
        elif t == "inicio_descanso": cur["bs"] = ts
        elif t == "fin_descanso":
            if cur["bs"]: cur["brk"] += (ts - cur["bs"]).total_seconds(); cur["bs"] = None
        elif t == "salida": close_(cur, ts); cur = None
    if cur:
        if ref - cur["i"] > timedelta(hours=16): R(cur["d"])["inc"].append("Jornada sin cerrar")
        else: close_(cur, ref); R(cur["d"])["inc"].append("En curso")
    for r in days.values(): r["ot"] = max(0, r["w"] - dh * 3600)
    return days

def wstate(evs): return evs[-1]["type"] if evs else None

# ---------- UI ----------
CSS = ("body{font:16px system-ui,sans-serif;background:#f3f5f8;margin:0;color:#1b2430}header{background:#0b2a5b;color:#fff;padding:14px 20px}"
       "main{max-width:1000px;margin:auto;padding:12px}.c{background:#fff;border-radius:10px;padding:16px;margin:12px 0;box-shadow:0 1px 3px #0002}"
       "button,.b{background:#0b2a5b;color:#fff;border:0;border-radius:8px;padding:9px 14px;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block}"
       ".g{background:#16a34a}.big{width:100%;padding:26px;font-size:22px;margin:8px 0}input,select{padding:9px;border:1px solid #c5ccd6;border-radius:8px;margin:3px;font-size:15px}"
       "table{width:100%;border-collapse:collapse}td,th{padding:7px;border-bottom:1px solid #e3e7ee;text-align:left;font-size:14px}.t{overflow-x:auto}small{color:#5b6675}")
JS0 = "const J=(u,m,d)=>fetch(u,{method:m||'GET',headers:{'Content-Type':'application/json'},body:d?JSON.stringify(d):undefined}).then(async r=>({ok:r.ok,d:await r.json().catch(()=>({}))}));"

def page(t, body, js=""):
    return (f"<!doctype html><html lang=es><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{t}</title><style>{CSS}</style><header><b>FichaPro</b> · {t}</header><main>{body}</main><script>{JS0}{js}</script></html>")

@app.get("/")
def index(): return redirect({"platform": "/platform", "admin": "/admin"}.get(session.get("role"), "/login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        u = db().execute("select u.*,c.active from users u left join companies c on c.id=u.company_id where email=?",
                         (request.form.get("email", "").strip().lower(),)).fetchone()
        if u and C(u["pw"], request.form.get("password", "")) and (u["role"] == "platform" or u["active"]):
            session.clear(); session.update(uid=u["id"], cid=u["company_id"], role=u["role"]); return redirect("/")
        err = "<p style=color:#b91c1c>Credenciales incorrectas o cuenta suspendida</p>"
    return page("Acceso", f"<div class=c><h2>Acceso empresa</h2>{err}<form method=post><input name=email type=email placeholder=Email required><br>"
                "<input name=password type=password placeholder=Contraseña required><br><button>Entrar</button></form></div>")

@app.get("/logout")
def logout(): session.clear(); return redirect("/login")

WJS = """const L={entrada:'ENTRADA',inicio_descanso:'INICIO DE DESCANSO',fin_descanso:'FIN DE DESCANSO',salida:'SALIDA'};
const S={entrada:'Trabajando',fin_descanso:'Trabajando',inicio_descanso:'En descanso'};
setInterval(()=>clk.textContent=new Date().toLocaleTimeString('es-ES',{timeZone:'Europe/Madrid'}),500);
async function go(t){const r=await J('/api/punch','POST',{token:TOK,pin:p.value,type:t});
if(!r.ok){ms.style.color='#b91c1c';ms.textContent=r.d.error||'Error';return}
n.textContent=r.d.name;st.textContent='Estado: '+(S[r.d.state]||'Fuera de jornada');
if(t){ms.style.color='#16a34a';ms.textContent='✔ '+L[t]+' registrada a las '+r.d.time}else ms.textContent='';
bt.innerHTML=r.d.allowed.map(a=>'<button class="big '+(a=='salida'?'':'g')+'" onclick="go(\\''+a+'\\')">'+L[a]+'</button>').join('')}"""

@app.get("/f/<token>")
def fichar_page(token):
    w = db().execute("select name from workers where token=?", (token,)).fetchone()
    if not w: abort(404)
    return page("Fichaje", "<div class=c style='text-align:center'><h2 id=n>Identifícate</h2><h1 id=clk></h1><p id=st></p>"
                "<input id=p type=password inputmode=numeric placeholder='Tu PIN'> <button onclick=go()>Continuar</button><div id=bt></div><p id=ms></p>"
                "<small>Se registra la fecha y hora del servidor. Sin geolocalización ni reconocimiento facial. "
                "Pide a tu empresa la información de privacidad.</small></div>", f"const TOK='{token}';" + WJS)

@app.post("/api/punch")
def punch():
    j = request.get_json(silent=True) or {}
    d = db()
    w = d.execute("select w.*,c.active from workers w join companies c on c.id=w.company_id where token=?", (j.get("token"),)).fetchone()
    if not w: abort(404)
    f = FAILS.get(w["id"], [0, now()])
    if now() - f[1] > timedelta(minutes=10): f = [0, now()]
    if f[0] >= 5: return jsonify(error="Demasiados intentos. Espera 10 minutos."), 429
    if not C(w["pin"], str(j.get("pin", ""))):
        FAILS[w["id"]] = [f[0] + 1, f[1]]; return jsonify(error="PIN incorrecto"), 403
    FAILS.pop(w["id"], None)
    if not w["active"] or (w["baja"] and datetime.now(TZ).date().isoformat() > w["baja"]):
        return jsonify(error="Trabajador de baja o empresa suspendida"), 403
    t = j.get("type"); d.execute("begin immediate")
    try:
        evs = events(w["company_id"], w["id"]); st = wstate(evs)
        if t:
            if t not in ALLOWED[st]:
                d.execute("rollback"); return jsonify(error=f"Secuencia no válida: no puedes registrar {L.get(t, t)} ahora"), 409
            ts = now(); d.execute("insert into punches(company_id,worker_id,type,ts) values(?,?,?,?)", (w["company_id"], w["id"], t, iso(ts)))
            st = t
        d.execute("commit")
    except Exception: d.execute("rollback"); raise
    return jsonify(name=w["name"], state=st, allowed=ALLOWED[st], time=datetime.now(TZ).strftime("%H:%M:%S"))

# ---------- panel empresa ----------
AJS = """let W=[];const T=t=>t?t:'-';
async function load(){const r=await J('/api/status?center='+encodeURIComponent(ce.value));W=r.d.workers||[];
tb.innerHTML=W.map(w=>`<tr><td>${w.name}<br><small>${w.center}</small></td><td>${w.state}</td><td>${T(w.first)}</td><td>${w.today}</td><td>${w.week}</td><td>${w.month}</td><td>${w.ot}</td><td>${w.inc}</td><td><button onclick="hist(${w.id})">Historial</button><br><small>${location.origin}/f/${w.token}</small></td></tr>`).join('');
sel.innerHTML='<option value=all>Toda la plantilla</option>'+W.map(w=>`<option value=${w.id}>${w.name}</option>`).join('')}
async function hist(id){const r=await J('/api/history?worker='+id);hs.innerHTML='<table><tr><th>Fecha</th><th>Tipo</th><th></th></tr>'+r.d.punches.map(p=>`<tr><td>${p.ts}</td><td>${p.type}${p.voided?' (ANULADO)':''}</td><td>${p.voided?'':`<button onclick="vd(${id},${p.id})">Anular</button>`}</td></tr>`).join('')+'</table><b>Rectificaciones</b><br>'+r.d.rects.map(x=>`${x.at}: ${x.kind} ${x.ts||''} — ${x.reason} (${x.by})`).join('<br>')}
async function vd(w,p){const m=prompt('Motivo de la anulación (obligatorio)');if(!m)return;await J('/api/rectify','POST',{worker_id:w,kind:'void',punch_id:p,reason:m});hist(w);load()}
async function add(){const r=await J('/api/rectify','POST',{worker_id:+sel2.value,kind:'add',type:ty.value,local_ts:dt.value,reason:rs.value});alert(r.ok?'Rectificación registrada':r.d.error);load()}
async function nw(){const r=await J('/api/workers','POST',{name:nm.value,dni:dni.value,position:ps.value,daily_hours:+dh.value||8,center:cn.value,alta:al.value,baja:bj.value});
out.innerHTML=r.ok?'Enlace: <b>'+location.origin+'/f/'+r.d.token+'</b><br>PIN: <b>'+r.d.pin+'</b> (entrégalo al trabajador; no se vuelve a mostrar)':r.d.error;load()}
function rep(f){location='/api/report?worker='+sel.value+'&from='+fr.value+'&to='+to.value+'&center='+encodeURIComponent(ce.value)+'&fmt='+f}
ce.onchange=load;load();setInterval(load,30000);"""

@app.get("/admin")
@need("admin")
def admin():
    co = db().execute("select name,plan from companies where id=?", (session["cid"],)).fetchone()
    return page(f"Panel · {E(co['name'])} (plan {co['plan']})",
        "<div class=c><b>Centro:</b><input id=ce placeholder='todos'> <a href=/logout class=b style=float:right>Salir</a><div class=t><table><thead><tr><th>Trabajador</th><th>Estado</th><th>Entrada hoy</th><th>Hoy</th><th>Semana</th><th>Mes</th><th>Extra mes</th><th>Incid.</th><th></th></tr></thead><tbody id=tb></tbody></table></div></div>"
        "<div class=c><h3>Informes (Inspección de Trabajo)</h3><select id=sel></select> Desde<input id=fr type=date> Hasta<input id=to type=date> <button onclick=\"rep('pdf')\">PDF</button> <button onclick=\"rep('xlsx')\">Excel</button></div>"
        "<div class=c><h3>Historial / rectificaciones</h3><div id=hs></div><hr><b>Añadir fichaje omitido</b><br><select id=sel2></select><select id=ty><option value=entrada>Entrada<option value=inicio_descanso>Inicio descanso<option value=fin_descanso>Fin descanso<option value=salida>Salida</select>"
        "<input id=dt type=datetime-local><input id=rs placeholder='Motivo (obligatorio)'><button onclick=add()>Registrar</button></div>"
        "<div class=c><h3>Nuevo trabajador</h3><input id=nm placeholder=Nombre><input id=dni placeholder=DNI/NIE><input id=ps placeholder=Puesto><input id=dh placeholder='Horas/día' size=8><input id=cn placeholder=Centro>"
        "Alta<input id=al type=date>Baja<input id=bj type=date><button onclick=nw()>Crear</button><p id=out></p></div>",
        AJS + "sel2.innerHTML='';setTimeout(()=>sel2.innerHTML=sel.innerHTML.replace('<option value=\"all\">Toda la plantilla</option>',''),800);")

@app.get("/api/status")
@need("admin")
def status():
    cid, ctr = session["cid"], request.args.get("center", "")
    n = now(); today = n.astimezone(TZ).date(); ws = today - timedelta(days=today.weekday()); ms = today.replace(day=1)
    out = []
    for w in db().execute("select * from workers where company_id=? and (?='' or center=?) and (baja is null or baja='' or baja>=?) order by name", (cid, ctr, ctr, today.isoformat())):
        ev = events(cid, w["id"], n - timedelta(days=40)); days = summarize(ev, w["daily_hours"], n); st = wstate(ev)
        tr = days.get(today); s = lambda a: sum(r["w"] for d, r in days.items() if d >= a)
        out.append(dict(id=w["id"], name=str(E(w["name"])), center=str(E(w["center"] or "")), token=w["token"],
                        state={"entrada": "Trabajando", "fin_descanso": "Trabajando", "inicio_descanso": "En descanso"}.get(st, "Fuera" if tr else "Sin fichar hoy"),
                        first=tr["s"][0][0].astimezone(TZ).strftime("%H:%M") if tr and tr["s"] else None,
                        today=hm(tr["w"]) if tr else "0:00", week=hm(s(ws)), month=hm(s(ms)),
                        ot=hm(sum(r["ot"] for d, r in days.items() if d >= ms)),
                        inc=sum(1 for r in days.values() for i in r["inc"] if i != "En curso")))
    return jsonify(workers=out)

@app.post("/api/workers")
@need("admin")
def new_worker():
    j = request.get_json(silent=True) or {}; cid = session["cid"]; d = db()
    co = d.execute("select plan from companies where id=?", (cid,)).fetchone()
    if d.execute("select count(*) from workers where company_id=?", (cid,)).fetchone()[0] >= PLANS[co["plan"]]:
        return jsonify(error="Límite de trabajadores del plan alcanzado"), 403
    if not str(j.get("name", "")).strip(): return jsonify(error="Nombre obligatorio"), 400
    pin, tok = f"{secrets.randbelow(10**6):06d}", secrets.token_urlsafe(24)
    d.execute("insert into workers(company_id,name,dni,position,daily_hours,center,alta,baja,token,pin) values(?,?,?,?,?,?,?,?,?,?)",
              (cid, j["name"].strip(), j.get("dni"), j.get("position"), float(j.get("daily_hours") or 8), j.get("center") or "", j.get("alta"), j.get("baja"), tok, H(pin)))
    aud(cid, "worker_create", j["name"]); return jsonify(token=tok, pin=pin)

def own_worker(wid):
    w = db().execute("select * from workers where id=? and company_id=?", (wid, session["cid"])).fetchone()
    if not w: abort(404)
    return w

@app.get("/api/history")
@need("admin")
def history():
    w = own_worker(request.args.get("worker")); cid = session["cid"]; f = lambda s: datetime.fromisoformat(s).astimezone(TZ).strftime("%d/%m/%Y %H:%M:%S")
    d = db(); voided = {r[0] for r in d.execute("select punch_id from rectifications where kind='void' and worker_id=?", (w["id"],))}
    p = [dict(id=r["id"], type=L[r["type"]], ts=f(r["ts"]), voided=r["id"] in voided) for r in d.execute("select * from punches where company_id=? and worker_id=? order by ts desc limit 200", (cid, w["id"]))]
    r = [dict(kind=x["kind"], ts=x["new_ts"] and f(x["new_ts"]), reason=str(E(x["reason"])), by=x["email"], at=f(x["ts"])) for x in
         d.execute("select r.*,u.email from rectifications r left join users u on u.id=r.user_id where r.worker_id=? and r.company_id=? order by r.id desc", (w["id"], cid))]
    return jsonify(punches=p, rects=r)

@app.post("/api/rectify")
@need("admin")
def rectify():
    j = request.get_json(silent=True) or {}; w = own_worker(j.get("worker_id")); cid = session["cid"]; d = db()
    reason = str(j.get("reason", "")).strip()
    if len(reason) < 5: return jsonify(error="El motivo es obligatorio (mín. 5 caracteres)"), 400
    if j.get("kind") == "void":
        p = d.execute("select id from punches where id=? and company_id=? and worker_id=?", (j.get("punch_id"), cid, w["id"])).fetchone()
        if not p: return jsonify(error="Fichaje no encontrado"), 404
        if d.execute("select 1 from rectifications where kind='void' and punch_id=?", (p["id"],)).fetchone(): return jsonify(error="Ya anulado"), 409
        d.execute("insert into rectifications(company_id,worker_id,kind,punch_id,reason,user_id,ts) values(?,?,?,?,?,?,?)", (cid, w["id"], "void", p["id"], reason, session["uid"], iso(now())))
    elif j.get("kind") == "add":
        if j.get("type") not in L: return jsonify(error="Tipo no válido"), 400
        try: nt = datetime.fromisoformat(j["local_ts"]).replace(tzinfo=TZ)
        except Exception: return jsonify(error="Fecha no válida"), 400
        if nt > now(): return jsonify(error="No se pueden registrar fichajes futuros"), 400
        d.execute("insert into rectifications(company_id,worker_id,kind,new_type,new_ts,reason,user_id,ts) values(?,?,?,?,?,?,?,?)", (cid, w["id"], "add", j["type"], iso(nt), reason, session["uid"], iso(now())))
    else: return jsonify(error="Tipo de rectificación no válido"), 400
    aud(cid, "rectify_" + j["kind"], f"worker={w['id']} {reason}"); return jsonify(ok=True)

# ---------- informes ----------
def report_data(cid, wid, a, b, center):
    n = now(); out = []
    q = "select * from workers where company_id=? and (?='all' or id=?) and (?='' or center=?) order by name"
    for w in db().execute(q, (cid, wid, wid if wid != "all" else 0, center, center)):
        days = {d: r for d, r in summarize(events(cid, w["id"]), w["daily_hours"], n).items() if a <= d <= b}
        rects = []
        for x in db().execute("select r.*,u.email,p.ts pts from rectifications r left join users u on u.id=r.user_id left join punches p on p.id=r.punch_id where r.worker_id=? and r.company_id=?", (w["id"], cid)):
            t = datetime.fromisoformat(x["new_ts"] or x["pts"]).astimezone(TZ)
            if a <= t.date() <= b:
                rects.append((datetime.fromisoformat(x["ts"]).astimezone(TZ).strftime("%d/%m/%Y %H:%M"), "Fichaje añadido" if x["kind"] == "add" else "Fichaje anulado",
                              t.strftime("%d/%m/%Y %H:%M"), x["reason"], x["email"]))
        rows = []
        for d in sorted(days):
            r = days[d]; tt = lambda x: x.astimezone(TZ).strftime("%H:%M")
            rows.append((d.strftime("%d/%m/%Y"), " | ".join(f"{tt(i)}-{tt(o)}" for i, o in r["s"]) or "-", hm(r["brk"]), hm(r["w"]), hm(r["ot"]), "; ".join(r["inc"])))
        out.append((w, rows, rects))
    return out

@app.get("/api/report")
@need("admin")
def report():
    cid = session["cid"]; co = db().execute("select name from companies where id=?", (cid,)).fetchone()["name"]
    try:
        a = datetime.fromisoformat(request.args.get("from") or "2000-01-01").date(); b = datetime.fromisoformat(request.args.get("to") or "2999-12-31").date()
    except ValueError: return jsonify(error="Fechas no válidas"), 400
    data = report_data(cid, request.args.get("worker", "all"), a, b, request.args.get("center", ""))
    gen = datetime.now(TZ).strftime("%d/%m/%Y %H:%M"); aud(cid, "report", f"{request.args.get('fmt')} {a}..{b}")
    H_ = ["Fecha", "Entrada-Salida", "Descansos", "Trabajado", "Extra", "Incidencias"]
    if request.args.get("fmt") == "xlsx":
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "Jornadas"
        ws.append(["Empresa", co, "Generado", gen]); ws.append(["Trabajador", "DNI/NIE"] + H_)
        for w, rows, _ in data:
            for r in rows: ws.append([w["name"], w["dni"] or ""] + list(r))
        w2 = wb.create_sheet("Rectificaciones"); w2.append(["Trabajador", "Fecha rectificación", "Acción", "Fichaje afectado", "Motivo", "Responsable"])
        for w, _, rc in data:
            for r in rc: w2.append([w["name"], *r])
        bio = io.BytesIO(); wb.save(bio); bio.seek(0)
        return send_file(bio, as_attachment=True, download_name="registro_jornada.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak
    st = getSampleStyleSheet(); bio = io.BytesIO(); el = []
    for w, rows, rc in data:
        el += [Paragraph(f"Registro de jornada — {E(co)}", st["Title"]),
               Paragraph(f"Trabajador: {E(w['name'])} · DNI/NIE: {E(w['dni'] or '-')} · Periodo: {a if a.year>2000 else 'inicio'} a {b if b.year<2999 else 'hoy'} · Generado: {gen}", st["Normal"]), Spacer(1, 8)]
        t = Table([H_] + [[Paragraph(str(E(c)), st["BodyText"]) for c in r] for r in rows] if rows else [H_, ["Sin registros"] + [""] * 5], repeatRows=1, colWidths=[70, 150, 60, 60, 50, 200])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b2a5b")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), .3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        el.append(t)
        if rc:
            el += [Spacer(1, 10), Paragraph("Rectificaciones", st["Heading3"])]
            el.append(Table([["Fecha", "Acción", "Fichaje afectado", "Motivo", "Responsable"]] + [[Paragraph(str(E(c)), st["BodyText"]) for c in r] for r in rc], colWidths=[80, 80, 90, 280, 100], style=[("GRID", (0, 0), (-1, -1), .3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
        el += [Spacer(1, 8), Paragraph("Registro generado por FichaPro a partir de fichajes reales sin alteración. El cumplimiento normativo es responsabilidad de la empresa.", st["Italic"]), PageBreak()]
    SimpleDocTemplate(bio, pagesize=landscape(A4)).build(el or [Paragraph("Sin trabajadores", st["Normal"])]); bio.seek(0)
    return send_file(bio, as_attachment=True, download_name="registro_jornada.pdf", mimetype="application/pdf")

# ---------- panel de plataforma ----------
PJS = """async function ld(){const r=await J('/api/companies');t.innerHTML=r.d.companies.map(c=>`<tr><td>${c.id}</td><td>${c.name}</td><td>${c.plan}</td><td>${c.workers}</td><td>${c.active?'Activa':'Suspendida'}</td><td><button onclick="tg(${c.id},${c.active?0:1})">${c.active?'Suspender':'Activar'}</button></td></tr>`).join('')}
async function tg(id,a){await J('/api/companies/'+id,'POST',{active:a});ld()}
async function cr(){const r=await J('/api/companies','POST',{name:nm.value,plan:pl.value,email:em.value,password:pw.value});if(!r.ok)alert(r.d.error);ld()}ld();"""

@app.get("/platform")
@need("platform")
def platform():
    return page("Plataforma", "<div class=c><a href=/logout class=b style=float:right>Salir</a><h3>Empresas</h3><table><tbody id=t></tbody></table></div>"
        "<div class=c><h3>Nueva empresa</h3><input id=nm placeholder=Empresa><select id=pl><option>basico<option>profesional<option>avanzado</select><input id=em placeholder='Email admin'><input id=pw type=password placeholder='Contraseña (mín. 10)'><button onclick=cr()>Crear</button></div>", PJS)

@app.get("/api/companies")
@need("platform")
def companies():
    return jsonify(companies=[dict(id=r["id"], name=str(E(r["name"])), plan=r["plan"], active=r["active"], workers=r["n"]) for r in
                              db().execute("select c.*,(select count(*) from workers w where w.company_id=c.id) n from companies c order by id")])

@app.post("/api/companies")
@need("platform")
def new_company():
    j = request.get_json(silent=True) or {}
    if j.get("plan") not in PLANS or not j.get("name") or len(j.get("password", "")) < 10 or "@" not in j.get("email", ""):
        return jsonify(error="Datos no válidos (contraseña mín. 10 caracteres)"), 400
    d = db()
    if d.execute("select 1 from users where email=?", (j["email"].lower(),)).fetchone(): return jsonify(error="Email ya registrado"), 409
    cid = d.execute("insert into companies(name,plan,created) values(?,?,?)", (j["name"], j["plan"], iso(now()))).lastrowid
    d.execute("insert into users(company_id,role,email,pw) values(?,?,?,?)", (cid, "admin", j["email"].lower(), H(j["password"])))
    aud(cid, "company_create", j["name"]); return jsonify(id=cid)

@app.post("/api/companies/<int:cid>")
@need("platform")
def upd_company(cid):
    j = request.get_json(silent=True) or {}
    if "active" in j: db().execute("update companies set active=? where id=?", (int(bool(j["active"])), cid))
    if j.get("plan") in PLANS: db().execute("update companies set plan=? where id=?", (j["plan"], cid))
    aud(cid, "company_update", str(j)); return jsonify(ok=True)

init()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
