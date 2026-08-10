"""Motor instalador idempotente sobre la API de Odoo (SaaS-compatible).

Procesa un 'lote' (development) definido en JSON y crea/actualiza, EN ORDEN, y por
etapas (ciclos) que van obteniendo los IDs para referenciarlos entre etapas:
  cargar IDs previos -> params -> models -> accesses -> fields ->
  server_actions -> automations -> views -> rules

Persistencia de IDs: cada param resuelto y cada modelo/campo/accion creado se guarda
en Parametros del sistema de Odoo (ir.config_parameter) con clave
  mlr.installer.<lote>.<TIPO>.<CLAVE>

Idempotente: antes de crear busca por nombre+modelo (o code/xmlid). Si existe y coincide,
actualiza; si no, crea. Nunca aborta por un error puntual: acumula todo en un informe.

Rollback: si se pasa un Rollback tracker, cada objeto CREADO en la corrida (no los que ya
existian) se registra para poder deshacerlo en orden inverso si al final hay errores.

Bitacora: el Report timestampa cada linea y puede transmitirla en vivo via on_line().

Marcadores admitidos dentro de code/arch/domain (listas de lineas o texto):
  {{PARAM:CLAVE}}   -> valor resuelto del param CLAVE (normalmente un id)
  {{ACTION:CLAVE}}  -> id de la accion de servidor creada con esa clave
  {{GROUP}}         -> xmlid del grupo custom del entorno
  {{GROUPS}}        -> 'base.group_system,<grupo custom>'  (para groups= de vistas)
"""
import re
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Sentinel: indica "no incluir esta clave al crear" (p.ej. crear ubicacion sin padre).
_OMIT = object()


class Report:
    """Acumula el informe. Cada entrada lleva hora (UTC ISO) y nivel.
    on_line(seq, ts, level, msg) se invoca por cada linea para transmision en vivo."""

    def __init__(self, on_line=None):
        self.lines = []      # strings "[OK] ..." (compatibilidad)
        self.entries = []    # dicts {seq, ts, level, msg}
        self.errors = []
        self.warnings = []
        self.on_line = on_line

    def _add(self, level, msg):
        ts = _now_iso()
        seq = len(self.entries) + 1
        self.entries.append({"seq": seq, "ts": ts, "level": level, "msg": msg})
        tag = {"OK": "[OK] ", "INFO": "[..] ", "WARN": "[AVISO] ", "ERROR": "[ERROR] "}[level]
        self.lines.append(tag + msg)
        if level == "WARN":
            self.warnings.append(msg)
        elif level == "ERROR":
            self.errors.append(msg)
        if self.on_line:
            try:
                self.on_line(seq, ts, level, msg)
            except Exception:
                pass

    def ok(self, msg):
        self._add("OK", msg)

    def info(self, msg):
        self._add("INFO", msg)

    def warn(self, msg):
        self._add("WARN", msg)

    def err(self, msg):
        self._add("ERROR", msg)

    def text(self):
        head = "RESUMEN: %d lineas, %d avisos, %d errores\n%s\n\n" % (
            len(self.entries), len(self.warnings), len(self.errors), "-" * 60)
        rows = []
        for e in self.entries:
            rows.append("%s  %-7s %s" % (e["ts"], "[%s]" % e["level"], e["msg"]))
        return head + "\n".join(rows)

    def html(self):
        colors = {"ERROR": "#b00020", "WARN": "#8a6d00", "OK": "#24606C", "INFO": "#555"}
        rows = []
        for e in self.entries:
            rows.append('<div style="color:%s;font-family:monospace;font-size:12px">'
                        '<span style="color:#98a">%s</span> %s</div>'
                        % (colors.get(e["level"], "#555"), e["ts"][11:19],
                           _esc("[%s] %s" % (e["level"], e["msg"]))))
        return ("<b>%d lineas · %d avisos · %d errores</b><hr>"
                % (len(self.entries), len(self.warnings), len(self.errors))) + "".join(rows)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Rollback:
    """Registra objetos CREADOS en la corrida para poder deshacerlos.
    Solo se registra lo que se crea (no lo que ya existia ni lo que se actualiza),
    de modo que deshacer NUNCA borra registros previos del cliente."""

    # Orden de borrado (dependencias primero): reglas, automatizaciones, vistas,
    # acciones, accesos, campos, modelos, xmlid del grupo, grupo.
    ORDER = ["ir.rule", "base.automation", "ir.ui.view", "ir.actions.server",
             "ir.model.access", "ir.model.fields", "ir.model", "ir.model.data",
             "res.groups"]

    def __init__(self):
        self.items = []    # (model, id)
        self.params = []   # keys de ir.config_parameter creadas por nosotros

    def add(self, model, rid):
        if rid:
            try:
                self.items.append((model, int(rid)))
            except (TypeError, ValueError):
                pass

    def add_param(self, key):
        if key:
            self.params.append(key)

    def count(self):
        return len(self.items)

    def undo(self, client, report):
        report.info("=" * 40)
        report.warn("ROLLBACK ACTIVADO: deshaciendo %d objeto(s) creado(s) en esta corrida"
                    % len(self.items))
        rank = {m: i for i, m in enumerate(self.ORDER)}
        # Orden base por dependencias de tipo; dentro del mismo tipo, lo mas nuevo primero
        # (los campos calculados/relacionados creados despues dependen de los anteriores).
        pending = sorted(enumerate(self.items),
                         key=lambda t: (rank.get(t[1][0], 999), -t[0]))
        pending = [it for _, it in pending]
        total = len(pending)
        undone = 0
        # Varias pasadas: si A no se puede borrar porque B depende de A, en la
        # siguiente pasada ya se borro B y entonces A si se puede. Se repite hasta
        # que una pasada completa no logre borrar nada mas.
        last_err = {}
        for _pass in range(6):
            if not pending:
                break
            still = []
            progressed = False
            for model, rid in pending:
                try:
                    client.execute(model, "unlink", [rid])
                    undone += 1
                    progressed = True
                    report.ok("Deshecho %s id %s" % (model, rid))
                except Exception as e:
                    msg = str(e)
                    if "does not exist" in msg or "no existe" in msg or "MissingError" in msg:
                        undone += 1
                        progressed = True
                        report.info("Ya no existia %s id %s (ok)" % (model, rid))
                    else:
                        last_err[(model, rid)] = e
                        still.append((model, rid))
            pending = still
            if not progressed:
                break
        for model, rid in pending:
            report.err("No se pudo deshacer %s id %s: %s" % (model, rid, last_err.get((model, rid), "")))
        # borrar nuestros parametros de rastreo para dejar la base limpia
        for key in self.params:
            try:
                pid = client.search("ir.config_parameter", [("key", "=", key)], 1)
                if pid:
                    client.execute("ir.config_parameter", "unlink", pid)
            except Exception:
                pass
        report.warn("ROLLBACK terminado: %d/%d objeto(s) deshecho(s), base restaurada"
                    % (undone, len(self.items)))


class Installer:
    def __init__(self, client, dev, report=None, tracker=None):
        self.c = client
        self.dev = dev
        self.r = report or Report()
        self.tracker = tracker
        self.ctx = {"PARAM": {}, "ACTION": {}}
        self.group_xmlid = ""
        self.devkey = dev.get("key") or "lote"

    def _track(self, model, rid):
        if self.tracker is not None:
            self.tracker.add(model, rid)

    # -- persistencia de IDs en Parametros del sistema ------------------
    def _pkey(self, kind, key):
        return "mlr.installer.%s.%s.%s" % (self.devkey, kind, key)

    def _persist(self, kind, key, value):
        pk = self._pkey(kind, key)
        try:
            self.c.set_param(pk, value)
            if self.tracker is not None:
                self.tracker.add_param(pk)
        except Exception as e:
            self.r.warn("No se pudo guardar el parametro %s: %s" % (pk, e))

    def _load_prior(self):
        try:
            params = self.c.list_params("mlr.installer.%")
        except Exception as e:
            self.r.info("Sin IDs previos en Parametros del sistema (%s)" % e)
            return
        seeded = 0
        for p in params:
            parts = p["key"].split(".")
            if len(parts) < 5:
                continue
            kind, key = parts[3], parts[4]
            val = p["value"]
            if kind == "ACTION" and key not in self.ctx["ACTION"]:
                self.ctx["ACTION"][key] = val
                seeded += 1
            elif kind == "PARAM" and key not in self.ctx["PARAM"]:
                self.ctx["PARAM"][key] = val
                seeded += 1
            elif kind == "GROUP" and not self.group_xmlid:
                self.group_xmlid = val
                seeded += 1
        if seeded:
            self.r.info("Sembrados %d IDs previos desde Parametros del sistema" % seeded)

    # -- resolucion de marcadores ---------------------------------------
    def subst(self, text):
        if isinstance(text, (list, tuple)):
            text = "\n".join(text)
        if not isinstance(text, str):
            return text
        out = text
        for k, v in self.ctx["PARAM"].items():
            out = out.replace("{{PARAM:%s}}" % k, str(v))
        for k, v in self.ctx["ACTION"].items():
            out = out.replace("{{ACTION:%s}}" % k, str(v))
        grp = self.group_xmlid or ""
        out = out.replace("{{GROUPS}}", ("base.group_system," + grp) if grp else "base.group_system")
        out = out.replace("{{GROUP}}", grp)
        return out

    def _groups_ids(self, xmlids):
        ids = []
        for x in xmlids:
            rid = self.c.ref(x.strip())
            if rid:
                ids.append(rid)
            else:
                self.r.warn("Grupo no encontrado: %s" % x)
        return ids

    # -- ejecucion completa ---------------------------------------------
    def run(self):
        try:
            self._load_prior()
            self._params()
            self._groups()
            self._models()
            self._accesses()
            self._fields()
            self._server_actions()
            self._automations()
            self._views()
            self._rules()
        except Exception as e:
            self.r.err("Fallo general: %s" % e)
        return self.r

    # -- etapas ----------------------------------------------------------
    def _resolve_create_val(self, raw):
        """Resuelve un valor de plantilla de creacion.
        - dict {"_ref": "modulo.xmlid"}        -> id del registro por XML ID (independiente del idioma)
        - dict {"_search":[model, by, value]}  -> id encontrado por busqueda (o False)
        - str -> se sustituyen marcadores ({{PARAM:..}}, {{GROUP}}, etc.)
        - otro -> tal cual"""
        if isinstance(raw, dict) and "_customer_parent" in raw:
            # Toma la ubicacion estandar de Customers y devuelve SU padre, de modo que la
            # ubicacion nueva quede como HERMANA de Customers (identica salvo el nombre).
            # Si Customers no tiene padre en esta base -> _OMIT (crear sin padre).
            # Si no hay ninguna ubicacion de cliente -> False (no se puede crear).
            cust = self.c.ref("stock.stock_location_customers")
            if not cust:
                found = self.c.search("stock.location", [("usage", "=", "customer")], 1)
                cust = found[0] if found else None
            if not cust:
                return False
            rec = self.c.search_read("stock.location", [("id", "=", cust)], ["location_id"], 1)
            parent = rec[0]["location_id"] if rec else False
            if isinstance(parent, (list, tuple)) and parent:
                return parent[0]
            if isinstance(parent, int) and parent:
                return parent
            return _OMIT   # Customers existe pero sin padre -> crear sin padre
        if isinstance(raw, dict) and "_any" in raw:
            # Intenta varias estrategias en orden; usa la primera que resuelva.
            for cand in raw["_any"]:
                rv = self._resolve_create_val(cand)
                if rv is _OMIT or rv:
                    return rv
            return False
        if isinstance(raw, dict) and "_ref" in raw:
            return self.c.ref(raw["_ref"]) or False
        if isinstance(raw, dict) and "_search" in raw:
            m, by, val = raw["_search"]
            rid = self.c.search(m, [(by, "=", val)], 1)
            return rid[0] if rid else False
        if isinstance(raw, str):
            return self.subst(raw)
        return raw

    def _param_use_field(self, p, rec_id, model=None):
        """Si el param define 'use_field', lee ese campo many2one del registro y devuelve
        su id (p.ej. crear un almacen y usar su ubicacion interna lot_stock_id)."""
        uf = p.get("use_field")
        if not uf or not rec_id:
            return rec_id
        m = model or p.get("model")
        row = self.c.search_read(m, [("id", "=", rec_id)], [uf], 1)
        if row and row[0].get(uf):
            val = row[0][uf]
            return val[0] if isinstance(val, (list, tuple)) else val
        return rec_id

    def _params(self):
        for p in self.dev.get("params", []):
            key = p["key"]
            try:
                if "fixed" in p:
                    self.ctx["PARAM"][key] = p["fixed"]
                    self._persist("PARAM", key, p["fixed"])
                    self.r.ok("Param %s = %s (fijo)" % (key, p["fixed"]))
                    continue
                # Idempotencia entre lotes: si otro lote (o corrida) ya resolvio/creo este
                # registro, se reutiliza el MISMO id (evita duplicados como dos ubicaciones).
                prev = self.ctx["PARAM"].get(key)
                if prev:
                    self._persist("PARAM", key, prev)
                    self.r.info("Param %s ya resuelto antes (id %s), reutilizado" % (key, prev))
                    continue
                # Resolucion por XML ID (independiente del idioma)
                if p.get("xmlid"):
                    rid = self.c.ref(p["xmlid"])
                    if not rid:
                        self.r.err("Param %s: no se encontro el XML ID %s" % (key, p["xmlid"]))
                        continue
                    resolved = self._param_use_field(p, rid)
                    self.ctx["PARAM"][key] = resolved
                    self._persist("PARAM", key, resolved)
                    self.r.ok("Param %s -> id %s (xmlid %s)" % (key, resolved, p["xmlid"]))
                    continue
                model = p["model"]; field = p["by"]; value = p["value"]
                rec = self.c.search(model, [(field, "=", value)], 1)
                if rec:
                    resolved = self._param_use_field(p, rec[0], model)
                    self.ctx["PARAM"][key] = resolved
                    self._persist("PARAM", key, resolved)
                    self.r.ok("Param %s -> id %s (%s %s=%s)" % (key, resolved, model, field, value))
                elif p.get("create"):
                    # No existe: lo creamos con la plantilla del lote.
                    cvals = {}
                    bad_ref = None
                    for k, raw in p["create"].items():
                        rv = self._resolve_create_val(raw)
                        if rv is _OMIT:
                            continue   # no incluir esta clave (p.ej. ubicacion sin padre)
                        if rv is False and isinstance(raw, dict) and any(
                                kk in raw for kk in ("_search", "_ref", "_any", "_customer_parent")):
                            bad_ref = (raw.get("_search") or raw.get("_ref")
                                       or raw.get("_any") or "_customer_parent")
                        cvals[k] = rv
                    if bad_ref is not None:
                        self.r.err("Param %s: no se pudo crear %s, falta el registro padre (%s)"
                                   % (key, model, bad_ref))
                        continue
                    nid = self.c.create(model, cvals)
                    self._track(model, nid)
                    resolved = self._param_use_field(p, nid, model)
                    self.ctx["PARAM"][key] = resolved
                    self._persist("PARAM", key, resolved)
                    self.r.ok("Param %s: no existia -> CREADO %s id %s (usado id %s). Revisa que sea correcto para esta base."
                              % (key, model, nid, resolved))
                else:
                    self.r.err("Param %s: no se encontro %s con %s=%s (revisa el 'value' para esta base)"
                               % (key, model, field, value))
            except Exception as e:
                self.r.err("Param %s: %s" % (key, e))

    def _groups(self):
        for g in self.dev.get("groups", []):
            name = g.get("name")
            key = g.get("key", "GROUP")
            try:
                found = self.c.search_read("res.groups", [("name", "=", name)], ["id"], 1)
                if found:
                    gid = found[0]["id"]
                    self.r.info("Grupo '%s' ya existe (id %s)" % (name, gid))
                else:
                    gid = self.c.create("res.groups", {"name": name})
                    self._track("res.groups", gid)
                    self.r.ok("Grupo creado '%s' (id %s)" % (name, gid))
                md = self.c.search_read("ir.model.data",
                                        [("model", "=", "res.groups"), ("res_id", "=", gid)],
                                        ["module", "name"], 1)
                if md:
                    xmlid = md[0]["module"] + "." + md[0]["name"]
                else:
                    mdname = "res_groups_%d" % gid
                    mdid = self.c.create("ir.model.data", {"module": "x_mlr", "name": mdname,
                                                           "model": "res.groups", "res_id": gid})
                    self._track("ir.model.data", mdid)
                    xmlid = "x_mlr." + mdname
                if key == "GROUP":
                    self.group_xmlid = xmlid
                self.ctx.setdefault("GROUPX", {})[key] = xmlid
                self._persist("GROUP", key, xmlid)
                self.r.ok("Grupo '%s' -> %s" % (name, xmlid))
            except Exception as e:
                self.r.err("Grupo '%s': %s" % (name, e))

    def _models(self):
        for m in self.dev.get("models", []):
            try:
                existing = self.c.model_id(m["model"])
                if existing:
                    self._persist("MODEL", m["model"], existing)
                    self.r.info("Modelo %s ya existe (id %s)" % (m["model"], existing))
                    continue
                mid = self.c.create("ir.model", {
                    "name": m["name"], "model": m["model"],
                    "transient": bool(m.get("transient")),
                })
                self._track("ir.model", mid)
                self.c._model_id_cache[m["model"]] = mid
                self._persist("MODEL", m["model"], mid)
                self.r.ok("Modelo creado %s (id %s)" % (m["model"], mid))
            except Exception as e:
                self.r.err("Modelo %s: %s" % (m["model"], e))

    def _accesses(self):
        for ac in self.dev.get("accesses", []):
            model = ac["model"]
            try:
                mid = self.c.model_id(model)
                if not mid:
                    self.r.err("Acceso %s: modelo inexistente" % model)
                    continue
                for gx in ac.get("groups", []):
                    gx = self.subst(gx)
                    gid = self.c.ref(gx)
                    if not gid:
                        self.r.err("Acceso %s: grupo %s no encontrado" % (model, gx))
                        continue
                    aname = "access_%s_%s" % (model.replace(".", "_"), gid)
                    vals = {"name": aname, "model_id": mid, "group_id": gid,
                            "perm_read": ac.get("read", True), "perm_write": ac.get("write", True),
                            "perm_create": ac.get("create", True), "perm_unlink": ac.get("unlink", True)}
                    found = self.c.search("ir.model.access",
                                          [("model_id", "=", mid), ("group_id", "=", gid)], 1)
                    if found:
                        self.c.write("ir.model.access", found, vals)
                        self.r.ok("Acceso actualizado %s / grupo %s" % (model, gid))
                    else:
                        aid = self.c.create("ir.model.access", vals)
                        self._track("ir.model.access", aid)
                        self.r.ok("Acceso creado %s / grupo %s" % (model, gid))
            except Exception as e:
                self.r.err("Acceso %s: %s" % (model, e))

    def _fields(self):
        for f in self.dev.get("fields", []):
            model = f["model"]; name = f["name"]
            try:
                mid = self.c.model_id(model)
                if not mid:
                    self.r.err("Campo %s.%s: modelo inexistente" % (model, name))
                    continue
                found = self.c.search_read(
                    "ir.model.fields",
                    [("model", "=", model), ("name", "=", name)], ["id", "ttype"], 1)
                vals = {
                    "name": name, "model_id": mid, "model": model,
                    "field_description": f["label"], "ttype": f["ttype"],
                    "state": "manual",
                }
                for opt in ("relation", "relation_field", "related", "store",
                            "required", "readonly", "tracking", "copied"):
                    if opt in f:
                        vals[opt] = f[opt]
                # Politica de borrado para many2one. Odoo por defecto usa 'set null',
                # que es INVALIDO en un m2o requerido: hay que forzar restrict/cascade.
                if f["ttype"] == "many2one":
                    od = f.get("on_delete") or f.get("ondelete")
                    if f.get("required") and od not in ("restrict", "cascade"):
                        od = "restrict"
                    if od:
                        vals["on_delete"] = od
                if f.get("compute"):
                    vals["compute"] = self.subst(f["compute"])
                    vals["store"] = f.get("store", True)
                    if f.get("depends"):
                        vals["depends"] = f["depends"]
                if f["ttype"] == "monetary" and f.get("currency_field"):
                    vals["currency_field"] = f["currency_field"]
                if found:
                    if found[0]["ttype"] != f["ttype"]:
                        self.r.err("Campo %s.%s YA EXISTE con tipo %s (esperado %s) — no se toca"
                                   % (model, name, found[0]["ttype"], f["ttype"]))
                        continue
                    upd = {k: v for k, v in vals.items()
                           if k in ("field_description", "compute", "depends", "store",
                                    "readonly", "tracking", "related", "required", "on_delete")}
                    self.c.write("ir.model.fields", [found[0]["id"]], upd)
                    self._persist("FIELD", "%s.%s" % (model, name), found[0]["id"])
                    self.r.ok("Campo actualizado %s.%s (id %s)" % (model, name, found[0]["id"]))
                else:
                    fid = self.c.create("ir.model.fields", vals)
                    self._track("ir.model.fields", fid)
                    self._persist("FIELD", "%s.%s" % (model, name), fid)
                    self.r.ok("Campo creado %s.%s (id %s)" % (model, name, fid))
            except Exception as e:
                self.r.err("Campo %s.%s: %s" % (model, name, e))

    def _server_actions(self):
        for a in self.dev.get("server_actions", []):
            key = a["key"]; name = a["name"]
            try:
                mid = self.c.model_id(a["model"])
                if not mid:
                    self.r.err("Accion %s: modelo %s inexistente" % (name, a["model"]))
                    continue
                vals = {"name": name, "model_id": mid, "state": "code",
                        "code": self.subst(a["code"])}
                if a.get("contextual"):
                    vals["binding_model_id"] = mid
                    vals["binding_type"] = "action"
                # NOTA: en Odoo 19 ir.actions.server ya NO tiene 'groups_id'.
                # La visibilidad por grupo se controla en la vista/menu; aqui no se envia.
                found = self.c.search("ir.actions.server", [("name", "=", name)], 1)
                if found:
                    self.c.write("ir.actions.server", found, vals)
                    aid = found[0]
                    self.r.ok("Accion actualizada %s (id %s)" % (name, aid))
                else:
                    aid = self.c.create("ir.actions.server", vals)
                    self._track("ir.actions.server", aid)
                    self.r.ok("Accion creada %s (id %s)" % (name, aid))
                self.ctx["ACTION"][key] = aid
                self._persist("ACTION", key, aid)
            except Exception as e:
                self.r.err("Accion %s: %s" % (name, e))

    def _automations(self):
        for au in self.dev.get("automations", []):
            name = au["name"]
            try:
                mid = self.c.model_id(au["model"])
                if not mid:
                    self.r.err("Automatizacion %s: modelo %s inexistente" % (name, au["model"]))
                    continue
                act_name = name + " (accion)"
                act_vals = {"name": act_name, "model_id": mid, "state": "code",
                            "code": self.subst(au["code"])}
                afound = self.c.search("ir.actions.server", [("name", "=", act_name)], 1)
                if afound:
                    self.c.write("ir.actions.server", afound, act_vals)
                    act_id = afound[0]
                else:
                    act_id = self.c.create("ir.actions.server", act_vals)
                    self._track("ir.actions.server", act_id)
                self._persist("AUTOM_ACTION", name, act_id)
                trig_fields = []
                for fn in au.get("trigger_fields", []):
                    fr = self.c.search_read("ir.model.fields",
                                            [("model", "=", au["model"]), ("name", "=", fn)],
                                            ["id"], 1)
                    if fr:
                        trig_fields.append(fr[0]["id"])
                bvals = {"name": name, "model_id": mid,
                         "action_server_ids": [(6, 0, [act_id])]}
                if au.get("filter_domain"):
                    bvals["filter_domain"] = self.subst(au["filter_domain"])
                if au.get("filter_pre_domain"):
                    bvals["filter_pre_domain"] = self.subst(au["filter_pre_domain"])
                if trig_fields:
                    bvals["trigger_field_ids"] = [(6, 0, trig_fields)]
                triggers = [au.get("trigger", "on_create")]
                if triggers[0] not in ("on_create", "on_create_or_write"):
                    triggers.append("on_create_or_write")
                bfound = self.c.search("base.automation", [("name", "=", name)], 1)
                last_err = None
                for trig in triggers:
                    try:
                        v = dict(bvals); v["trigger"] = trig
                        if bfound:
                            self.c.write("base.automation", bfound, v)
                        else:
                            bid = self.c.create("base.automation", v)
                            self._track("base.automation", bid)
                        note = "" if trig == triggers[0] else " (trigger fallback: %s)" % trig
                        self.r.ok("Automatizacion %s %s%s" % (
                            "actualizada" if bfound else "creada", name, note))
                        last_err = None
                        break
                    except Exception as e2:
                        last_err = e2
                if last_err is not None:
                    raise last_err
            except Exception as e:
                self.r.err("Automatizacion %s: %s (revisa estructura base.automation en esta version)"
                           % (name, e))

    def _views(self):
        for v in self.dev.get("views", []):
            name = v["name"]
            optional = bool(v.get("optional"))
            try:
                arch = self.subst(v["arch"])
                vals = {"name": name, "model": v["model"], "type": v.get("type", "form"),
                        "arch": arch}
                inh = v.get("inherit")
                if inh:
                    cands = inh if isinstance(inh, (list, tuple)) else [inh]
                    iid = None; used = None
                    for cand in cands:
                        iid = self.c.ref(cand)
                        if iid:
                            used = cand; break
                    if not iid:
                        msg = "Vista %s: no se encontro la vista padre (%s)" % (name, ", ".join(cands))
                        (self.r.warn if optional else self.r.err)(msg)
                        continue
                    vals["inherit_id"] = iid
                    vals["mode"] = "extension"
                    if used != cands[0]:
                        self.r.info("Vista %s: padre alterno %s" % (name, used))
                found = self.c.search("ir.ui.view", [("name", "=", name)], 1)
                if found:
                    self.c.write("ir.ui.view", found, {"arch": arch})
                    self._persist("VIEW", name, found[0])
                    self.r.ok("Vista actualizada %s" % name)
                else:
                    vid = self.c.create("ir.ui.view", vals)
                    self._track("ir.ui.view", vid)
                    self._persist("VIEW", name, vid)
                    self.r.ok("Vista creada %s" % name)
            except Exception as e:
                if optional:
                    self.r.warn("Vista opcional %s no aplicada: %s" % (name, e))
                else:
                    self.r.err("Vista %s: %s" % (name, e))

    def _rules(self):
        for ru in self.dev.get("rules", []):
            name = ru["name"]
            try:
                mid = self.c.model_id(ru["model"])
                if not mid:
                    self.r.err("Regla %s: modelo %s inexistente" % (name, ru["model"]))
                    continue
                gids = self._groups_ids([self.subst(g) for g in ru.get("groups", [])])
                vals = {"name": name, "model_id": mid,
                        "domain_force": self.subst(ru["domain_force"]),
                        "groups": [(6, 0, gids)],
                        "perm_read": ru.get("perm_read", True),
                        "perm_write": ru.get("perm_write", False),
                        "perm_create": ru.get("perm_create", False),
                        "perm_unlink": ru.get("perm_unlink", False)}
                found = self.c.search("ir.rule", [("name", "=", name)], 1)
                if found:
                    self.c.write("ir.rule", found, vals)
                    self._persist("RULE", name, found[0])
                    self.r.ok("Regla actualizada %s" % name)
                else:
                    rid = self.c.create("ir.rule", vals)
                    self._track("ir.rule", rid)
                    self._persist("RULE", name, rid)
                    self.r.ok("Regla creada %s" % name)
            except Exception as e:
                self.r.err("Regla %s: %s" % (name, e))


def send_report_email(client, report, dev_name):
    """Envia el informe por correo desde Odoo."""
    try:
        me = client.search_read("res.users", [("id", "=", client.uid)], ["login", "email"], 1)
        login = (me[0].get("email") or me[0].get("login") or "").lower() if me else ""
        redirect = {"m.arellano@mlrconsultores.com", "direccionjm@mlrconsultores.com"}
        to = "m.gomez@mlrconsultores.com" if login in redirect else (login or "m.gomez@mlrconsultores.com")
        body = "<h3>Instalador MLR — %s</h3>%s" % (_esc(dev_name), report.html())
        mail_id = client.create("mail.mail", {
            "subject": "Instalador MLR Odoo — %s" % dev_name,
            "email_to": to, "body_html": body, "auto_delete": True,
        })
        try:
            client.execute("mail.mail", "send", [mail_id])
        except Exception as e2:
            # 'send' devuelve None y algunas bases SaaS no lo pueden serializar por XML-RPC.
            # El correo igual queda encolado/enviado; tratamos ese caso puntual como exito.
            if "marshal None" in str(e2) or "allow_none" in str(e2):
                pass
            else:
                raise
        report.info("Correo del informe enviado a %s" % to)
        return to
    except Exception as e:
        report.err("No se pudo enviar el correo: %s" % e)
        return None
