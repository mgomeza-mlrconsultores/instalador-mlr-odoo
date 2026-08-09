"""Instalador MLR Odoo — plataforma web EN LINEA para desplegar codigos por API (SaaS).

- Identidad MLR, login con contrasena maestra, API keys cifradas (security.py).
- Catalogo de codigos con HISTORIAL DE VERSIONES por app:
    apps / <codigo_app> / versions / <version> / *.json
  Subes bundles (.zip con varios .json) o codigos sueltos (.json); cada subida es una
  VERSION de esa app. Puedes instalar la ultima o una version anterior. Si la version ya
  existe, avisa y solo la reemplaza si marcas 'Sobrescribir'.
- 100% libre: Flask + SQLite + cryptography + gunicorn. Lista para nube gratis (Render).

Local:      pip install -r requirements.txt ; python app.py  -> http://127.0.0.1:5000
Produccion: gunicorn app:app                                  (ver README_DESPLIEGUE.md)
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
                   flash, session)

from odoo_client import OdooClient, OdooError
from installer import Installer, Report, send_report_email
import security as sec
import brand

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
APPS_DIR = os.path.join(DATA_DIR, "apps")
DB = os.path.join(DATA_DIR, "instalador.db")
os.makedirs(APPS_DIR, exist_ok=True)

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
    con.execute("""CREATE TABLE IF NOT EXISTS connections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, url TEXT NOT NULL, database TEXT NOT NULL,
        username TEXT NOT NULL, api_key_enc TEXT NOT NULL DEFAULT '',
        group_xmlid TEXT DEFAULT '')""")
    con.commit()
    con.close()


def setting(key, default=None):
    con = db()
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return r["value"] if r else default


def set_setting(key, value):
    con = db()
    con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()


@app.context_processor
def inject_brand():
    return {"brand": brand}


# --- Auth --------------------------------------------------------------
def is_configured():
    return bool(setting("verifier"))


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not is_configured():
            return redirect(url_for("setup"))
        if not session.get("auth") or not session.get("fkey"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def fkey():
    return session["fkey"].encode()


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if is_configured():
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        if not f.get("password") or f["password"] != f.get("confirm"):
            flash("Las contrasenas no coinciden.")
            return redirect(url_for("setup"))
        salt = sec.new_salt()
        key = sec.derive_key(f["password"], salt)
        set_setting("salt", salt)
        set_setting("verifier", sec.make_verifier(key))
        set_setting("admin_user", (f.get("username") or "mlr").strip())
        flash("Configuracion creada. Inicia sesion.")
        return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not is_configured():
        return redirect(url_for("setup"))
    if request.method == "POST":
        f = request.form
        user = (f.get("username") or "").strip()
        key = sec.derive_key(f.get("password", ""), setting("salt"))
        if user == setting("admin_user") and sec.check_verifier(key, setting("verifier")):
            session["auth"] = user
            session["fkey"] = key.decode()
            return redirect(url_for("index"))
        flash("Usuario o contrasena incorrectos.")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesion cerrada.")
    return redirect(url_for("login"))


# --- Catalogo con versiones -------------------------------------------
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
    """[{app_code, app_name, versions:[{version, uploaded_at, files:[devmeta...]}]}] mas nuevas primero."""
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


def _conn(cid):
    con = db()
    c = con.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
    con.close()
    return c


# --- Rutas -------------------------------------------------------------
@app.route("/")
@login_required
def index():
    con = db()
    conns = con.execute("SELECT * FROM connections ORDER BY name").fetchall()
    con.close()
    return render_template("index.html", connections=conns, apps=list_apps())


@app.route("/connection/save", methods=["POST"])
@login_required
def conn_save():
    f = request.form
    enc = sec.encrypt(fkey(), f.get("api_key", "")) if f.get("api_key") else None
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
    return redirect(url_for("index"))


@app.route("/connection/delete/<int:cid>")
@login_required
def conn_delete(cid):
    con = db()
    con.execute("DELETE FROM connections WHERE id=?", (cid,))
    con.commit()
    con.close()
    flash("Conexion eliminada.")
    return redirect(url_for("index"))


@app.route("/connection/test/<int:cid>")
@login_required
def conn_test(cid):
    c = _conn(cid)
    try:
        key = sec.decrypt(fkey(), c["api_key_enc"])
        if not key:
            flash("Esta conexion no tiene API key; editala y guardala.")
            return redirect(url_for("index"))
        cli = OdooClient(c["url"], c["database"], c["username"], key)
        uid = cli.connect()
        flash("Conexion OK (uid %s) en %s / %s" % (uid, c["url"], c["database"]))
    except (OdooError, ValueError) as e:
        flash("Fallo de conexion: %s" % e)
    return redirect(url_for("index"))


def _extract_devs(fil):
    """Devuelve [(nombre_archivo, dict)] desde un .json o un .zip."""
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


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    fil = request.files.get("bundle")
    if not fil or not fil.filename:
        flash("Elige un archivo .json o .zip.")
        return redirect(url_for("index"))
    overwrite = request.form.get("overwrite") == "1"
    try:
        devs = _extract_devs(fil)
    except Exception as e:
        flash("No se pudo leer el archivo: %s" % e)
        return redirect(url_for("index"))
    if not devs:
        flash("El archivo no contiene codigos .json.")
        return redirect(url_for("index"))

    first = devs[0][1]
    app_code = _safe(request.form.get("app_code") or first.get("app_code") or first.get("app") or "sin_codigo")
    app_name = (request.form.get("app_name") or first.get("app") or app_code).strip()
    version = _safe(request.form.get("version") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))

    vdir = version_dir(app_code, version)
    if os.path.isdir(vdir) and not overwrite:
        flash("Ya existe la version '%s' de la app '%s'. Marca 'Sobrescribir' para reemplazarla."
              % (version, app_code))
        return redirect(url_for("index"))

    os.makedirs(vdir, exist_ok=True)
    # limpia si sobrescribe
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
    return redirect(url_for("index"))


@app.route("/version/delete", methods=["POST"])
@login_required
def version_delete():
    ac = request.form["app_code"]; v = request.form["version"]
    vdir = version_dir(ac, v)
    if os.path.isdir(vdir):
        import shutil
        shutil.rmtree(vdir)
        flash("Eliminada version '%s' de '%s'." % (v, ac))
        # si la app queda sin versiones, borra la app
        adir = app_dir(ac)
        vroot = os.path.join(adir, "versions")
        if os.path.isdir(vroot) and not [x for x in os.listdir(vroot)
                                         if os.path.isdir(os.path.join(vroot, x))]:
            shutil.rmtree(adir)
    return redirect(url_for("index"))


def _apply_group(dev, c):
    if not dev.get("group_xmlid") and c["group_xmlid"]:
        dev["group_xmlid"] = c["group_xmlid"]
    return dev


def _client_for(c):
    key = sec.decrypt(fkey(), c["api_key_enc"])
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
    return render_template("report.html", report=rep, conn=c,
                           devfile="%s · version %s" % (app_code, version), mail_to=to)


@app.route("/healthz")
def healthz():
    return "ok"


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
