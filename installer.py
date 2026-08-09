"""Motor instalador idempotente sobre la API de Odoo (SaaS-compatible).

Procesa un 'lote' (development) definido en JSON y crea/actualiza, EN ORDEN, y por
etapas (ciclos) que van obteniendo los IDs para referenciarlos entre etapas:
  cargar IDs previos -> params -> models -> accesses -> fields ->
  server_actions -> automations -> views -> rules

Persistencia de IDs: cada param resuelto y cada modelo/campo/accion creado se guarda
en Parametros del sistema de Odoo (ir.config_parameter) con clave
  mlr.installer.<lote>.<TIPO>.<CLAVE>
Al iniciar cada corrida se leen TODOS los 'mlr.installer.*' y se siembran en el contexto,
de modo que las vistas ({{ACTION:...}}) y otros lotes pueden referenciar IDs creados antes,
incluso entre re-ejecuciones.

Idempotente: antes de crear busca por nombre+modelo (o code/xmlid). Si existe y coincide,
actualiza; si no coincide el tipo de un campo, registra el error y sigue. Nunca aborta por
un error puntual: acumula todo en un informe y (opcional) lo manda por correo al final.

Marcadores admitidos dentro de code/arch/domain (listas de lineas o texto):
  {{PARAM:CLAVE}}   -> valor resuelto del param CLAVE (normalmente un id)
  {{ACTION:CLAVE}}  -> id de la accion de servidor creada con esa clave
  {{GROUP}}         -> xmlid del grupo custom del entorno
  {{GROUPS}}        -> 'base.group_system,<grupo custom>'  (para groups= de vistas)
"""


class Report:
    def __init__(self):
        self.lines = []
        self.errors = []
        self.warnings = []

    def ok(self, msg):
        self.lines.append("[OK] " + msg)

    def info(self, msg):
        self.lines.append("[..] " + msg)

    def warn(self, msg):
        self.lines.append("[AVISO] " + msg)
        self.warnings.append(msg)

    def err(self, msg):
        self.lines.append("[ERROR] " + msg)
        self.errors.append(msg)

    def text(self):
        head = "RESUMEN: %d lineas, %d avisos, %d errores\n%s\n\n" % (
            len(self.lines), len(self.warnings), len(self.errors), "-" * 50)
        return head + "\n".join(self.lines)

    def html(self):
        rows = []
        for ln in self.lines:
            if ln.startswith("[ERROR]"):
                color = "#b00020"
            elif ln.startswith("[AVISO]"):
                color = "#8a6d00"
            elif ln.startswith("[OK]"):
                color = "#24606C"
            else:
                color = "#555"
            rows.append('<div style="color:%s;font-family:monospace;font-size:12px">%s</div>'
                        % (color, _esc(ln)))
        return ("<b>%d lineas · %d avisos · %d errores</b><hr>"
                % (len(self.lines), len(self.warnings), len(self.errors))) + "".join(rows)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Installer:
    def __init__(self, client, dev, report=None):
        self.c = client
        self.dev = dev
        self.r = report or Report()
        self.ctx = {"PARAM": {}, "ACTION": {}}
        self.group_custom = dev.get("group_xmlid") or ""
        self.devkey = dev.get("key") or "lote"

    # -- persistencia de IDs en Parametros del sistema ------------------
    def _pkey(self, kind, key):
        return "mlr.installer.%s.%s.%s" % (self.devkey, kind, key)

    def _persist(self, kind, key, value):
        try:
            self.c.set_param(self._pkey(kind, key), value)
        except Exception as e:
            self.r.warn("No se pudo guardar el parametro %s: %s" % (self._pkey(kind, key), e))

    def _load_prior(self):
        """Siembra ctx con los IDs previamente guardados (de este y otros lotes)."""
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
        out = out.replace("{{GROUPS}}", "base.group_system," + self.group_custom)
        out = out.replace("{{GROUP}}", self.group_custom)
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
    def _params(self):
        for p in self.dev.get("params", []):
            key = p["key"]
            try:
                if "fixed" in p:
                    self.ctx["PARAM"][key] = p["fixed"]
                    self._persist("PARAM", key, p["fixed"])
                    self.r.ok("Param %s = %s (fijo)" % (key, p["fixed"]))
                    continue
                model = p["model"]; field = p["by"]; value = p["value"]
                rec = self.c.search(model, [(field, "=", value)], 1)
                if rec:
                    self.ctx["PARAM"][key] = rec[0]
                    self._persist("PARAM", key, rec[0])
                    self.r.ok("Param %s -> id %s (%s %s=%s)" % (key, rec[0], model, field, value))
                else:
                    self.r.err("Param %s: no se encontro %s con %s=%s (revisa el 'value' para esta base)"
                               % (key, model, field, value))
            except Exception as e:
                self.r.err("Param %s: %s" % (key, e))

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
                        self.c.create("ir.model.access", vals)
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
                                    "readonly", "tracking", "related", "required")}
                    self.c.write("ir.model.fields", [found[0]["id"]], upd)
                    self._persist("FIELD", "%s.%s" % (model, name), found[0]["id"])
                    self.r.ok("Campo actualizado %s.%s (id %s)" % (model, name, found[0]["id"]))
                else:
                    fid = self.c.create("ir.model.fields", vals)
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
                if a.get("groups"):
                    gids = self._groups_ids([self.subst(g) for g in a["groups"]])
                    if gids:
                        vals["groups_id"] = [(6, 0, gids)]
                found = self.c.search("ir.actions.server", [("name", "=", name)], 1)
                if found:
                    self.c.write("ir.actions.server", found, vals)
                    aid = found[0]
                    self.r.ok("Accion actualizada %s (id %s)" % (name, aid))
                else:
                    aid = self.c.create("ir.actions.server", vals)
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
                # 1) accion de servidor con el codigo
                act_name = name + " (accion)"
                act_vals = {"name": act_name, "model_id": mid, "state": "code",
                            "code": self.subst(au["code"])}
                afound = self.c.search("ir.actions.server", [("name", "=", act_name)], 1)
                if afound:
                    self.c.write("ir.actions.server", afound, act_vals)
                    act_id = afound[0]
                else:
                    act_id = self.c.create("ir.actions.server", act_vals)
                self._persist("AUTOM_ACTION", name, act_id)
                # 2) campos de disparo
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
                # 3) crear/actualizar base.automation con fallback de 'trigger'
                triggers = [au.get("trigger", "on_create")]
                if triggers[0] not in ("on_create", "on_create_or_write"):
                    triggers.append("on_create_or_write")  # fallback tolerante a versiones
                bfound = self.c.search("base.automation", [("name", "=", name)], 1)
                last_err = None
                for trig in triggers:
                    try:
                        v = dict(bvals); v["trigger"] = trig
                        if bfound:
                            self.c.write("base.automation", bfound, v)
                        else:
                            self.c.create("base.automation", v)
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
                    self._persist("RULE", name, rid)
                    self.r.ok("Regla creada %s" % name)
            except Exception as e:
                self.r.err("Regla %s: %s" % (name, e))


def send_report_email(client, report, dev_name):
    """Envia el informe por correo desde Odoo. Si el usuario es m.arellano/direccionjm,
    lo manda a m.gomez@mlrconsultores.com; si no, al correo del usuario ejecutor."""
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
        client.execute("mail.mail", "send", [mail_id])
        return to
    except Exception as e:
        report.err("No se pudo enviar el correo: %s" % e)
        return None
