"""Instalador MLR Odoo — plataforma web EN LINEA (multiusuario) para desplegar codigos
Odoo por API (SaaS), con identidad MLR.

Secciones (con menu): Panel, Catalogo de codigos (apps con versiones), Conexiones (bases
Odoo guardadas, se eligen al instalar), Historial de instalaciones, y Usuarios (rol
administrador / usuario). En la instalacion solo se elige conexion + version; nada se teclea
en el momento (el grupo custom y los parametros van guardados de antes).

Roles:
  - administrador: todo (usuarios, conexiones, subir/borrar codigos, instalar, historial).
  - usuario: ve el catalogo e instala, ve conexiones (sin secretos) e historial.

Seguridad: login por usuario/contrasena (hash PBKDF2). API keys cifradas con una llave de
aplicacion del servidor (env APP_FERNET_KEY o DATA_DIR/.appkey). Ver security.py.
"""
import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, request, redirect, url_for, render_template,
                   flash, session, abort)

from odoo_client import OdooClient, OdooError
from installer import Installer, Report, send_report_email
import security as sec
import brand

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
APPS_DIR = os.path.join(DATA_DIR, "apps")
DB = os.path.join(DATA_DIR, "instalador.db")
os.makedirs(APPS_DIR, exist_ok=True)

APP_KEY = sec.load_app_key(DATA_DIR)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())


# --- SQLite ------------------------------------------------------------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL, pass_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'usuario', created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS connections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, url TEXT NOT NULL, database TEXT NOT NULL,
        username TEXT NOT NULL, api_key_enc TEXT NOT NULL DEFAULT '',
        group_xmlid TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE IF NOT EXISTS install_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, user TEXT, connection_name TEXT, database TEXT,
        app_code TEXT, version TEXT, ok INTEGER, warn INTEGER, err INTEGER,
        report_html TEXT)""")
    con.commit()
    con.close()


# --- Usuarios ----------------------------------------------------------
def any_users():
    con = db()
    n = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    con.close()
    return n > 0


def get_user(username):
    con = db()
    r = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    con.close()
    return r


def create_user(username, password, role):
    con = db()
    con.execute("INSERT INTO users(username,pass_hash,role,created_at) VALUES(?,?,?,?)",
                (username.strip(), sec.hash_password(password), role,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
    con.commit()
    con.close()


# --- Marca + contexto de menu -----------------------------------------
@app.context_processor
def inject_ctx():
    return {"brand": brand, "cur_user": session.get("auth"), "cur_role": session.get("role")}


# --- Auth --------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not any_users():
            return redirect(url_for("setup"))
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not any_users():
            return redirect(url_for("setup"))
        if not session.get("auth"):
            return redirect(url_for("login"))
        if session.get("role") != "administrador":
            flash("Necesitas rol de administrador para eso.")
            return redirect(url_for("dashboard"))
        return f(*a, **k)
    return wrapper


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if any_users():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        if not f.get("username") or not f.get("password") or f["password"] != f.get("confirm"):
            flash("Revisa usuario y que las contrasenas coincidan.")
            return redirect(url_for("setup"))
        create_user(f["username"], f["password"], "administrador")
        flash("Administrador creado. Inicia sesion.")
        return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not any_users():
        return redirect(url_for("setup"))
    if request.method == "POST":
        f = request.form
        u = get_user((f.get("username") or "").strip())
        if u and sec.verify_password(f.get("password", ""), u["pass_hash"]):
            session["auth"] = u["username"]
            session["role"] = u["role"]
            return redirect(url_for("dashboard"))
        flash("Usuario o contrasena incorrectos.")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesion cerrada.")
    return redirect(url_for("login"))


# --- Catalogo (helpers) -----------------------------------------------
def _safe(name, repl="_"):
    return re.sub(r"[^A-Za-z0-9._-]", repl, os.path.basename(name or "")).strip("._") or "x"


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def app_dir(app_code):
    return os.path.join(APPS_DIR, _safe(app_code))


def version_dir(app_code, version):
    return os.path.join(app_dir(app_code), "versions", _safe(version))


def list_apps():
    apps = []
    if not os.path.isdir(APPS_DIR):
        return apps
    for ac in sorted(os.listdir(APPS_DIR)):
        adir = os.path.join(APPS_DIR, ac)
        if not os.path.isdir(adir):
            continue
        meta = {}
        if os.path.isfile(os.path.join(adir, "app.json")):
            try:
                meta = _read_json(os.path.join(adir, "app.json"))
            except Exception:
                meta = {}
        vdir = os.path.join(adir, "versions")
        versions = []
        if os.path.isdir(vdir):
            for v in os.listdir(vdir):
                vp = os.path.join(vdir, v)
                if not os.path.isdir(vp):
                    continue
                vmeta = {}
                if os.path.isfile(os.path.join(vp, "_meta.json")):
                    try:
                        vmeta = _read_json(os.path.join(vp, "_meta.json"))
                    except Exception:
                        vmeta = {}
                devs = []
                for fn in sorted(os.listdir(vp)):
                    if fn.endswith(".json") and fn != "_meta.json":
                        try:
                            d = _read_json(os.path.join(vp, fn))
                        except Exception as e:
                            d = {"name": "ERROR %s: %s" % (fn, e), "order": 999}
                        d["_file"] = fn
                        devs.append(d)
                devs.sort(key=lambda d: (d.get("order", 999), d.get("_file", "")))
                versions.append({"version": v, "uploaded_at": vmeta.get("uploaded_at", ""),
                                 "devs": devs})
        versions.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
        apps.append({"app_code": ac, "app_name": meta.get("app_name", ac), "versions": versions})
    return apps


def connections_list():
    con = db()
    rows = con.execute("SELECT * FROM connections ORDER BY name").fetchall()
    con.close()
    return rows


def _conn(cid):
    con = db()
    c = con.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
    con.close()
    return c


def save_install_log(user, conn, app_code, version, rep):
    ok = sum(1 for l in rep.lines if l.startswith("[OK]"))
    con = db()
    con.execute("""INSERT INTO install_logs(ts,user,connection_name,database,app_code,version,ok,warn,err,report_html)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), user or "",
                 conn["name"] if conn else "", conn["database"] if conn else "",
                 app_code, version, ok, len(rep.warnings), len(rep.errors), rep.html()))
    con.commit()
    con.close()


# --- Paginas -----------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    apps = list_apps()
    conns = connections_list()
    con = db()
    nlogs = con.execute("SELECT COUNT(*) c FROM install_logs").fetchone()["c"]
    nusers = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    last = con.execute("""SELECT ts,app_code,version,connection_name,ok,warn,err
                          FROM install_logs ORDER BY id DESC LIMIT 5""").fetchall()
    con.close()
    nver = sum(len(a["versions"]) for a in apps)
    return render_template("dashboard.html", n_apps=len(apps), n_ver=nver,
                           n_conns=len(conns), n_logs=nlogs, n_users=nusers, last=last)


@app.route("/catalogo")
@login_required
def catalogo():
    return render_template("catalogo.html", apps=list_apps(), connections=connections_list())


@app.route("/upload", methods=["POST"])
@admin_required
def upload():
    fil = request.files.get("bundle")
    if not fil or not fil.filename:
        flash("Elige un archivo .json o .zip.")
        return redirect(url_for("catalogo"))
    overwrite = request.form.get("overwrite") == "1"
    try:
        devs = _extract_devs(fil)
    except Exception as e:
        flash("No se pudo leer el archivo: %s" % e)
        return redirect(url_for("catalogo"))
    if not devs:
        flash("El archivo no contiene codigos .json.")
        return redirect(url_for("catalogo"))
    first = devs[0][1]
    app_code = _safe(request.form.get("app_code") or first.get("app_code") or first.get("app") or "sin_codigo")
    app_name = (request.form.get("app_name") or first.get("app") or app_code).strip()
    version = _safe(request.form.get("version") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    vdir = version_dir(app_code, version)
    if os.path.isdir(vdir) and not overwrite:
        flash("Ya existe la version '%s' de la app '%s'. Marca 'Sobrescribir' para reemplazarla."
              % (version, app_code))
        return redirect(url_for("catalogo"))
    os.makedirs(vdir, exist_ok=True)
    for old in os.listdir(vdir):
        if old.endswith(".json"):
            os.remove(os.path.join(vdir, old))
    saved = []
    for name, data in devs:
        if not name.endswith(".json"):
            name += ".json"
        with open(os.path.join(vdir, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        saved.append(name)
    with open(os.path.join(app_dir(app_code), "app.json"), "w", encoding="utf-8") as fh:
        json.dump({"app_code": app_code, "app_name": app_name}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(vdir, "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version, "app_code": app_code, "app_name": app_name,
                   "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "files": saved}, fh, ensure_ascii=False, indent=2)
    flash("App '%s' version '%s': %d codigo(s) guardado(s)%s."
          % (app_name, version, len(saved), " (sobrescrita)" if overwrite else ""))
    return redirect(url_for("catalogo"))


def _extract_devs(fil):
    fn = fil.filename.lower()
    out = []
    if fn.endswith(".json"):
        out.append((_safe(fil.filename), json.load(fil.stream)))
    elif fn.endswith(".zip"):
        zf = zipfile.ZipFile(io.BytesIO(fil.read()))
        for m in zf.namelist():
            if m.endswith("/") or not m.lower().endswith(".json"):
                continue
            if os.path.basename(m) == "_meta.json":
                continue
            out.append((_safe(m), json.loads(zf.read(m).decode("utf-8"))))
    else:
        raise ValueError("Formato no soportado (sube .json o .zip).")
    return out


@app.route("/version/delete", methods=["POST"])
@admin_required
def version_delete():
    ac = request.form["app_code"]; v = request.form["version"]
    vdir = version_dir(ac, v)
    import shutil
    if os.path.isdir(vdir):
        shutil.rmtree(vdir)
        flash("Eliminada version '%s' de '%s'." % (v, ac))
        adir = app_dir(ac); vroot = os.path.join(adir, "versions")
        if os.path.isdir(vroot) and not [x for x in os.listdir(vroot)
                                         if os.path.isdir(os.path.join(vroot, x))]:
            shutil.rmtree(adir)
    return redirect(url_for("catalogo"))


# --- Conexiones --------------------------------------------------------
@app.route("/conexiones")
@login_required
def conexiones():
    return render_template("conexiones.html", connections=connections_list())


@app.route("/connection/save", methods=["POST"])
@admin_required
def conn_save():
    f = request.form
    enc = sec.encrypt(APP_KEY, f.get("api_key", "")) if f.get("api_key") else None
    con = db()
    if f.get("id"):
        if enc is not None:
            con.execute("UPDATE connections SET name=?,url=?,database=?,username=?,api_key_enc=?,group_xmlid=? WHERE id=?",
                        (f["name"], f["url"], f["database"], f["username"], enc, f.get("group_xmlid", ""), f["id"]))
        else:
            con.execute("UPDATE connections SET name=?,url=?,database=?,username=?,group_xmlid=? WHERE id=?",
                        (f["name"], f["url"], f["database"], f["username"], f.get("group_xmlid", ""), f["id"]))
    else:
        con.execute("INSERT INTO connections(name,url,database,username,api_key_enc,group_xmlid) VALUES(?,?,?,?,?,?)",
                    (f["name"], f["url"], f["database"], f["username"], enc or "", f.get("group_xmlid", "")))
    con.commit()
    con.close()
    flash("Conexion guardada.")
    return redirect(url_for("conexiones"))


@app.route("/connection/delete/<int:cid>")
@admin_required
def conn_delete(cid):
    con = db()
    con.execute("DELETE FROM connections WHERE id=?", (cid,))
    con.commit()
    con.close()
    flash("Conexion eliminada.")
    return redirect(url_for("conexiones"))


@app.route("/connection/test/<int:cid>")
@admin_required
def conn_test(cid):
    c = _conn(cid)
    try:
        key = sec.decrypt(APP_KEY, c["api_key_enc"])
        if not key:
            flash("Esta conexion no tiene API key; editala y guardala.")
            return redirect(url_for("conexiones"))
        cli = OdooClient(c["url"], c["database"], c["username"], key)
        uid = cli.connect()
        flash("Conexion OK (uid %s) en %s / %s" % (uid, c["url"], c["database"]))
    except (OdooError, ValueError) as e:
        flash("Fallo de conexion: %s" % e)
    return redirect(url_for("conexiones"))


# --- Instalar ----------------------------------------------------------
def _apply_group(dev, c):
    if not dev.get("group_xmlid") and c["group_xmlid"]:
        dev["group_xmlid"] = c["group_xmlid"]
    return dev


def _client_for(c):
    key = sec.decrypt(APP_KEY, c["api_key_enc"])
    if not key:
        raise OdooError("La conexion no tiene API key guardada (editala y guardala).")
    cli = OdooClient(c["url"], c["database"], c["username"], key)
    cli.connect()
    return cli


@app.route("/install", methods=["POST"])
@login_required
def install():
    c = _conn(request.form["connection_id"])
    app_code = request.form["app_code"]; version = request.form["version"]
    send_mail = request.form.get("send_mail") == "1"
    rep = Report()
    to = None
    try:
        cli = _client_for(c)
        rep.info("Conectado a %s / %s como uid %s" % (c["url"], c["database"], cli.uid))
        vdir = version_dir(app_code, version)
        files = sorted(fn for fn in os.listdir(vdir) if fn.endswith(".json") and fn != "_meta.json")
        devs = []
        for fn in files:
            d = _read_json(os.path.join(vdir, fn)); d["_file"] = fn; devs.append(d)
        devs.sort(key=lambda d: (d.get("order", 999), d.get("_file", "")))
        rep.info("Instalando app '%s' version '%s' (%d codigos, en orden)"
                 % (app_code, version, len(devs)))
        for d in devs:
            dev = _apply_group(d, c)
            rep.info("=" * 40)
            rep.info(">>> CODIGO: %s  [app %s · v%s]" % (dev.get("name"), app_code, version))
            Installer(cli, dev, rep).run()
        if send_mail:
            to = send_report_email(cli, rep, "%s v%s" % (app_code, version))
    except (OdooError, ValueError) as e:
        rep.err("Conexion: %s" % e)
    except Exception as e:
        rep.err("Error inesperado: %s" % e)
    try:
        save_install_log(session.get("auth"), c, app_code, version, rep)
    except Exception as e:
        rep.info("No se pudo guardar el historial: %s" % e)
    return render_template("report.html", report=rep, conn=c,
                           devfile="%s · version %s" % (app_code, version), mail_to=to)


# --- Historial ---------------------------------------------------------
@app.route("/logs")
@login_required
def logs():
    q_app = (request.args.get("app") or "").strip()
    q_from = (request.args.get("from") or "").strip()
    q_to = (request.args.get("to") or "").strip()
    sql = ("SELECT id,ts,user,connection_name,database,app_code,version,ok,warn,err "
           "FROM install_logs WHERE 1=1")
    args = []
    if q_app:
        sql += " AND app_code LIKE ?"; args.append("%" + q_app + "%")
    if q_from:
        sql += " AND ts >= ?"; args.append(q_from)
    if q_to:
        sql += " AND ts <= ?"; args.append(q_to + "T23:59:59")
    sql += " ORDER BY id DESC LIMIT 500"
    con = db()
    rows = con.execute(sql, args).fetchall()
    con.close()
    return render_template("logs.html", rows=rows, q_app=q_app, q_from=q_from, q_to=q_to)


@app.route("/logs/<int:lid>")
@login_required
def log_view(lid):
    con = db()
    r = con.execute("SELECT * FROM install_logs WHERE id=?", (lid,)).fetchone()
    con.close()
    if not r:
        return redirect(url_for("logs"))
    return render_template("log_view.html", r=r)


# --- Usuarios (admin) --------------------------------------------------
@app.route("/usuarios")
@admin_required
def usuarios():
    con = db()
    rows = con.execute("SELECT id,username,role,created_at FROM users ORDER BY username").fetchall()
    con.close()
    return render_template("usuarios.html", rows=rows)


@app.route("/usuarios/crear", methods=["POST"])
@admin_required
def user_create():
    f = request.form
    role = f.get("role") if f.get("role") in ("administrador", "usuario") else "usuario"
    if not f.get("username") or not f.get("password"):
        flash("Usuario y contrasena requeridos.")
        return redirect(url_for("usuarios"))
    if get_user(f["username"].strip()):
        flash("Ese usuario ya existe.")
        return redirect(url_for("usuarios"))
    create_user(f["username"], f["password"], role)
    flash("Usuario creado.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/rol", methods=["POST"])
@admin_required
def user_role():
    f = request.form
    role = f.get("role") if f.get("role") in ("administrador", "usuario") else "usuario"
    con = db()
    if role != "administrador":
        admins = con.execute("SELECT COUNT(*) c FROM users WHERE role='administrador'").fetchone()["c"]
        target = con.execute("SELECT role FROM users WHERE id=?", (f["id"],)).fetchone()
        if target and target["role"] == "administrador" and admins <= 1:
            con.close()
            flash("No puedes quitar el ultimo administrador.")
            return redirect(url_for("usuarios"))
    con.execute("UPDATE users SET role=? WHERE id=?", (role, f["id"]))
    con.commit()
    con.close()
    flash("Rol actualizado.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/reset", methods=["POST"])
@admin_required
def user_reset():
    f = request.form
    if not f.get("password"):
        flash("Contrasena requerida.")
        return redirect(url_for("usuarios"))
    con = db()
    con.execute("UPDATE users SET pass_hash=? WHERE id=?", (sec.hash_password(f["password"]), f["id"]))
    con.commit()
    con.close()
    flash("Contrasena actualizada.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/borrar/<int:uid>")
@admin_required
def user_delete(uid):
    con = db()
    u = con.execute("SELECT username,role FROM users WHERE id=?", (uid,)).fetchone()
    admins = con.execute("SELECT COUNT(*) c FROM users WHERE role='administrador'").fetchone()["c"]
    if u and u["role"] == "administrador" and admins <= 1:
        con.close()
        flash("No puedes borrar el ultimo administrador.")
        return redirect(url_for("usuarios"))
    if u and u["username"] == session.get("auth"):
        con.close()
        flash("No puedes borrarte a ti mismo mientras estas conectado.")
        return redirect(url_for("usuarios"))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()
    flash("Usuario eliminado.")
    return redirect(url_for("usuarios"))


@app.route("/healthz")
def healthz():
    return "ok"


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
