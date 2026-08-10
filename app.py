"""Instalador MLR Odoo — plataforma web multiusuario para desplegar codigos Odoo por API.

Persistencia: todo en base de datos (PostgreSQL en la nube via DATABASE_URL, SQLite en local)
a traves de store.py: usuarios, conexiones, catalogo (apps/versiones/codigos), historial y la
llave de cifrado. Asi nada se pierde en redeploys.

Roles: administrador (todo) / usuario (ve catalogo, instala, ve conexiones e historial).
Secciones con menu: Panel, Catalogo, Conexiones, Historial, Usuarios.
"""
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, request, redirect, url_for, render_template, flash, session)

from odoo_client import OdooClient, OdooError
from installer import Installer, Report, send_report_email
import security as sec
import store
import brand

store.init()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())


# --- Llave de cifrado de la app (persistida en la BD) ------------------
def _load_app_key():
    env = os.environ.get("APP_FERNET_KEY")
    if env:
        return env.encode()
    k = store.get_setting("app_fernet_key")
    if not k:
        k = sec.new_app_key()
        store.set_setting("app_fernet_key", k)
    return k.encode()


APP_KEY = _load_app_key()


@app.context_processor
def inject_ctx():
    return {"brand": brand, "cur_user": session.get("auth"), "cur_role": session.get("role")}


# --- Auth --------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if store.count_users() == 0:
            return redirect(url_for("setup"))
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if store.count_users() == 0:
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
    if store.count_users() > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        if not f.get("username") or not f.get("password") or f["password"] != f.get("confirm"):
            flash("Revisa usuario y que las contrasenas coincidan.")
            return redirect(url_for("setup"))
        store.create_user(f["username"], sec.hash_password(f["password"]), "administrador")
        flash("Administrador creado. Inicia sesion.")
        return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if store.count_users() == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        f = request.form
        u = store.get_user((f.get("username") or "").strip())
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


# --- helpers -----------------------------------------------------------
def _safe(name, repl="_"):
    return re.sub(r"[^A-Za-z0-9._-]", repl, os.path.basename(name or "")).strip("._") or "x"


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


# --- Panel -------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    apps = store.list_apps()
    nver = sum(len(a["versions"]) for a in apps)
    last = store.list_logs(limit=5)
    return render_template("dashboard.html", n_apps=len(apps), n_ver=nver,
                           n_conns=store.count(store.connections), n_logs=store.count(store.install_logs),
                           n_users=store.count_users(), last=last)


# --- Catalogo ----------------------------------------------------------
@app.route("/catalogo")
@login_required
def catalogo():
    return render_template("catalogo.html", apps=store.list_apps(),
                           connections=store.list_connections())


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
    if store.version_exists(app_code, version) and not overwrite:
        flash("Ya existe la version '%s' de la app '%s'. Marca 'Sobrescribir' para reemplazarla."
              % (version, app_code))
        return redirect(url_for("catalogo"))
    files = []
    for name, data in devs:
        if not name.endswith(".json"):
            name += ".json"
        files.append((name, data))
    store.save_version(app_code, app_name, version, files, overwrite=overwrite)
    flash("App '%s' version '%s': %d codigo(s) guardado(s)%s."
          % (app_name, version, len(files), " (sobrescrita)" if overwrite else ""))
    return redirect(url_for("catalogo"))


@app.route("/version/delete", methods=["POST"])
@admin_required
def version_delete():
    store.delete_version(request.form["app_code"], request.form["version"])
    flash("Version eliminada.")
    return redirect(url_for("catalogo"))


# --- Conexiones --------------------------------------------------------
@app.route("/conexiones")
@login_required
def conexiones():
    return render_template("conexiones.html", connections=store.list_connections())


@app.route("/connection/save", methods=["POST"])
@admin_required
def conn_save():
    f = request.form
    enc = sec.encrypt(APP_KEY, f.get("api_key", "")) if f.get("api_key") else None
    store.save_connection(f.get("id"), f["name"], f["url"], f["database"], f["username"],
                          enc, f.get("group_xmlid", ""))
    flash("Conexion guardada.")
    return redirect(url_for("conexiones"))


@app.route("/connection/delete/<int:cid>")
@admin_required
def conn_delete(cid):
    store.delete_connection(cid)
    flash("Conexion eliminada.")
    return redirect(url_for("conexiones"))


@app.route("/connection/test/<int:cid>")
@admin_required
def conn_test(cid):
    c = store.get_connection(cid)
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
    c = store.get_connection(request.form["connection_id"])
    app_code = request.form["app_code"]; version = request.form["version"]
    send_mail = request.form.get("send_mail") == "1"
    rep = Report()
    to = None
    try:
        cli = _client_for(c)
        rep.info("Conectado a %s / %s como uid %s" % (c["url"], c["database"], cli.uid))
        devs = store.version_devs(app_code, version)
        devs.sort(key=lambda d: (d.get("order", 999), d.get("_file", "")))
        rep.info("Instalando app '%s' version '%s' (%d codigos, en orden)"
                 % (app_code, version, len(devs)))
        for dev in devs:
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
        ok = sum(1 for l in rep.lines if l.startswith("[OK]"))
        store.add_log(datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      session.get("auth"), c["name"] if c else "", c["database"] if c else "",
                      app_code, version, ok, len(rep.warnings), len(rep.errors), rep.html())
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
    rows = store.list_logs(q_app, q_from, q_to)
    return render_template("logs.html", rows=rows, q_app=q_app, q_from=q_from, q_to=q_to)


@app.route("/logs/<int:lid>")
@login_required
def log_view(lid):
    r = store.get_log(lid)
    if not r:
        return redirect(url_for("logs"))
    return render_template("log_view.html", r=r)


# --- Usuarios (admin) --------------------------------------------------
@app.route("/usuarios")
@admin_required
def usuarios():
    return render_template("usuarios.html", rows=store.list_users())


@app.route("/usuarios/crear", methods=["POST"])
@admin_required
def user_create():
    f = request.form
    role = f.get("role") if f.get("role") in ("administrador", "usuario") else "usuario"
    if not f.get("username") or not f.get("password"):
        flash("Usuario y contrasena requeridos.")
        return redirect(url_for("usuarios"))
    if store.get_user(f["username"].strip()):
        flash("Ese usuario ya existe.")
        return redirect(url_for("usuarios"))
    store.create_user(f["username"], sec.hash_password(f["password"]), role)
    flash("Usuario creado.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/rol", methods=["POST"])
@admin_required
def user_role():
    f = request.form
    role = f.get("role") if f.get("role") in ("administrador", "usuario") else "usuario"
    target = store.get_user_by_id(f["id"])
    if role != "administrador" and target and target["role"] == "administrador" and store.count_admins() <= 1:
        flash("No puedes quitar el ultimo administrador.")
        return redirect(url_for("usuarios"))
    store.update_user_role(f["id"], role)
    flash("Rol actualizado.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/reset", methods=["POST"])
@admin_required
def user_reset():
    f = request.form
    if not f.get("password"):
        flash("Contrasena requerida.")
        return redirect(url_for("usuarios"))
    store.update_user_pass(f["id"], sec.hash_password(f["password"]))
    flash("Contrasena actualizada.")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/borrar/<int:uid>")
@admin_required
def user_delete(uid):
    u = store.get_user_by_id(uid)
    if u and u["role"] == "administrador" and store.count_admins() <= 1:
        flash("No puedes borrar el ultimo administrador.")
        return redirect(url_for("usuarios"))
    if u and u["username"] == session.get("auth"):
        flash("No puedes borrarte a ti mismo mientras estas conectado.")
        return redirect(url_for("usuarios"))
    store.delete_user(uid)
    flash("Usuario eliminado.")
    return redirect(url_for("usuarios"))


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=5000)
