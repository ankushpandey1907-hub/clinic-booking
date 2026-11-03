"""Clinic queue booking: patient page, payment confirmation, admin dashboard.

Run:  python app.py        (Python 3.8+, no installs)  ->  http://localhost:8000
Admin: http://localhost:8000/admin

Settings (environment variables):
  CLINIC_NAME       shown on the page
  PAYMENT_LINK      your payment link (Razorpay, Stripe, PayPal, etc.)
  PAYMENT_REF_PARAM optional query-parameter name your provider uses to pass a
                    reference (e.g. client_reference_id for Stripe)
  FEE_TEXT          e.g. "Consultation fee: 500"
  ADMIN_USER / ADMIN_PASSWORD   admin login (change the password!)
  WEBHOOK_SECRET    enables POST /api/webhook for automatic payment confirmation
  PORT, DB_PATH
"""
import base64, hmac, json, os, secrets, sqlite3, threading
from datetime import date, timedelta, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

CLINIC = os.environ.get("CLINIC_NAME", "Care Clinic")
PAY = os.environ.get("PAYMENT_LINK", "")
PAY_REF = os.environ.get("PAYMENT_REF_PARAM", "")
FEE = os.environ.get("FEE_TEXT", "Consultation fee")
AUSER = os.environ.get("ADMIN_USER", "admin")
APASS = os.environ.get("ADMIN_PASSWORD", "change-me")
WSECRET = os.environ.get("WEBHOOK_SECRET", "")
DB = os.environ.get("DB_PATH", "clinic.db")
lock = threading.Lock()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS bookings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, day TEXT NOT NULL,
        name TEXT NOT NULL, phone TEXT NOT NULL, reason TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', token INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP)""")
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN paid TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS u_tok ON bookings(day, token)")
    c.commit(); c.close()


def confirm(bid):
    """Mark a pending booking as paid. Returns True if it changed."""
    c = db()
    try:
        cur = c.execute("UPDATE bookings SET status='confirmed', paid=? WHERE id=? AND status='pending'",
                        (datetime.now().strftime("%Y-%m-%d %H:%M"), bid))
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def new_booking(key, day, name, phone, reason):
    """Insert a booking and give it the next number for that day (booking order)."""
    with lock:
        c = db()
        try:
            t = c.execute("SELECT COALESCE(MAX(token),0)+1 FROM bookings WHERE day=?", (day,)).fetchone()[0]
            cur = c.execute("INSERT INTO bookings(key,day,name,phone,reason,token) VALUES(?,?,?,?,?,?)",
                            (key, day, name, phone, reason, t))
            c.commit()
            return cur.lastrowid, t
        finally:
            c.close()


def receipt_no(day, token):
    return "R%s-%03d" % (day.replace("-", ""), token or 0)


RECEIPT = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Receipt __NO__</title>
<style>body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#f4f7f6;color:#12343b}
.r{max-width:420px;margin:20px auto;background:#fff;border:1px solid #d5e0df;border-radius:12px;padding:24px}
h1{font-size:22px;margin:0}.c{color:#5d7479;margin:0 0 16px}.n{font-size:48px;font-weight:700;text-align:center;margin:12px 0}
table{width:100%;border-collapse:collapse}td{padding:6px 0;border-bottom:1px solid #e1ebea;vertical-align:top}td:first-child{color:#5d7479;width:40%}
.p{display:inline-block;margin-top:14px;padding:4px 12px;border-radius:99px;background:#e1f1f0;color:#0e7c86;font-weight:700}
button{display:block;width:calc(100% - 40px);max-width:380px;margin:0 auto 20px;padding:12px;border:0;border-radius:10px;background:#0e7c86;color:#fff;font:600 16px system-ui;cursor:pointer}
@media print{button{display:none}body{background:#fff}.r{border:0}}</style></head><body>
<div class="r"><h1>__CLINIC__</h1><p class="c">Payment receipt</p>
<div class="n">#__TOKEN__</div><p class="c" style="text-align:center">Queue number for __DAY__</p>
<table><tr><td>Receipt no.</td><td>__NO__</td></tr><tr><td>Patient</td><td>__NAME__</td></tr>
<tr><td>Phone</td><td>__PHONE__</td></tr><tr><td>Visit day</td><td>__DAY__</td></tr>
<tr><td>Paid on</td><td>__PAID__</td></tr><tr><td>For</td><td>__FEE__</td></tr></table>
<span class="p">PAID</span></div><button onclick="print()">Print or save as PDF</button></body></html>"""


PATIENT = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__CLINIC__</title>
<style>
body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#f4f7f6;color:#12343b}
main{max-width:480px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:26px;margin:0 0 4px}p.s{color:#5d7479;margin:0 0 16px}
label{display:block;font-weight:600;margin:12px 0 4px}
input,select,textarea{width:100%;box-sizing:border-box;padding:12px;border:1px solid #c9d8d6;border-radius:10px;font:inherit}
button,a.btn{display:block;width:100%;box-sizing:border-box;text-align:center;margin-top:16px;padding:14px;border:0;border-radius:12px;background:#0e7c86;color:#fff;font:600 17px system-ui;text-decoration:none;cursor:pointer}
button:disabled{opacity:.5}.box{background:#fff;border:1px solid #d5e0df;border-radius:12px;padding:16px;margin-top:16px}
.tok{font-size:56px;font-weight:700;text-align:center;line-height:1.1}.err{color:#b3261e;min-height:1.3em}
</style></head><body><main>
<h1>__CLINIC__</h1><p class="s">Join the queue for a day. You get your queue number right away. Payment confirms it.</p>
<div id="f"><label for="day">Day</label><select id="day"></select>
<label for="n">Full name</label><input id="n" autocomplete="name">
<label for="p">Phone number</label><input id="p" type="tel" autocomplete="tel">
<label for="r">Reason for visit</label><textarea id="r" rows="2"></textarea>
<p class="err" id="e" role="alert"></p><button id="go">Continue to payment</button></div>
<div id="pay" class="box" hidden><b>Your queue number</b><div class="tok" id="pt"></div><p>__FEE__. Pay now to confirm this booking. Unpaid bookings are not confirmed.</p>
<a class="btn" id="pl" target="_blank" rel="noopener">Pay now</a><p id="w" style="color:#5d7479">Waiting for payment&hellip;</p></div>
<div id="ok" class="box" hidden><p style="margin:0;text-align:center">Booking confirmed for <b id="od"></b></p>
<div class="tok" id="tk"></div><p style="text-align:center;margin:0">Your queue number. Show this number at the clinic.</p><a class="btn" id="rc" target="_blank" rel="noopener">View receipt</a></div>
</main><script>
var $=function(i){return document.getElementById(i)},D=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
for(var i=0;i<7;i++){var d=new Date();d.setDate(d.getDate()+i);var v=d.getFullYear()+"-"+("0"+(d.getMonth()+1)).slice(-2)+"-"+("0"+d.getDate()).slice(-2);
var o=document.createElement("option");o.value=v;o.textContent=(i?D[d.getDay()]+" ":"Today ")+v;$("day").appendChild(o)}
var timer;function poll(id,key){clearInterval(timer);timer=setInterval(function(){fetch("/api/status?id="+id+"&key="+key).then(function(r){return r.json()}).then(function(s){
if(s.status==="confirmed"||s.status==="done"){clearInterval(timer);$("pay").hidden=true;$("f").hidden=true;$("ok").hidden=false;$("od").textContent=s.day;$("tk").textContent="#"+s.token;$("rc").href="/receipt?id="+id+"&key="+key}
else if(s.status==="cancelled"){clearInterval(timer);$("w").textContent="This booking was cancelled. Please book again.";localStorage.removeItem("b")}}).catch(function(){})},4000)}
function show(b){$("f").hidden=true;$("pay").hidden=false;$("pt").textContent="#"+b.token;if(b.pay_url){$("pl").href=b.pay_url}else{$("pl").hidden=true;$("w").textContent="Payment link not set up yet. Please pay at the clinic desk."}poll(b.id,b.key)}
$("go").onclick=function(){$("e").textContent="";$("go").disabled=true;
fetch("/api/book",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({day:$("day").value,name:$("n").value,phone:$("p").value,reason:$("r").value})})
.then(function(r){return r.json().then(function(j){return{ok:r.ok,j:j}})}).then(function(x){$("go").disabled=false;
if(!x.ok){$("e").textContent=x.j.error||"Could not book. Try again.";return}
try{localStorage.setItem("b",JSON.stringify(x.j))}catch(e){}show(x.j)}).catch(function(){$("go").disabled=false;$("e").textContent="No connection. Try again."})};
try{var sv=JSON.parse(localStorage.getItem("b")||"null");if(sv)show(sv)}catch(e){}
</script></body></html>"""

ADMIN = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin</title>
<style>body{margin:0;font:15px/1.4 system-ui,sans-serif;background:#f4f7f6;color:#12343b}main{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:22px}input{padding:8px;border:1px solid #c9d8d6;border-radius:8px}.sum{margin:10px 0;color:#5d7479}
.w{overflow-x:auto}table{width:100%;border-collapse:collapse;background:#fff}th,td{text-align:left;padding:10px;border-bottom:1px solid #e1ebea;vertical-align:top}
.b{border:1px solid #c9d8d6;background:#fff;border-radius:8px;padding:6px 10px;margin:2px;cursor:pointer;font:inherit}.s-pending{color:#a15c00}.s-confirmed{color:#0e7c86}.s-done{color:#5d7479}.s-cancelled{color:#b3261e}
</style></head><body><main><h1>__CLINIC__: appointments</h1>
<label>Day <input type="date" id="d"></label><div class="sum" id="sum"></div>
<div class="w"><table><thead><tr><th>Queue</th><th>Patient</th><th>Phone</th><th>Reason</th><th>Status</th><th></th></tr></thead><tbody id="t"></tbody></table></div></main>
<script>var $=function(i){return document.getElementById(i)};$("d").value=new Date().toISOString().slice(0,10);
function td(tr,t){var c=document.createElement("td");c.textContent=t;tr.appendChild(c);return c}
function act(id,a){fetch("/admin/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,action:a})}).then(load)}
function load(){fetch("/admin/api/list?day="+$("d").value).then(function(r){return r.json()}).then(function(L){var t=$("t");t.innerHTML="";
var c={};L.forEach(function(b){c[b.status]=(c[b.status]||0)+1;var tr=document.createElement("tr");td(tr,b.token?"#"+b.token:"-");td(tr,b.name);td(tr,b.phone);td(tr,b.reason);
var s=td(tr,b.status);s.className="s-"+b.status;var a=td(tr,"");
[["pending","paid","Mark paid"],["confirmed","done","Seen"],["pending","cancel","Cancel"],["confirmed","cancel","Cancel"]].forEach(function(x){if(b.status===x[0]){var e=document.createElement("button");e.className="b";e.textContent=x[2];e.onclick=function(){act(b.id,x[1])};a.appendChild(e)}});t.appendChild(tr)});
$("sum").textContent=L.length?(c.confirmed||0)+" waiting, "+(c.done||0)+" seen, "+(c.pending||0)+" unpaid":"No bookings for this day."})}
$("d").onchange=load;load();setInterval(load,10000);</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def js(self, code, obj):
        self.send(code, json.dumps(obj))

    def body(self):
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 20000)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def admin_ok(self):
        try:
            u, p = base64.b64decode(self.headers.get("Authorization", "")[6:]).decode().split(":", 1)
            ok = hmac.compare_digest(u, AUSER) & hmac.compare_digest(p, APASS)
        except Exception:
            ok = False
        if not ok:
            self.send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="Admin"'})
        return ok

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self.send(200, PATIENT.replace("__CLINIC__", CLINIC).replace("__FEE__", FEE), "text/html")
        elif u.path == "/api/status":
            try:
                r = db().execute("SELECT status,token,day FROM bookings WHERE id=? AND key=?",
                                 (int(q["id"][0]), q["key"][0])).fetchone()
            except Exception:
                r = None
            self.js(200, dict(r)) if r else self.js(404, {"error": "not found"})
        elif u.path == "/receipt":
            try:
                r = db().execute("SELECT * FROM bookings WHERE id=? AND key=?", (int(q["id"][0]), q["key"][0])).fetchone()
            except Exception:
                r = None
            if not r or r["status"] not in ("confirmed", "done"):
                return self.send(404, "Receipt not available. Payment has not been confirmed.", "text/plain")
            vals = {"CLINIC": CLINIC, "NO": receipt_no(r["day"], r["token"]), "TOKEN": r["token"], "DAY": r["day"],
                    "NAME": r["name"], "PHONE": r["phone"], "PAID": r["paid"] or "-", "FEE": FEE}
            page = RECEIPT
            for k, v in vals.items():
                page = page.replace("__%s__" % k, escape(str(v)))
            self.send(200, page, "text/html")
        elif u.path == "/admin":
            if self.admin_ok():
                self.send(200, ADMIN.replace("__CLINIC__", CLINIC), "text/html")
        elif u.path == "/admin/api/list":
            if self.admin_ok():
                day = (q.get("day") or [date.today().isoformat()])[0]
                rows = db().execute("SELECT id,name,phone,reason,status,token FROM bookings WHERE day=? "
                                    "ORDER BY token IS NULL, token, id", (day,)).fetchall()
                self.js(200, [dict(r) for r in rows])
        else:
            self.send(404, "Not found", "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        d = self.body()
        if p == "/api/book":
            name, phone = str(d.get("name", "")).strip()[:80], str(d.get("phone", "")).strip()[:20]
            reason, day = str(d.get("reason", "")).strip()[:300], str(d.get("day", ""))
            try:
                dd = date.fromisoformat(day)
            except ValueError:
                return self.js(400, {"error": "Choose a valid day."})
            if not (date.today() <= dd <= date.today() + timedelta(days=6)):
                return self.js(400, {"error": "Choose a day in the next 7 days."})
            if not name:
                return self.js(400, {"error": "Enter your full name."})
            if sum(ch.isdigit() for ch in phone) < 7:
                return self.js(400, {"error": "Enter a valid phone number."})
            key = secrets.token_urlsafe(12)
            bid, tok = new_booking(key, day, name, phone, reason)
            url = PAY
            if PAY and PAY_REF:
                url += ("&" if "?" in PAY else "?") + urlencode({PAY_REF: bid})
            self.js(200, {"id": bid, "key": key, "day": day, "token": tok, "pay_url": url})
        elif p == "/api/webhook":
            sec = self.headers.get("X-Webhook-Secret", "")
            if not WSECRET or not hmac.compare_digest(sec, WSECRET):
                return self.js(403, {"error": "forbidden"})
            try:
                self.js(200, {"confirmed": confirm(int(d.get("booking_id")))})
            except (TypeError, ValueError):
                self.js(400, {"error": "booking_id required"})
        elif p == "/admin/api/action":
            if not self.admin_ok():
                return
            try:
                bid, a = int(d.get("id")), d.get("action")
            except (TypeError, ValueError):
                return self.js(400, {"error": "bad request"})
            if a == "paid":
                confirm(bid)
            elif a in ("done", "cancel"):
                c = db()
                c.execute("UPDATE bookings SET status=? WHERE id=? AND status IN ('pending','confirmed')",
                          ("done" if a == "done" else "cancelled", bid))
                c.commit(); c.close()
            self.js(200, {"ok": True})
        else:
            self.send(404, "Not found", "text/plain")


if __name__ == "__main__":
    init()
    port = int(os.environ.get("PORT", 8000))
    if APASS == "change-me":
        print("WARNING: set ADMIN_PASSWORD before going live.")
    print(f"Running on http://localhost:{port}  (admin: /admin)")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
