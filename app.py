import os, sqlite3, json, uuid, time, io, csv
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, g, render_template, request, redirect, url_for, session, jsonify, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
SECRET = os.environ.get("SECRET_KEY", "change-me-in-production")
app.secret_key = SECRET
if os.environ.get("APP_ENV") == "production" and (SECRET == "change-me-in-production" or not os.environ.get("ADMIN_PASS")):
    raise RuntimeError("Set SECRET_KEY and ADMIN_PASS before running in production")
UPLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOADS, exist_ok=True)
app.config.update(MAX_CONTENT_LENGTH=5 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db"))
STATUSES = ["New", "Confirmed", "Packed", "Shipped", "Delivered", "Cancelled"]
WORKER_ALLOWED = {"Packed", "Shipped"}
ROLES = ["admin", "manager", "worker"]
IST = timezone(timedelta(hours=5, minutes=30))
def now(): return datetime.now(IST)

def save_photo(f):
    if not f or not f.filename: return None
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"): return None
    name = uuid.uuid4().hex + "." + ext
    f.save(os.path.join(UPLOADS, name)); return "/static/uploads/" + name

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
      price INTEGER NOT NULL, mrp INTEGER DEFAULT 0, stock INTEGER DEFAULT 0, image_url TEXT DEFAULT '', active INTEGER DEFAULT 1,
      cost_price INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS expenses(
      id INTEGER PRIMARY KEY, expense_date TEXT NOT NULL, category TEXT NOT NULL,
      amount INTEGER NOT NULL CHECK(amount >= 0), note TEXT DEFAULT '', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ingredients(id INTEGER PRIMARY KEY, name TEXT NOT NULL, note TEXT DEFAULT '', emoji TEXT DEFAULT '', photo TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, address TEXT, pincode TEXT,
      items TEXT, total INTEGER, status TEXT DEFAULT 'New', created_at TEXT);
    CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, name TEXT, username TEXT UNIQUE, password_hash TEXT, role TEXT, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, phone TEXT UNIQUE, email TEXT DEFAULT '', password_hash TEXT, address TEXT DEFAULT '', pincode TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS stock(product_id INTEGER, location_id INTEGER, qty INTEGER DEFAULT 0, rack TEXT DEFAULT '', PRIMARY KEY(product_id, location_id));
    """)
    # Safe migrations: add new fields/tables without deleting existing data.
    for col in ("user_id INTEGER", "assigned_to INTEGER"):
        try: c.execute("ALTER TABLE orders ADD COLUMN " + col)
        except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE products ADD COLUMN cost_price INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError: pass
    if not c.execute("SELECT 1 FROM products").fetchone():
        c.executemany("INSERT INTO products(name,description,price,mrp,stock) VALUES(?,?,?,?,?)", [
            ("Classic Makhana 100g", "A light, crunchy pantry snack. Update this description to match your product.", 199, 199, 100),
            ("Sattu Flour 1kg", "A traditional Indian pantry staple. Update this description to match your product.", 149, 149, 100),
            ("Wood-Pressed Mustard Oil 1L", "Traditional wood-pressed oil for everyday cooking. Update this description to match your product.", 349, 349, 100)])
    if not c.execute("SELECT 1 FROM ingredients").fetchone():
        c.executemany("INSERT INTO ingredients(name,note,emoji) VALUES(?,?,?)", [
            ("Makhana", "A crunchy snack option", "🌰"), ("Sattu", "A traditional Indian staple", "🌾"), ("Wood-Pressed Oil", "For everyday cooking", "🫙")])
    if not c.execute("SELECT 1 FROM locations").fetchone():
        c.execute("INSERT INTO locations(name,address) VALUES('Main Warehouse','')")
    if not c.execute("SELECT 1 FROM stock").fetchone():
        loc = c.execute("SELECT MIN(id) FROM locations").fetchone()[0]
        c.execute("INSERT INTO stock(product_id,location_id,qty) SELECT id,?,stock FROM products", (loc,))
    if not c.execute("SELECT 1 FROM staff").fetchone():
        c.execute("INSERT INTO staff(name,username,password_hash,role) VALUES(?,?,?,'admin')",
                  ("Owner", os.environ.get("ADMIN_USER", "admin"), generate_password_hash(os.environ.get("ADMIN_PASS", "admin123"))))
    c.commit(); c.close()
init()

# ---------- security helpers ----------
FAILS = {}
def throttled():
    ip = request.remote_addr; t = time.time()
    FAILS[ip] = [x for x in FAILS.get(ip, []) if t - x < 120]
    return len(FAILS[ip]) >= 5
def fail(): FAILS.setdefault(request.remote_addr, []).append(time.time())
def safe_next(n, default): return n if n and n.startswith("/") and not n.startswith("//") else default

@app.before_request
def same_origin():
    if request.method == "POST":
        o = request.headers.get("Origin") or request.headers.get("Referer")
        if o and urlparse(o).netloc != request.host: abort(403)

def current_staff():
    if "_staff" not in g:
        g._staff = db().execute("SELECT * FROM staff WHERE id=? AND active=1", (session.get("sid"),)).fetchone() if session.get("sid") else None
    return g._staff

def current_user():
    return db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone() if session.get("uid") else None

@app.context_processor
def ctx(): return dict(me=current_user(), staff=current_staff())

def staff_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            s = current_staff()
            if not s: return redirect(url_for("staff_login", next=request.path))
            if roles and s["role"] not in roles: abort(403)
            return f(*a, **k)
        return w
    return deco

# ---------- stock helpers ----------
def sync(pid):
    db().execute("UPDATE products SET stock=(SELECT COALESCE(SUM(qty),0) FROM stock WHERE product_id=?) WHERE id=?", (pid, pid))
def deduct(pid, n):
    for r in db().execute("SELECT location_id,qty FROM stock WHERE product_id=? AND qty>0 ORDER BY qty DESC", (pid,)).fetchall():
        t = min(n, r["qty"]); n -= t
        db().execute("UPDATE stock SET qty=qty-? WHERE product_id=? AND location_id=?", (t, pid, r["location_id"]))
        if n == 0: break
    sync(pid)
def restore(pid, n):
    loc = db().execute("SELECT MIN(id) m FROM locations").fetchone()["m"]
    db().execute("INSERT INTO stock(product_id,location_id,qty) VALUES(?,?,?) ON CONFLICT(product_id,location_id) DO UPDATE SET qty=qty+?", (pid, loc, n, n))
    sync(pid)

# ---------- storefront ----------
@app.route("/")
def store():
    ps = db().execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    ing = db().execute("SELECT * FROM ingredients ORDER BY id").fetchall()
    return render_template("store.html", products=ps, ingredients=ing)

@app.post("/api/order")
def api_order():
    u = current_user()
    if not u: return jsonify(error="Please log in to place an order"), 401
    d = request.get_json(force=True, silent=True) or {}
    name, phone, addr, pin = [str(d.get(k, "")).strip() for k in ("name", "phone", "address", "pincode")]
    if not (name and phone.isdigit() and len(phone) == 10 and addr and pin.isdigit() and len(pin) == 6):
        return jsonify(error="Enter valid name, 10-digit phone, address and 6-digit pincode"), 400
    items, total, con = [], 0, db()
    for it in d.get("items", []):
        p = con.execute("SELECT * FROM products WHERE id=? AND active=1", (it.get("id"),)).fetchone()
        try: n = int(it.get("qty", 0))
        except (TypeError, ValueError): n = 0
        if not p or n < 1: continue
        if p["stock"] < n: return jsonify(error=f"Only {p['stock']} left of {p['name']}"), 400
        items.append({"id": p["id"], "name": p["name"], "price": p["price"], "qty": n}); total += p["price"] * n
        deduct(p["id"], n)
    if not items: return jsonify(error="Cart is empty"), 400
    cur = con.execute("INSERT INTO orders(name,phone,address,pincode,items,total,created_at,user_id) VALUES(?,?,?,?,?,?,?,?)",
        (name, phone, addr, pin, json.dumps(items), total, now().isoformat(timespec="seconds"), u["id"]))
    con.execute("UPDATE users SET address=?,pincode=? WHERE id=? AND address=''", (addr, pin, u["id"]))
    con.commit()
    return jsonify(ok=True, order_id=cur.lastrowid, total=total)

# ---------- customer accounts ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form; name, phone, pw = f["name"].strip(), f["phone"].strip(), f["password"]
        if not name or not (phone.isdigit() and len(phone) == 10) or len(pw) < 6:
            flash("Enter name, 10-digit phone and a password of 6+ characters")
        elif db().execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
            flash("This phone is already registered. Please log in.")
        else:
            cur = db().execute("INSERT INTO users(name,phone,email,password_hash) VALUES(?,?,?,?)", (name, phone, f.get("email", "").strip(), generate_password_hash(pw)))
            db().commit(); session.pop("uid", None); session["uid"] = cur.lastrowid
            return redirect(safe_next(request.args.get("next"), url_for("store")))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if throttled(): flash("Too many attempts. Try again in 2 minutes.")
        else:
            u = db().execute("SELECT * FROM users WHERE phone=?", (request.form["phone"].strip(),)).fetchone()
            if u and check_password_hash(u["password_hash"], request.form["password"]):
                session["uid"] = u["id"]; return redirect(safe_next(request.args.get("next"), url_for("store")))
            fail(); flash("Wrong phone or password")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("uid", None); return redirect(url_for("store"))

@app.route("/account", methods=["GET", "POST"])
def account():
    u = current_user()
    if not u: return redirect(url_for("login", next="/account"))
    if request.method == "POST":
        f = request.form
        db().execute("UPDATE users SET name=?,email=?,address=?,pincode=? WHERE id=?", (f["name"].strip(), f.get("email", "").strip(), f.get("address", "").strip(), f.get("pincode", "").strip(), u["id"]))
        db().commit(); flash("Profile saved"); return redirect(url_for("account"))
    rows = db().execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    return render_template("account.html", orders=[dict(r, items=json.loads(r["items"])) for r in rows])

# ---------- staff auth ----------
@app.route("/admin/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        if throttled(): flash("Too many attempts. Try again in 2 minutes.")
        else:
            s = db().execute("SELECT * FROM staff WHERE username=? AND active=1", (request.form["username"].strip(),)).fetchone()
            if s and check_password_hash(s["password_hash"], request.form["password"]):
                session["sid"] = s["id"]
                return redirect(safe_next(request.args.get("next"), url_for("dashboard")) if request.args.get("next", "").startswith("/admin") else url_for("dashboard"))
            fail(); flash("Wrong username or password")
    return render_template("staff_login.html")

@app.route("/admin/logout")
def staff_logout():
    session.pop("sid", None); return redirect(url_for("staff_login"))

# ---------- admin: dashboard ----------
def count_since(days):
    n = now()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0) if days == 0 else n - timedelta(days=days)
    r = db().execute("SELECT COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE created_at>=? AND status!='Cancelled'", (start.isoformat(timespec="seconds"),)).fetchone()
    return {"orders": r["c"], "revenue": r["s"]}

@app.route("/admin")
@staff_required()
def dashboard():
    if current_staff()["role"] == "worker": return redirect(url_for("orders"))
    stats = [("Today", count_since(0)), ("Last 2 days", count_since(2)), ("Last 7 days", count_since(7)), ("Last 30 days", count_since(30))]
    r = db().execute("SELECT COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE status!='Cancelled'").fetchone()
    pending = db().execute("SELECT COUNT(*) FROM orders WHERE status='New'").fetchone()[0]
    low = db().execute("SELECT * FROM products WHERE stock<=10 AND active=1").fetchall()
    recent = db().execute("SELECT * FROM orders ORDER BY id DESC LIMIT 5").fetchall()
    today = now().date()
    start = (today - timedelta(days=6)).isoformat()
    agg = {r["d"]: r for r in db().execute("SELECT substr(created_at,1,10) d, COUNT(*) c, COALESCE(SUM(total),0) s FROM orders WHERE created_at>=? AND status!='Cancelled' GROUP BY d", (start,))}
    days = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i); a = agg.get(d.isoformat())
        days.append({"label": d.strftime("%a %d"), "orders": a["c"] if a else 0, "revenue": a["s"] if a else 0})
    mx = max([x["revenue"] for x in days] + [1])
    for x in days: x["pct"] = int(x["revenue"] * 100 / mx)
    bystatus = {r[0]: r[1] for r in db().execute("SELECT status, COUNT(*) FROM orders GROUP BY status")}
    tp = {}
    for row in db().execute("SELECT items FROM orders WHERE created_at>=? AND status!='Cancelled'", ((now() - timedelta(days=30)).isoformat(timespec="seconds"),)):
        for it in json.loads(row["items"]):
            t = tp.setdefault(it["id"], {"name": it["name"], "qty": 0, "revenue": 0}); t["qty"] += it["qty"]; t["revenue"] += it["qty"] * it["price"]
    top = sorted(tp.values(), key=lambda x: -x["revenue"])[:5]
    return render_template("dashboard.html", stats=stats, total=r, pending=pending, low=low, recent=recent, days=days, bystatus=bystatus, top=top, statuses=STATUSES)

# ---------- admin: catalogue ----------
@app.route("/admin/products", methods=["GET", "POST"])
@staff_required("admin", "manager")
def products():
    con = db()
    if request.method == "POST":
        f = request.form
        img = save_photo(request.files.get("photo")) or f.get("image_url", "").strip()
        vals = (f["name"].strip(), f.get("description", "").strip(), int(f["price"]), int(f.get("mrp") or 0), img, 1 if f.get("active") else 0, max(0, int(f.get("cost_price") or 0)))
        if f.get("id"):
            con.execute("UPDATE products SET name=?,description=?,price=?,mrp=?,image_url=?,active=?,cost_price=? WHERE id=?", vals + (int(f["id"]),))
        else:
            cur = con.execute("INSERT INTO products(name,description,price,mrp,image_url,active,cost_price) VALUES(?,?,?,?,?,?,?)", vals)
            qty = int(f.get("stock") or 0)
            if qty: restore(cur.lastrowid, qty)
        con.commit(); return redirect(url_for("products"))
    edit = con.execute("SELECT * FROM products WHERE id=?", (request.args.get("edit"),)).fetchone() if request.args.get("edit") else None
    return render_template("products.html", products=con.execute("SELECT * FROM products ORDER BY id DESC").fetchall(), edit=edit)

@app.post("/admin/products/<int:pid>/delete")
@staff_required("admin", "manager")
def delete_product(pid):
    db().execute("DELETE FROM products WHERE id=?", (pid,)); db().execute("DELETE FROM stock WHERE product_id=?", (pid,)); db().commit()
    return redirect(url_for("products"))

@app.route("/admin/ingredients", methods=["GET", "POST"])
@staff_required("admin", "manager")
def ingredients():
    con = db()
    if request.method == "POST":
        f = request.form; photo = save_photo(request.files.get("photo"))
        if f.get("id"):
            old = con.execute("SELECT photo FROM ingredients WHERE id=?", (f["id"],)).fetchone()
            con.execute("UPDATE ingredients SET name=?,note=?,emoji=?,photo=? WHERE id=?", (f["name"].strip(), f.get("note", "").strip(), f.get("emoji", "").strip(), photo or (old["photo"] if old else ""), int(f["id"])))
        else:
            con.execute("INSERT INTO ingredients(name,note,emoji,photo) VALUES(?,?,?,?)", (f["name"].strip(), f.get("note", "").strip(), f.get("emoji", "").strip(), photo or ""))
        con.commit(); return redirect(url_for("ingredients"))
    edit = con.execute("SELECT * FROM ingredients WHERE id=?", (request.args.get("edit"),)).fetchone() if request.args.get("edit") else None
    return render_template("ingredients.html", items=con.execute("SELECT * FROM ingredients ORDER BY id").fetchall(), edit=edit)

@app.post("/admin/ingredients/<int:iid>/delete")
@staff_required("admin", "manager")
def delete_ingredient(iid):
    db().execute("DELETE FROM ingredients WHERE id=?", (iid,)); db().commit(); return redirect(url_for("ingredients"))

# ---------- admin: inventory by location ----------
@app.route("/admin/inventory", methods=["GET", "POST"])
@staff_required()
def inventory():
    con, s = db(), current_staff()
    if request.method == "POST":
        if s["role"] not in ("admin", "manager"): abort(403)
        f = request.form
        if f["action"] == "loc" and f["name"].strip():
            con.execute("INSERT INTO locations(name,address) VALUES(?,?)", (f["name"].strip(), f.get("address", "").strip()))
        elif f["action"] == "set":
            pid, lid, qty = int(f["product_id"]), int(f["location_id"]), max(0, int(f["qty"]))
            con.execute("INSERT INTO stock(product_id,location_id,qty,rack) VALUES(?,?,?,?) ON CONFLICT(product_id,location_id) DO UPDATE SET qty=?, rack=?", (pid, lid, qty, f.get("rack", "").strip(), qty, f.get("rack", "").strip()))
            sync(pid)
        con.commit(); return redirect(url_for("inventory"))
    prods = con.execute("SELECT * FROM products ORDER BY name").fetchall()
    locs = con.execute("SELECT * FROM locations ORDER BY id").fetchall()
    rows = con.execute("SELECT s.product_id pid, l.name loc, s.qty, s.rack FROM stock s JOIN locations l ON l.id=s.location_id ORDER BY l.name").fetchall()
    by = {}
    for r in rows: by.setdefault(r["pid"], []).append(r)
    return render_template("inventory.html", prods=prods, locs=locs, by=by)

# ---------- admin: orders & workers ----------
@app.route("/admin/orders")
@staff_required()
def orders():
    s = current_staff(); st = request.args.get("status")
    where, args = [], []
    if st in STATUSES: where.append("o.status=?"); args.append(st)
    if s["role"] == "worker": where.append("(o.assigned_to=? OR o.assigned_to IS NULL)"); args.append(s["id"])
    elif request.args.get("worker"): where.append("o.assigned_to=?"); args.append(request.args["worker"])
    kw = request.args.get("q", "").strip()
    if kw: where.append("(CAST(o.id AS TEXT)=? OR o.name LIKE ? OR o.phone LIKE ?)"); args += [kw, f"%{kw}%", f"%{kw}%"]
    q = "SELECT o.*, w.name worker FROM orders o LEFT JOIN staff w ON w.id=o.assigned_to" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY o.id DESC"
    rows = db().execute(q, args).fetchall()
    workers = db().execute("SELECT id,name,role FROM staff WHERE active=1 ORDER BY name").fetchall()
    return render_template("orders.html", orders=[dict(r, items=json.loads(r["items"])) for r in rows], statuses=STATUSES, cur=st, workers=workers)

@app.post("/admin/orders/<int:oid>/status")
@staff_required()
def set_status(oid):
    s, new = current_staff(), request.form["status"]
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if new not in STATUSES or not o or o["status"] == "Cancelled": abort(400)
    if s["role"] == "worker" and (new not in WORKER_ALLOWED or o["assigned_to"] != s["id"]): abort(403)
    if new == "Cancelled":
        for it in json.loads(o["items"]): restore(it["id"], it["qty"])
    db().execute("UPDATE orders SET status=? WHERE id=?", (new, oid)); db().commit()
    return redirect(request.referrer or url_for("orders"))

@app.post("/admin/orders/<int:oid>/assign")
@staff_required("admin", "manager")
def assign(oid):
    w = request.form.get("worker") or None
    db().execute("UPDATE orders SET assigned_to=? WHERE id=?", (w, oid)); db().commit()
    return redirect(request.referrer or url_for("orders"))

@app.post("/admin/orders/<int:oid>/take")
@staff_required("worker")
def take(oid):
    db().execute("UPDATE orders SET assigned_to=? WHERE id=? AND assigned_to IS NULL", (current_staff()["id"], oid)); db().commit()
    return redirect(request.referrer or url_for("orders"))

# ---------- admin: staff management ----------
@app.route("/admin/staff", methods=["GET", "POST"])
@staff_required("admin")
def staff_admin():
    con = db()
    if request.method == "POST":
        f = request.form
        if f["role"] not in ROLES or len(f["password"]) < 6 or not f["username"].strip():
            flash("Username, role and a 6+ character password are required")
        elif con.execute("SELECT 1 FROM staff WHERE username=?", (f["username"].strip(),)).fetchone():
            flash("Username already exists")
        else:
            con.execute("INSERT INTO staff(name,username,password_hash,role) VALUES(?,?,?,?)", (f["name"].strip() or f["username"].strip(), f["username"].strip(), generate_password_hash(f["password"]), f["role"])); con.commit()
        return redirect(url_for("staff_admin"))
    load = {r[0]: r[1] for r in con.execute("SELECT assigned_to, COUNT(*) FROM orders WHERE status IN ('New','Confirmed','Packed') AND assigned_to IS NOT NULL GROUP BY assigned_to")}
    return render_template("staff.html", members=con.execute("SELECT * FROM staff ORDER BY id").fetchall(), load=load, roles=ROLES)

@app.post("/admin/staff/<int:sid>/toggle")
@staff_required("admin")
def staff_toggle(sid):
    if sid != current_staff()["id"]: db().execute("UPDATE staff SET active=1-active WHERE id=?", (sid,)); db().commit()
    return redirect(url_for("staff_admin"))

@app.post("/admin/staff/<int:sid>/password")
@staff_required("admin")
def staff_password(sid):
    if len(request.form["password"]) >= 6:
        db().execute("UPDATE staff SET password_hash=? WHERE id=?", (generate_password_hash(request.form["password"]), sid)); db().commit()
    return redirect(url_for("staff_admin"))


# ---------- admin: business analytics, expenses and exports ----------
def _report_dates():
    today = now().date()
    start_raw = request.args.get("start", "").strip()
    end_raw = request.args.get("end", "").strip()
    try:
        start = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else today.replace(day=1)
    except ValueError:
        start = today.replace(day=1)
    try:
        end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else today
    except ValueError:
        end = today
    if start > end:
        start, end = end, start
    return start, end

def _business_report(start, end):
    con = db()
    start_dt = datetime.combine(start, datetime.min.time(), IST).isoformat(timespec="seconds")
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), IST).isoformat(timespec="seconds")
    orders = con.execute("""SELECT * FROM orders WHERE created_at>=? AND created_at<? ORDER BY created_at""",
                         (start_dt, end_dt)).fetchall()
    active_orders = [dict(o) for o in orders if o["status"] != "Cancelled"]
    delivered = [o for o in active_orders if o["status"] == "Delivered"]
    gross_sales = sum(int(o["total"] or 0) for o in active_orders)
    delivered_sales = sum(int(o["total"] or 0) for o in delivered)
    items_by_product = {}
    cogs = 0
    units = 0
    for order in delivered:
        try: items = json.loads(order.get("items") or "[]")
        except (ValueError, TypeError): items = []
        for item in items:
            qty = max(0, int(item.get("qty", 0) or 0))
            pid = item.get("id")
            product = con.execute("SELECT name,cost_price FROM products WHERE id=?", (pid,)).fetchone() if pid else None
            cost = int(product["cost_price"] or 0) if product else 0
            name = item.get("name") or (product["name"] if product else "Unknown product")
            line_revenue = qty * int(item.get("price", 0) or 0)
            line_cost = qty * cost
            units += qty
            cogs += line_cost
            p = items_by_product.setdefault(name, {"name": name, "qty": 0, "revenue": 0, "cost": 0})
            p["qty"] += qty; p["revenue"] += line_revenue; p["cost"] += line_cost
    expenses = con.execute("SELECT * FROM expenses WHERE expense_date>=? AND expense_date<=? ORDER BY expense_date DESC,id DESC",
                            (start.isoformat(), end.isoformat())).fetchall()
    expense_total = sum(int(e["amount"] or 0) for e in expenses)
    expense_by_category = {}
    for e in expenses:
        expense_by_category[e["category"]] = expense_by_category.get(e["category"], 0) + int(e["amount"] or 0)
    delivered_profit = delivered_sales - cogs
    return {
        "start": start, "end": end, "orders": orders, "active_orders": active_orders,
        "delivered": delivered, "order_count": len(active_orders),
        "delivered_count": len(delivered), "pending_count": sum(o["status"] not in ("Delivered","Cancelled") for o in active_orders),
        "cancelled_count": sum(o["status"] == "Cancelled" for o in orders),
        "gross_sales": gross_sales, "delivered_sales": delivered_sales,
        "average_order": round(gross_sales / len(active_orders)) if active_orders else 0,
        "units": units, "cogs": cogs, "expense_total": expense_total,
        "gross_profit": delivered_profit, "net_profit": delivered_profit - expense_total,
        "expenses": expenses, "expense_by_category": expense_by_category,
        "top_products": sorted(items_by_product.values(), key=lambda x: x["revenue"], reverse=True)[:10],
        "cost_missing": any((not con.execute("SELECT cost_price FROM products WHERE id=?", (it.get("id"),)).fetchone() or {"cost_price":0})["cost_price"] == 0
                           for o in delivered for it in (json.loads(o["items"] or "[]") if o["items"] else []) if it.get("id"))
    }

@app.route("/admin/analytics")
@staff_required("admin", "manager")
def analytics():
    start, end = _report_dates()
    report = _business_report(start, end)
    return render_template("analytics.html", r=report)

@app.route("/admin/expenses", methods=["GET", "POST"])
@staff_required("admin", "manager")
def expenses():
    con = db()
    if request.method == "POST":
        f = request.form
        category = f.get("category", "").strip()
        try:
            amount = int(f.get("amount", "0"))
            expense_date = datetime.strptime(f.get("expense_date", ""), "%Y-%m-%d").date().isoformat()
        except (ValueError, TypeError):
            flash("Enter a valid date and whole-rupee amount.")
            return redirect(url_for("expenses"))
        if not category or amount <= 0:
            flash("Category and an amount greater than zero are required.")
            return redirect(url_for("expenses"))
        con.execute("INSERT INTO expenses(expense_date,category,amount,note,created_at) VALUES(?,?,?,?,?)",
                    (expense_date, category[:80], amount, f.get("note", "").strip()[:500], now().isoformat(timespec="seconds")))
        con.commit(); flash("Expense saved.")
        return redirect(url_for("expenses"))
    start, end = _report_dates()
    rows = con.execute("SELECT * FROM expenses WHERE expense_date>=? AND expense_date<=? ORDER BY expense_date DESC,id DESC",
                       (start.isoformat(), end.isoformat())).fetchall()
    total = sum(int(row["amount"]) for row in rows)
    return render_template("expenses.html", rows=rows, total=total, start=start, end=end, today=now().date())

@app.post("/admin/expenses/<int:eid>/delete")
@staff_required("admin", "manager")
def delete_expense(eid):
    db().execute("DELETE FROM expenses WHERE id=?", (eid,)); db().commit()
    flash("Expense deleted.")
    return redirect(request.referrer or url_for("expenses"))

@app.route("/admin/reports.csv")
@staff_required("admin", "manager")
def reports_csv():
    start, end = _report_dates()
    r = _business_report(start, end)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ANNVIDHA FOODS - Order Report", start.isoformat(), end.isoformat()])
    writer.writerow(["Order ID", "Date", "Customer", "Phone", "Status", "Total INR"])
    for o in r["orders"]:
        writer.writerow([o["id"], o["created_at"], o["name"], o["phone"], o["status"], o["total"]])
    writer.writerow([])
    writer.writerow(["Metric", "Value INR / count"])
    for label, value in [("Non-cancelled order count", r["order_count"]), ("Delivered orders", r["delivered_count"]),
                         ("Cancelled orders", r["cancelled_count"]), ("Order value (non-cancelled)", r["gross_sales"]),
                         ("Delivered revenue", r["delivered_sales"]), ("Estimated cost of goods", r["cogs"]),
                         ("Gross profit before expenses", r["gross_profit"]), ("Operating expenses", r["expense_total"]),
                         ("Estimated net profit", r["net_profit"])]:
        writer.writerow([label, value])
    from flask import Response
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=annvidha-report-{start}-{end}.csv"})

@app.route("/admin/reports.pdf")
@staff_required("admin", "manager")
def reports_pdf():
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except ImportError:
        flash("PDF support is missing. Run: pip install reportlab")
        return redirect(url_for("analytics", **request.args))
    start, end = _report_dates()
    r = _business_report(start, end)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=14*mm, leftMargin=14*mm, topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    story = [Paragraph("ANNVIDHA FOODS — Business Report", styles["Title"]),
             Paragraph(f"Period: {start:%d %b %Y} to {end:%d %b %Y}", styles["Normal"]), Spacer(1, 8)]
    data = [["Metric", "Value"], ["Non-cancelled orders", str(r["order_count"])],
            ["Delivered orders", str(r["delivered_count"])], ["Pending orders", str(r["pending_count"])],
            ["Cancelled orders", str(r["cancelled_count"])], ["Order value (non-cancelled)", f"Rs. {r['gross_sales']:,}"],
            ["Delivered revenue", f"Rs. {r['delivered_sales']:,}"], ["Estimated cost of goods", f"Rs. {r['cogs']:,}"],
            ["Gross profit before expenses", f"Rs. {r['gross_profit']:,}"], ["Recorded expenses", f"Rs. {r['expense_total']:,}"],
            ["Estimated net profit", f"Rs. {r['net_profit']:,}"]]
    t = Table(data, colWidths=[90*mm, 65*mm])
    t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#183b2b")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
                           ("GRID",(0,0),(-1,-1),0.4,colors.grey),("PADDING",(0,0),(-1,-1),6),
                           ("BACKGROUND",(0,1),(-1,-1),colors.whitesmoke)]))
    story += [t, Spacer(1, 12), Paragraph("Top products (delivered orders)", styles["Heading2"])]
    pdata = [["Product", "Units", "Sales", "Estimated cost"]]
    pdata += [[p["name"], str(p["qty"]), f"Rs. {p['revenue']:,}", f"Rs. {p['cost']:,}"] for p in r["top_products"]]
    if len(pdata) == 1: pdata.append(["No delivered product sales", "0", "Rs. 0", "Rs. 0"])
    pt = Table(pdata, repeatRows=1, colWidths=[110*mm, 25*mm, 40*mm, 40*mm])
    pt.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#dbeafe")),("GRID",(0,0),(-1,-1),0.4,colors.grey),("PADDING",(0,0),(-1,-1),5)]))
    story += [pt, Spacer(1, 8), Paragraph("Important: profit is an estimate based on current product cost prices and recorded expenses. Historical costs may differ. Enter accurate product costs and all expenses for meaningful results.", styles["Italic"])]
    doc.build(story)
    from flask import Response
    return Response(buffer.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=annvidha-report-{start}-{end}.pdf"})

if __name__ == "__main__":
    app.run(debug=True)
