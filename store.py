"""Capa de persistencia del Instalador MLR.

Usa PostgreSQL si existe la variable DATABASE_URL (nube, persistente); si no, SQLite local.
Guarda TODO en la base: settings, usuarios, conexiones, catalogo (apps/versiones/codigos),
historial de instalaciones y la llave de cifrado de la app. Asi nada se pierde en redeploys.
"""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import (create_engine, MetaData, Table, Column, Integer, String,
                        Text, UniqueConstraint, select, insert, update, delete, func,
                        inspect as sa_inspect, text as sa_text)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))


def _url():
    u = os.environ.get("DATABASE_URL")
    if u:
        if u.startswith("postgres://"):
            u = "postgresql+psycopg2://" + u[len("postgres://"):]
        elif u.startswith("postgresql://") and "+psycopg2" not in u:
            u = "postgresql+psycopg2://" + u[len("postgresql://"):]
        return u
    os.makedirs(DATA_DIR, exist_ok=True)
    return "sqlite:///" + os.path.join(DATA_DIR, "instalador.db")


ENGINE = create_engine(_url(), pool_pre_ping=True, future=True)
IS_PG = ENGINE.dialect.name == "postgresql"
md = MetaData()

settings = Table("settings", md,
                 Column("key", String(190), primary_key=True),
                 Column("value", Text))
users = Table("users", md,
              Column("id", Integer, primary_key=True),
              Column("username", String(190), unique=True, nullable=False),
              Column("pass_hash", Text, nullable=False),
              Column("role", String(40), nullable=False, default="usuario"),
              Column("created_at", String(40)))
connections = Table("connections", md,
                    Column("id", Integer, primary_key=True),
                    Column("name", Text, nullable=False),
                    Column("url", Text, nullable=False),
                    Column("database", Text, nullable=False),
                    Column("username", Text, nullable=False),
                    Column("api_key_enc", Text, default=""),
                    Column("group_xmlid", Text, default=""))
apps = Table("apps", md,
             Column("app_code", String(190), primary_key=True),
             Column("app_name", Text))
versions = Table("versions", md,
                 Column("id", Integer, primary_key=True),
                 Column("app_code", String(190), nullable=False),
                 Column("version", String(190), nullable=False),
                 Column("uploaded_at", String(40)),
                 UniqueConstraint("app_code", "version", name="uq_app_version"))
codes = Table("codes", md,
              Column("id", Integer, primary_key=True),
              Column("version_id", Integer, nullable=False),
              Column("filename", String(255)),
              Column("content", Text),
              Column("ordernum", Integer, default=999))
install_logs = Table("install_logs", md,
                     Column("id", Integer, primary_key=True),
                     Column("ts", String(40)),
                     Column("user", Text),
                     Column("connection_name", Text),
                     Column("database", Text),
                     Column("app_code", String(190)),
                     Column("version", String(190)),
                     Column("ok", Integer), Column("warn", Integer), Column("err", Integer),
                     Column("report_html", Text),
                     Column("report_txt", Text),
                     Column("job_id", String(64)))

# Corridas de instalacion en vivo (bitacora en tiempo real)
jobs = Table("jobs", md,
             Column("id", String(64), primary_key=True),
             Column("ts_start", String(40)),
             Column("ts_end", String(40)),
             Column("user", Text),
             Column("connection_name", Text),
             Column("database", Text),
             Column("app_code", String(190)),
             Column("version", String(190)),
             Column("status", String(20)),
             Column("ok", Integer, default=0),
             Column("warn", Integer, default=0),
             Column("err", Integer, default=0))
job_lines = Table("job_lines", md,
                  Column("id", Integer, primary_key=True),
                  Column("job_id", String(64), nullable=False),
                  Column("seq", Integer),
                  Column("ts", String(40)),
                  Column("level", String(10)),
                  Column("line", Text))


def _ensure_column(table_name, col_name, ddl_type):
    """Anade una columna si falta (migracion suave; PG y SQLite)."""
    try:
        insp = sa_inspect(ENGINE)
        cols = [c["name"] for c in insp.get_columns(table_name)]
        if col_name not in cols:
            with ENGINE.begin() as c:
                c.execute(sa_text('ALTER TABLE %s ADD COLUMN %s %s'
                                  % (table_name, col_name, ddl_type)))
    except Exception:
        pass


def init():
    md.create_all(ENGINE)
    # migraciones suaves para bases que ya existian
    _ensure_column("install_logs", "report_txt", "TEXT")
    _ensure_column("install_logs", "job_id", "VARCHAR(64)")


def backend():
    return "PostgreSQL" if IS_PG else "SQLite"


# ---- settings ----
def get_setting(key, default=None):
    with ENGINE.connect() as c:
        r = c.execute(select(settings.c.value).where(settings.c.key == key)).fetchone()
    return r[0] if r else default


def set_setting(key, value):
    with ENGINE.begin() as c:
        n = c.execute(update(settings).where(settings.c.key == key).values(value=value)).rowcount
        if not n:
            c.execute(insert(settings).values(key=key, value=value))


# ---- usuarios ----
def count_users():
    with ENGINE.connect() as c:
        return c.execute(select(func.count()).select_from(users)).scalar() or 0


def get_user(username):
    with ENGINE.connect() as c:
        r = c.execute(select(users).where(users.c.username == username)).mappings().fetchone()
    return dict(r) if r else None


def list_users():
    with ENGINE.connect() as c:
        return [dict(r) for r in c.execute(
            select(users.c.id, users.c.username, users.c.role, users.c.created_at)
            .order_by(users.c.username)).mappings()]


def create_user(username, pass_hash, role):
    with ENGINE.begin() as c:
        c.execute(insert(users).values(username=username.strip(), pass_hash=pass_hash,
                                        role=role, created_at=_now()))


def update_user_role(uid, role):
    with ENGINE.begin() as c:
        c.execute(update(users).where(users.c.id == int(uid)).values(role=role))


def update_user_pass(uid, pass_hash):
    with ENGINE.begin() as c:
        c.execute(update(users).where(users.c.id == int(uid)).values(pass_hash=pass_hash))


def delete_user(uid):
    with ENGINE.begin() as c:
        c.execute(delete(users).where(users.c.id == int(uid)))


def count_admins():
    with ENGINE.connect() as c:
        return c.execute(select(func.count()).select_from(users)
                         .where(users.c.role == "administrador")).scalar() or 0


def get_user_by_id(uid):
    with ENGINE.connect() as c:
        r = c.execute(select(users).where(users.c.id == int(uid))).mappings().fetchone()
    return dict(r) if r else None


# ---- conexiones ----
def list_connections():
    with ENGINE.connect() as c:
        return [dict(r) for r in c.execute(select(connections).order_by(connections.c.name)).mappings()]


def get_connection(cid):
    with ENGINE.connect() as c:
        r = c.execute(select(connections).where(connections.c.id == int(cid))).mappings().fetchone()
    return dict(r) if r else None


def save_connection(cid, name, url, database, username, api_key_enc, group_xmlid=""):
    with ENGINE.begin() as c:
        if cid:
            vals = dict(name=name, url=url, database=database, username=username,
                        group_xmlid=group_xmlid)
            if api_key_enc is not None:
                vals["api_key_enc"] = api_key_enc
            c.execute(update(connections).where(connections.c.id == int(cid)).values(**vals))
        else:
            c.execute(insert(connections).values(name=name, url=url, database=database,
                                                  username=username, api_key_enc=api_key_enc or "",
                                                  group_xmlid=group_xmlid))


def delete_connection(cid):
    with ENGINE.begin() as c:
        c.execute(delete(connections).where(connections.c.id == int(cid)))


# ---- catalogo ----
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_app(app_code, app_name):
    with ENGINE.begin() as c:
        n = c.execute(update(apps).where(apps.c.app_code == app_code).values(app_name=app_name)).rowcount
        if not n:
            c.execute(insert(apps).values(app_code=app_code, app_name=app_name))


def version_exists(app_code, version):
    with ENGINE.connect() as c:
        r = c.execute(select(versions.c.id).where(versions.c.app_code == app_code)
                      .where(versions.c.version == version)).fetchone()
    return r[0] if r else None


def save_version(app_code, app_name, version, devs, overwrite=False):
    """devs: lista de (filename, dict). Crea/reemplaza la version y sus codigos."""
    upsert_app(app_code, app_name)
    with ENGINE.begin() as c:
        vid = c.execute(select(versions.c.id).where(versions.c.app_code == app_code)
                        .where(versions.c.version == version)).scalar()
        if vid:
            c.execute(delete(codes).where(codes.c.version_id == vid))
            c.execute(update(versions).where(versions.c.id == vid).values(uploaded_at=_now()))
        else:
            res = c.execute(insert(versions).values(app_code=app_code, version=version,
                                                    uploaded_at=_now()))
            vid = res.inserted_primary_key[0]
        for fn, data in devs:
            c.execute(insert(codes).values(version_id=vid, filename=fn,
                                           content=json.dumps(data, ensure_ascii=False),
                                           ordernum=int(data.get("order", 999) or 999)))
    return vid


def delete_version(app_code, version):
    with ENGINE.begin() as c:
        vid = c.execute(select(versions.c.id).where(versions.c.app_code == app_code)
                        .where(versions.c.version == version)).scalar()
        if vid:
            c.execute(delete(codes).where(codes.c.version_id == vid))
            c.execute(delete(versions).where(versions.c.id == vid))
        left = c.execute(select(func.count()).select_from(versions)
                         .where(versions.c.app_code == app_code)).scalar()
        if not left:
            c.execute(delete(apps).where(apps.c.app_code == app_code))


def list_apps():
    out = []
    with ENGINE.connect() as c:
        arows = c.execute(select(apps).order_by(apps.c.app_code)).mappings().all()
        for a in arows:
            vrows = c.execute(select(versions).where(versions.c.app_code == a["app_code"])
                              .order_by(versions.c.uploaded_at.desc())).mappings().all()
            vers = []
            for v in vrows:
                crows = c.execute(select(codes).where(codes.c.version_id == v["id"])
                                  .order_by(codes.c.ordernum, codes.c.filename)).mappings().all()
                devs = []
                for cr in crows:
                    try:
                        d = json.loads(cr["content"])
                    except Exception as e:
                        d = {"name": "ERROR %s" % e, "order": 999}
                    d["_file"] = cr["filename"]
                    devs.append(d)
                vers.append({"version": v["version"], "uploaded_at": v["uploaded_at"], "devs": devs})
            out.append({"app_code": a["app_code"], "app_name": a["app_name"] or a["app_code"],
                        "versions": vers})
    return out


def version_devs(app_code, version):
    with ENGINE.connect() as c:
        vid = c.execute(select(versions.c.id).where(versions.c.app_code == app_code)
                        .where(versions.c.version == version)).scalar()
        if not vid:
            return []
        crows = c.execute(select(codes).where(codes.c.version_id == vid)
                          .order_by(codes.c.ordernum, codes.c.filename)).mappings().all()
    devs = []
    for cr in crows:
        d = json.loads(cr["content"]); d["_file"] = cr["filename"]; devs.append(d)
    return devs


# ---- historial ----
def add_log(ts, user, conn_name, database, app_code, version, ok, warn, err,
            report_html, report_txt="", job_id=""):
    with ENGINE.begin() as c:
        c.execute(insert(install_logs).values(ts=ts, user=user, connection_name=conn_name,
                  database=database, app_code=app_code, version=version, ok=ok, warn=warn,
                  err=err, report_html=report_html, report_txt=report_txt, job_id=job_id))


def list_logs(q_app="", q_from="", q_to="", limit=500):
    st = select(install_logs.c.id, install_logs.c.ts, install_logs.c.user,
                install_logs.c.connection_name, install_logs.c.database, install_logs.c.app_code,
                install_logs.c.version, install_logs.c.ok, install_logs.c.warn, install_logs.c.err,
                install_logs.c.job_id)
    if q_app:
        st = st.where(install_logs.c.app_code.like("%" + q_app + "%"))
    if q_from:
        st = st.where(install_logs.c.ts >= q_from)
    if q_to:
        st = st.where(install_logs.c.ts <= q_to + "T23:59:59")
    st = st.order_by(install_logs.c.id.desc()).limit(limit)
    with ENGINE.connect() as c:
        return [dict(r) for r in c.execute(st).mappings()]


def get_log(lid):
    with ENGINE.connect() as c:
        r = c.execute(select(install_logs).where(install_logs.c.id == int(lid))).mappings().fetchone()
    return dict(r) if r else None


def count(table):
    with ENGINE.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar() or 0


# ---- corridas en vivo (jobs) ----
def create_job(job_id, ts_start, user, connection_name, database, app_code, version):
    with ENGINE.begin() as c:
        c.execute(insert(jobs).values(id=job_id, ts_start=ts_start, user=user,
                  connection_name=connection_name, database=database, app_code=app_code,
                  version=version, status="running", ok=0, warn=0, err=0))


def finish_job(job_id, status, ok, warn, err, ts_end):
    with ENGINE.begin() as c:
        c.execute(update(jobs).where(jobs.c.id == job_id).values(
            status=status, ok=ok, warn=warn, err=err, ts_end=ts_end))


def get_job(job_id):
    with ENGINE.connect() as c:
        r = c.execute(select(jobs).where(jobs.c.id == job_id)).mappings().fetchone()
    return dict(r) if r else None


def add_job_line(job_id, seq, ts, level, line):
    with ENGINE.begin() as c:
        c.execute(insert(job_lines).values(job_id=job_id, seq=seq, ts=ts, level=level, line=line))


def job_lines_all(job_id):
    with ENGINE.connect() as c:
        return [dict(r) for r in c.execute(
            select(job_lines).where(job_lines.c.job_id == job_id)
            .order_by(job_lines.c.seq)).mappings()]


def job_lines_after(job_id, after_seq):
    with ENGINE.connect() as c:
        return [dict(r) for r in c.execute(
            select(job_lines).where(job_lines.c.job_id == job_id)
            .where(job_lines.c.seq > int(after_seq))
            .order_by(job_lines.c.seq)).mappings()]
