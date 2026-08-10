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
import threading
import uuid
import zipfile
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Mexico_City")
except Exception:
    _TZ = None
from functools import wraps

from flask import (Flask, request, redirect, url_for, render_template, flash, session,
                   jsonify, Response, abort)

from odoo_client import OdooClient, OdooError
from installer import Installer, Report, Rollback, send_report_email
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


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_install_job(job_id, c, app_code, version, user, send_mail, rollback_enabled):
    """Corre la instalacion en segundo plano, transmitiendo cada linea a la BD (bitacora en vivo)."""
    seqbox = {"n": 0}

    def on_line(seq, ts, level, msg):
        seqbox["n"] = seq
        try:
            store.add_job_line(job_id, seq, ts, level, msg)
        except Exception:
            pass

    rep = Report(on_line=on_line)
    tracker = Rollback()
    cli = None
    status = "done"
    try:
        rep.info("Conectando a %s / %s ..." % (c["url"], c["database"]))
        cli = _client_for(c)
        rep.info("Conectado a %s / %s como uid %s" % (c["url"], c["database"], cli.uid))
        devs = store.version_devs(app_code, version)
        devs.sort(key=lambda d: (d.get("order", 999), d.get("_file", "")))
        rep.info("Instalando app '%s' version '%s' (%d codigos, en orden)"
                 % (app_code, version, len(devs)))
        if rollback_enabled:
            rep.info("Modo ROLLBACK activo: si hay errores, se deshara TODO lo creado en esta corrida.")
        for dev in devs:
            rep.info("=" * 40)
            rep.info(">>> CODIGO: %s  [app %s · v%s]" % (dev.get("name"), app_code, version))
            Installer(cli, dev, rep, tracker).run()
        if send_mail:
            send_report_email(cli, rep, "%s v%s" % (app_code, version))
    except (OdooError, ValueError) as e:
        rep.err("Conexion: %s" % e)
    except Exception as e:
        rep.err("Error inesperado: %s" % e)

    if rep.errors and rollback_enabled:
        if cli is not None and tracker.count():
            try:
                tracker.undo(cli, rep)
            except Exception as e:
                rep.err("Fallo durante el rollback: %s" % e)
        else:
            rep.info("No hay objetos creados que deshacer.")
        status = "rolledback"
    elif rep.errors:
        status = "error"

    ok = sum(1 for e in rep.entries if e["level"] == "OK")
    try:
        store.add_log(_now(), user, c.get("name", ""), c.get("database", ""),
                      app_code, version, ok, len(rep.warnings), len(rep.errors),
                      rep.html(), rep.text(), job_id)
    except Exception:
        pass
    try:
        store.finish_job(job_id, status, ok, len(rep.warnings), len(rep.errors), _now())
    except Exception:
        pass


@app.route("/install", methods=["POST"])
@login_required
def install():
    c = store.get_connection(request.form["connection_id"])
    if not c:
        flash("Conexion no encontrada.")
        return redirect(url_for("catalogo"))
    app_code = request.form["app_code"]; version = request.form["version"]
    send_mail = request.form.get("send_mail") == "1"
    rollback_enabled = request.form.get("rollback") == "1"
    job_id = uuid.uuid4().hex
    store.create_job(job_id, _now(), session.get("auth"), c["name"], c["database"],
                     app_code, version)
    t = threading.Thread(target=_run_install_job,
                         args=(job_id, c, app_code, version, session.get("auth"),
                               send_mail, rollback_enabled), daemon=True)
    t.start()
    return redirect(url_for("install_job", job_id=job_id))


def _fmt_local(iso):
    """Convierte un ISO UTC a hora local (America/Mexico_City) legible."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if _TZ is not None:
            dt = dt.astimezone(_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso


@app.route("/install/job/<job_id>")
@login_required
def install_job(job_id):
    job = store.get_job(job_id)
    if not job:
        abort(404)
    lines = store.job_lines_all(job_id)
    return render_template("install_job.html", job=job, lines=lines, fmt=_fmt_local)


@app.route("/install/job/<job_id>/tail")
@login_required
def install_job_tail(job_id):
    after = int(request.args.get("after", 0) or 0)
    job = store.get_job(job_id)
    if not job:
        abort(404)
    lines = store.job_lines_after(job_id, after)
    return jsonify({
        "status": job["status"], "ok": job["ok"], "warn": job["warn"], "err": job["err"],
        "lines": [{"seq": l["seq"], "ts": l["ts"], "level": l["level"], "text": l["line"]}
                  for l in lines],
    })


def _job_txt(job, lines):
    head = []
    head.append("INSTALADOR MLR - BITACORA DE INSTALACION")
    head.append("=" * 60)
    head.append("App:      %s  version %s" % (job["app_code"], job["version"]))
    head.append("Base:     %s (%s)" % (job["connection_name"], job["database"]))
    head.append("Usuario:  %s" % job["user"])
    head.append("Inicio:   %s" % _fmt_local(job["ts_start"]))
    head.append("Fin:      %s" % _fmt_local(job["ts_end"]))
    head.append("Estado:   %s" % job["status"])
    head.append("Resumen:  %s OK - %s avisos - %s errores" % (job["ok"], job["warn"], job["err"]))
    head.append("=" * 60)
    body = ["%s  [%s] %s" % (_fmt_local(l["ts"]), l["level"], l["line"]) for l in lines]
    return "\n".join(head + [""] + body) + "\n"


@app.route("/install/job/<job_id>/download")
@login_required
def install_job_download(job_id):
    job = store.get_job(job_id)
    if not job:
        abort(404)
    lines = store.job_lines_all(job_id)
    txt = _job_txt(job, lines)
    fname = "instalacion_%s_v%s_%s.txt" % (job["app_code"], job["version"], job_id[:8])
    return Response(txt, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


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


@app.route("/logs/<int:lid>/download")
@login_required
def log_download(lid):
    r = store.get_log(lid)
    if not r:
        abort(404)
    txt = r.get("report_txt") or ""
    if not txt:
        # informe antiguo sin texto: derivar del HTML de forma simple
        txt = re.sub("<[^>]+>", "", (r.get("report_html") or "")).strip()
    head = ("INSTALADOR MLR - INFORME\n%s\nApp: %s v%s\nBase: %s (%s)\nUsuario: %s\nFecha: %s\n"
            "Resumen: %s OK - %s avisos - %s errores\n%s\n\n"
            % ("=" * 60, r["app_code"], r["version"], r["connection_name"], r["database"],
               r["user"], _fmt_local(r["ts"]), r["ok"], r["warn"], r["err"], "=" * 60))
    fname = "informe_%s_v%s_%s.txt" % (r["app_code"], r["version"], lid)
    return Response(head + txt + "\n", mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


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
