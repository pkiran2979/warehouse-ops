import os, io, time, secrets, datetime as dt
from functools import wraps
import pandas as pd
from flask import (Flask, render_template, request, redirect, url_for, flash, send_file, abort, session, g, has_request_context)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.utils import secure_filename
import rag

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE, "uploads")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///" + os.path.join(BASE, "warehouse.db")),
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),   # random per start if unset (sessions reset on restart)
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("HTTPS", "0") == "1",   # set HTTPS=1 behind TLS
    PERMANENT_SESSION_LIFETIME=dt.timedelta(minutes=int(os.getenv("IDLE_MINUTES", "30"))),  # idle timeout
)
db = SQLAlchemy(app)

# category -> storage zone (putaway rule)
ZONE_RULES = {"Cement & Binders": "DRY", "Steel & Rebar": "YARD", "Paints & Chemicals": "CHEM",
              "Tiles & Finishes": "COVERED", "Electrical": "SECURE", "Plumbing": "COVERED",
              "Timber": "COVERED", "Aggregates & Blocks": "YARD"}
ZONE_NOTES = {"DRY": "Dry, raised pallets; keep away from moisture (cement)", "YARD": "Open yard; heavy items, ground level",
              "CHEM": "Ventilated, shelf-life tracked (FEFO), away from heat", "COVERED": "Covered racking, protect from damp",
              "SECURE": "Locked cage for high-value items"}
ALLOWED_DOCS = {"pdf", "docx", "txt", "md", "xlsx", "csv"}


ROLES = {"admin": "Admin (everything + users)", "storekeeper": "Storekeeper (receive, issue, count, import, upload)",
         "viewer": "Viewer (read-only + downloads)"}
WRITE_ROLES = ("admin", "storekeeper")


def current_username():
    u = getattr(g, "user", None) if has_request_context() else None
    return u.username if u else "system"


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    full_name = db.Column(db.String(120))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="viewer", nullable=False)
    active = db.Column(db.Boolean, default=True)
    must_change = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    last_login = db.Column(db.DateTime)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check(self, pw):
        return check_password_hash(self.password_hash, pw)


class Material(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    unit = db.Column(db.String(20), default="pcs")
    min_stock = db.Column(db.Float, default=0)
    shelf_life_days = db.Column(db.Integer)
    storage_rule = db.Column(db.String(255))

    @property
    def zone(self):
        return ZONE_RULES.get(self.category, "COVERED")


class Bin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    zone = db.Column(db.String(20), nullable=False)
    capacity = db.Column(db.Float, default=1000)


class Movement(db.Model):
    """Append-only stock ledger. Stock on hand = SUM(qty)."""
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=dt.datetime.utcnow)
    material_id = db.Column(db.Integer, db.ForeignKey("material.id"), nullable=False)
    bin_id = db.Column(db.Integer, db.ForeignKey("bin.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)  # + in, - out
    mtype = db.Column(db.String(12), nullable=False)  # GRN / ISSUE / COUNT / OPENING
    ref = db.Column(db.String(60))
    note = db.Column(db.String(255))
    expiry = db.Column(db.Date)
    user = db.Column(db.String(60), default=lambda: current_username())
    material = db.relationship("Material")
    bin = db.relationship("Bin")


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    stored_as = db.Column(db.String(255))
    doc_type = db.Column(db.String(30))
    chunks = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=dt.datetime.utcnow)


# ---------- helpers ----------
def stock_by_material():
    rows = db.session.query(Movement.material_id, func.sum(Movement.qty)).group_by(Movement.material_id).all()
    return {m: q for m, q in rows}


def stock_in_bin(material_id, bin_id):
    return db.session.query(func.coalesce(func.sum(Movement.qty), 0)).filter_by(
        material_id=material_id, bin_id=bin_id).scalar()


def suggest_bin(material):
    """Putaway: emptiest bin in the material's zone."""
    bins = Bin.query.filter_by(zone=material.zone).all()
    if not bins:
        return None
    load = dict(db.session.query(Movement.bin_id, func.sum(Movement.qty)).group_by(Movement.bin_id).all())
    return min(bins, key=lambda b: (load.get(b.id, 0) or 0) / (b.capacity or 1))


def stock_df():
    stock = stock_by_material()
    rows = []
    for m in Material.query.order_by(Material.category, Material.name):
        q = stock.get(m.id, 0) or 0
        rows.append({"SKU": m.sku, "Material": m.name, "Category": m.category, "Unit": m.unit,
                     "On hand": q, "Min stock": m.min_stock, "Status": "LOW" if q < m.min_stock else "OK"})
    return pd.DataFrame(rows)


def ledger_df(q=None):
    rows = []
    for mv in q.all():
        rows.append({"Date": mv.ts, "SKU": mv.material.sku, "Material": mv.material.name, "Bin": mv.bin.code,
                     "Type": mv.mtype, "Qty": mv.qty, "Reference": mv.ref, "Note": mv.note, "User": mv.user})
    return pd.DataFrame(rows)


def xlsx_response(sheets, name):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for sheet, df in sheets.items():
            df.to_excel(w, sheet_name=sheet, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")



# ---------- authentication & authorisation ----------
PUBLIC = {"login", "static"}
WRITE_PAGES = {"receive", "issue", "count", "import_excel"}   # viewers cannot even open these
ADMIN_ONLY = {"users", "user_create", "user_update", "user_reset", "delete_doc"}
FAILS = {}   # username -> (count, first_fail_ts); simple brute-force throttle


@app.before_request
def guard():
    g.user = None
    if request.endpoint is None or request.endpoint in PUBLIC:
        return
    uid = session.get("uid")
    user = db.session.get(User, uid) if uid else None
    if not user or not user.active:
        session.clear()
        flash("Please sign in to continue", "info")
        return redirect(url_for("login", next=request.path))
    g.user = user
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf_token", ""), session.get("csrf", "x")):
            abort(400, "Invalid form token, reload the page")
    if user.must_change and request.endpoint not in {"account", "logout"}:
        flash("Please set a new password to continue", "warning")
        return redirect(url_for("account"))
    ep = request.endpoint
    if ep in ADMIN_ONLY and user.role != "admin":
        abort(403)
    if user.role not in WRITE_ROLES and (ep in WRITE_PAGES or (request.method == "POST" and ep not in {"account", "logout"})):
        abort(403)


@app.after_request
def secure_headers(resp):
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; "
                                               "frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if not request.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"   # never cache stock data or documents
    return resp


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    u = getattr(g, "user", None)
    return {"me": u, "csrf_token": session["csrf"], "can_write": bool(u and u.role in WRITE_ROLES)}


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, msg="Your role does not allow this action."), 403


@app.errorhandler(400)
def bad(e):
    return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400


COMMON = {"password1", "admin123", "welcome1", "password123", "12345678a", "qwerty123", "letmein123"}


def valid_password(pw, username=""):
    return (len(pw) >= 8 and any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw)
            and pw.lower() not in COMMON and pw.lower() != username.lower())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        n, first = FAILS.get(name, (0, time.time()))
        if n >= 5 and time.time() - first < 300:
            flash("Too many attempts. Try again in a few minutes.", "danger")
            return render_template("login.html"), 429
        user = User.query.filter(func.lower(User.username) == name).first()
        if user and user.active and user.check(request.form.get("password", "")):
            FAILS.pop(name, None)
            session.clear()
            session.permanent = True
            session["uid"] = user.id
            session["csrf"] = secrets.token_hex(16)
            user.last_login = dt.datetime.utcnow()
            db.session.commit()
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        FAILS[name] = (n + 1 if time.time() - first < 300 else 1, first if time.time() - first < 300 else time.time())
        flash("Invalid username or password", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        cur, new = request.form.get("current", ""), request.form.get("new", "")
        if not g.user.check(cur):
            flash("Current password is incorrect", "danger")
        elif new != request.form.get("confirm"):
            flash("New passwords do not match", "danger")
        elif not valid_password(new, g.user.username):
            flash("Password needs 8+ characters with letters and numbers, and must not be a common password", "danger")
        else:
            g.user.set_password(new); g.user.must_change = False
            db.session.commit()
            flash("Password updated", "success")
            return redirect(url_for("dashboard"))
    return render_template("account.html")


def active_admins():
    return User.query.filter_by(role="admin", active=True).count()


@app.route("/users")
def users():
    return render_template("users.html", users=User.query.order_by(User.username).all(), roles=ROLES)


@app.route("/users/create", methods=["POST"])
def user_create():
    f = request.form
    name = f.get("username", "").strip().lower()
    if not name.replace("_", "").replace(".", "").isalnum() or User.query.filter(func.lower(User.username) == name).first():
        flash("Username invalid or already taken", "danger")
    elif f.get("role") not in ROLES or not valid_password(f.get("password", "")):
        flash("Choose a role and a password with 8+ characters, letters and numbers", "danger")
    else:
        u = User(username=name, full_name=f.get("full_name"), role=f["role"], must_change=True)
        u.set_password(f["password"])
        db.session.add(u); db.session.commit()
        flash(f"User {name} created (must change password at first login)", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/update", methods=["POST"])
def user_update(uid):
    u = db.get_or_404(User, uid)
    role, active = request.form.get("role", u.role), request.form.get("active") == "1"
    if role not in ROLES:
        abort(400)
    losing_admin = u.role == "admin" and u.active and (role != "admin" or not active)
    if losing_admin and active_admins() <= 1:
        flash("There must be at least one active admin", "danger")
    else:
        u.role, u.active = role, active
        db.session.commit()
        flash(f"Updated {u.username}", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/reset", methods=["POST"])
def user_reset(uid):
    u = db.get_or_404(User, uid)
    pw = request.form.get("password", "")
    if not valid_password(pw):
        flash("Password needs 8+ characters with letters and numbers", "danger")
    else:
        u.set_password(pw); u.must_change = True
        db.session.commit()
        flash(f"Password reset for {u.username}; they must change it at next login", "success")
    return redirect(url_for("users"))


# ---------- routes ----------
@app.route("/")
def dashboard():
    stock = stock_by_material()
    mats = Material.query.all()
    low = [(m, stock.get(m.id, 0) or 0) for m in mats if (stock.get(m.id, 0) or 0) < m.min_stock]
    week = dt.datetime.utcnow() - dt.timedelta(days=7)
    inb = db.session.query(func.coalesce(func.sum(Movement.qty), 0)).filter(Movement.mtype == "GRN", Movement.ts >= week).scalar()
    outb = -db.session.query(func.coalesce(func.sum(Movement.qty), 0)).filter(Movement.mtype == "ISSUE", Movement.ts >= week).scalar()
    soon = dt.date.today() + dt.timedelta(days=60)
    expiring = Movement.query.filter(Movement.expiry != None, Movement.expiry <= soon, Movement.qty > 0).order_by(Movement.expiry).limit(8).all()
    cat = {}
    for m in mats:
        cat[m.category] = cat.get(m.category, 0) + (stock.get(m.id, 0) or 0)
    recent = Movement.query.order_by(Movement.ts.desc()).limit(8).all()
    return render_template("dashboard.html", recent=recent, n_mat=len(mats), n_bins=Bin.query.count(), low=low,
                           inb=inb, outb=outb, expiring=expiring, cat=cat)


@app.route("/materials", methods=["GET", "POST"])
def materials():
    if request.method == "POST":
        f = request.form
        if Material.query.filter_by(sku=f["sku"].strip()).first():
            flash("SKU already exists", "danger")
        else:
            db.session.add(Material(sku=f["sku"].strip(), name=f["name"], category=f["category"], unit=f["unit"],
                                    min_stock=float(f.get("min_stock") or 0),
                                    shelf_life_days=int(f["shelf_life_days"]) if f.get("shelf_life_days") else None,
                                    storage_rule=f.get("storage_rule")))
            db.session.commit()
            flash("Material added", "success")
        return redirect(url_for("materials"))
    stock = stock_by_material()
    return render_template("materials.html", mats=Material.query.order_by(Material.category, Material.name).all(),
                           stock=stock, cats=list(ZONE_RULES))


@app.route("/import", methods=["GET", "POST"])
def import_excel():
    """Upload .xlsx with sheet 'Materials' (and optional 'OpeningStock')."""
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith((".xlsx", ".xls")):
            flash("Please upload an Excel file", "danger")
            return redirect(url_for("import_excel"))
        try:
            sheets = pd.read_excel(file, sheet_name=None)
            added = updated = opened = 0
            for _, r in sheets.get("Materials", pd.DataFrame()).iterrows():
                sku = str(r.get("sku", "")).strip()
                if not sku or sku == "nan":
                    continue
                m = Material.query.filter_by(sku=sku).first()
                new = m is None
                m = m or Material(sku=sku)
                m.name = str(r.get("name", sku)); m.category = str(r.get("category", "Aggregates & Blocks"))
                m.unit = str(r.get("unit", "pcs")); m.min_stock = float(r.get("min_stock", 0) or 0)
                sl = r.get("shelf_life_days")
                m.shelf_life_days = int(sl) if pd.notna(sl) else None
                sr = r.get("storage_rule"); m.storage_rule = str(sr) if pd.notna(sr) else None
                db.session.add(m); added += new; updated += (not new)
            db.session.flush()
            for _, r in sheets.get("OpeningStock", pd.DataFrame()).iterrows():
                m = Material.query.filter_by(sku=str(r.get("sku", "")).strip()).first()
                b = Bin.query.filter_by(code=str(r.get("bin", "")).strip()).first()
                if m and b and float(r.get("qty", 0) or 0) > 0:
                    db.session.add(Movement(material_id=m.id, bin_id=b.id, qty=float(r["qty"]), mtype="OPENING", ref="IMPORT"))
                    opened += 1
            db.session.commit()
            flash(f"Import done: {added} new, {updated} updated materials, {opened} opening stock lines", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Import failed: {e}", "danger")
        return redirect(url_for("import_excel"))
    return render_template("import.html")


@app.route("/receive", methods=["GET", "POST"])
def receive():
    mats = Material.query.order_by(Material.name).all()
    if request.method == "POST":
        m = db.session.get(Material, int(request.form["material_id"]))
        qty = float(request.form["qty"])
        b = db.session.get(Bin, int(request.form["bin_id"])) if request.form.get("bin_id") else suggest_bin(m)
        if qty <= 0 or not b:
            flash("Quantity must be greater than zero, and a storage bin must be available for this material", "danger")
            return redirect(url_for("receive"))
        exp = request.form.get("expiry")
        if not exp and m.shelf_life_days:
            exp = (dt.date.today() + dt.timedelta(days=m.shelf_life_days)).isoformat()
        db.session.add(Movement(material_id=m.id, bin_id=b.id, qty=qty, mtype="GRN", ref=request.form.get("ref") or "GRN",
                                note=request.form.get("note"), expiry=dt.date.fromisoformat(exp) if exp else None))
        db.session.commit()
        flash(f"Received {qty:g} {m.unit} of {m.name} into {b.code}", "success")
        return redirect(url_for("receive"))
    sugg = {m.id: (suggest_bin(m).code if suggest_bin(m) else "-") for m in mats}
    mine = Movement.query.filter_by(mtype="GRN", user=g.user.username).order_by(Movement.ts.desc()).limit(5).all()
    return render_template("receive.html", mats=mats, bins=Bin.query.order_by(Bin.code).all(), sugg=sugg,
                           zone_notes=ZONE_NOTES, selected=request.args.get("material_id", type=int), mine=mine)


@app.route("/issue", methods=["GET", "POST"])
def issue():
    if request.method == "POST":
        m = db.session.get(Material, int(request.form["material_id"]))
        b = db.session.get(Bin, int(request.form["bin_id"]))
        qty = float(request.form["qty"])
        if qty <= 0 or qty > stock_in_bin(m.id, b.id):
            flash(f"Not enough stock in {b.code} (available {stock_in_bin(m.id, b.id):g})", "danger")
        else:
            db.session.add(Movement(material_id=m.id, bin_id=b.id, qty=-qty, mtype="ISSUE",
                                    ref=request.form.get("ref") or "SITE", note=request.form.get("note")))
            db.session.commit()
            flash(f"Issued {qty:g} {m.unit} of {m.name} from {b.code}", "success")
        return redirect(url_for("issue"))
    rows = db.session.query(Movement.material_id, Movement.bin_id, func.sum(Movement.qty)).group_by(
        Movement.material_id, Movement.bin_id).having(func.sum(Movement.qty) > 0).all()
    avail = [{"m": db.session.get(Material, a), "b": db.session.get(Bin, b), "q": q} for a, b, q in rows]
    avail.sort(key=lambda x: x["m"].name)
    mine = Movement.query.filter_by(mtype="ISSUE", user=g.user.username).order_by(Movement.ts.desc()).limit(5).all()
    return render_template("issue.html", avail=avail, mine=mine)


@app.route("/api/stock")
def api_stock():
    mid, bid = request.args.get("material_id", type=int), request.args.get("bin_id", type=int)
    m = db.session.get(Material, mid) if mid else None
    if not m or not bid:
        abort(400)
    return {"qty": stock_in_bin(mid, bid), "unit": m.unit}


@app.route("/count", methods=["GET", "POST"])
def count():
    """Cycle count: enter counted qty; the difference is posted as a COUNT adjustment."""
    if request.method == "POST":
        m = db.session.get(Material, int(request.form["material_id"]))
        b = db.session.get(Bin, int(request.form["bin_id"]))
        counted = float(request.form["counted"])
        diff = counted - stock_in_bin(m.id, b.id)
        if diff:
            db.session.add(Movement(material_id=m.id, bin_id=b.id, qty=diff, mtype="COUNT", ref="CYCLE-COUNT",
                                    note=request.form.get("note") or "Cycle count adjustment"))
            db.session.commit()
        flash(f"Count recorded. Variance: {diff:+g}", "success" if not diff else "warning")
        return redirect(url_for("count"))
    return render_template("count.html", mats=Material.query.order_by(Material.name).all(),
                           bins=Bin.query.order_by(Bin.code).all())


@app.route("/ledger")
def ledger():
    q = Movement.query.order_by(Movement.ts.desc())
    t = request.args.get("type")
    if t:
        q = q.filter_by(mtype=t)
    if request.args.get("sku"):
        q = q.join(Material).filter(Material.sku.ilike(f"%{request.args['sku']}%"))
    if request.args.get("fmt") == "xlsx":
        return xlsx_response({"Ledger": ledger_df(q)}, "stock_ledger.xlsx")
    return render_template("ledger.html", rows=q.limit(200).all(), t=t or "", sku=request.args.get("sku", ""))


@app.route("/export/stock.xlsx")
def export_stock():
    return xlsx_response({"Stock": stock_df()}, "stock_on_hand.xlsx")


@app.route("/export/template.xlsx")
def export_template():
    mats = pd.DataFrame([{"sku": "CEM-001", "name": "OPC Cement 50kg", "category": "Cement & Binders", "unit": "bag",
                          "min_stock": 200, "shelf_life_days": 90, "storage_rule": "Keep dry"}])
    opening = pd.DataFrame([{"sku": "CEM-001", "bin": "DRY-01", "qty": 500}])
    return xlsx_response({"Materials": mats, "OpeningStock": opening}, "warehouse_import_template.xlsx")


@app.route("/documents", methods=["GET", "POST"])
def documents():
    results, q = [], request.args.get("q", "")
    if request.method == "POST":
        f = request.files.get("file")
        ext = f.filename.rsplit(".", 1)[-1].lower() if f and "." in f.filename else ""
        if not f or ext not in ALLOWED_DOCS:
            flash("Allowed: " + ", ".join(sorted(ALLOWED_DOCS)), "danger")
            return redirect(url_for("documents"))
        stored = f"{dt.datetime.utcnow():%Y%m%d%H%M%S}_{secure_filename(f.filename)}"
        path = os.path.join(UPLOADS, stored)
        f.save(path)
        d = Document(filename=f.filename, stored_as=stored, doc_type=request.form.get("doc_type", "SOP"))
        db.session.add(d); db.session.flush()
        if ext in ("xlsx",):
            text = "\n".join(df.to_csv(index=False) for df in pd.read_excel(path, sheet_name=None).values())
        else:
            text = rag.extract_text(path)
        d.chunks = rag.index_document(d.id, d.filename, d.doc_type, text)
        db.session.commit()
        flash(f"Uploaded and indexed ({d.chunks} chunks)", "success")
        return redirect(url_for("documents"))
    if q:
        results = rag.search(q)
    return render_template("documents.html", docs=Document.query.order_by(Document.uploaded_at.desc()).all(),
                           results=results, q=q, backend=rag.backend())


@app.route("/documents/<int:doc_id>/download")
def download_doc(doc_id):
    d = db.get_or_404(Document, doc_id)
    return send_file(os.path.join(UPLOADS, d.stored_as), as_attachment=True, download_name=d.filename)


@app.route("/documents/<int:doc_id>/delete", methods=["POST"])
def delete_doc(doc_id):
    d = db.get_or_404(Document, doc_id)
    rag.delete_document(d.id)
    try:
        os.remove(os.path.join(UPLOADS, d.stored_as))
    except OSError:
        pass
    db.session.delete(d); db.session.commit()
    return redirect(url_for("documents"))


with app.app_context():
    db.create_all()
    if User.query.count() == 0:   # first run: bootstrap an admin who must change the password
        first_pw = os.getenv("ADMIN_PASSWORD") or (secrets.token_urlsafe(9) + "1a")
        a = User(username="admin", full_name="Administrator", role="admin", must_change=True)
        a.set_password(first_pw)
        db.session.add(a); db.session.commit()
        if not os.getenv("ADMIN_PASSWORD"):
            print(f"\n*** First run: sign in as 'admin' with password: {first_pw}  (you must change it) ***\n")

if __name__ == "__main__":
    app.run(debug=True, port=5001)
