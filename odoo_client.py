"""Cliente ligero de la API externa de Odoo (XML-RPC). Funciona en Odoo Online/SaaS."""
import xmlrpc.client


class OdooError(Exception):
    pass


class OdooClient:
    def __init__(self, url, db, username, api_key):
        self.url = (url or "").rstrip("/")
        self.db = db
        self.username = username
        self.api_key = api_key
        self.uid = None
        self.models = None
        self._model_id_cache = {}

    def connect(self):
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        try:
            self.uid = common.authenticate(self.db, self.username, self.api_key, {})
        except Exception as e:
            raise OdooError(f"No se pudo conectar a {self.url}: {e}")
        if not self.uid:
            raise OdooError("Autenticacion fallida (revisa base de datos, usuario y API key).")
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        return self.uid

    def execute(self, model, method, *args, **kwargs):
        return self.models.execute_kw(
            self.db, self.uid, self.api_key, model, method, list(args), kwargs or {}
        )

    # Helpers ------------------------------------------------------------
    def search(self, model, domain, limit=None):
        kw = {}
        if limit:
            kw["limit"] = limit
        return self.execute(model, "search", domain, **kw)

    def search_read(self, model, domain, fields, limit=None):
        kw = {"fields": fields}
        if limit:
            kw["limit"] = limit
        return self.execute(model, "search_read", domain, **kw)

    def create(self, model, vals):
        return self.execute(model, "create", vals)

    def write(self, model, ids, vals):
        return self.execute(model, "write", ids, vals)

    def ref(self, xmlid):
        """Resuelve un XML ID (module.name) a su res_id, o None."""
        if not xmlid or "." not in xmlid:
            return None
        module, name = xmlid.split(".", 1)
        r = self.search_read(
            "ir.model.data",
            [("module", "=", module), ("name", "=", name)],
            ["res_id"], 1,
        )
        return r[0]["res_id"] if r else None

    def model_id(self, model_name):
        if model_name in self._model_id_cache:
            return self._model_id_cache[model_name]
        r = self.search_read("ir.model", [("model", "=", model_name)], ["id"], 1)
        mid = r[0]["id"] if r else None
        if mid:
            self._model_id_cache[model_name] = mid
        return mid

    # --- Parametros del sistema (ir.config_parameter) -------------------
    def set_param(self, key, value):
        return self.execute("ir.config_parameter", "set_param", key, str(value))

    def get_param(self, key, default=False):
        return self.execute("ir.config_parameter", "get_param", key, default)

    def list_params(self, like):
        return self.search_read("ir.config_parameter", [("key", "=like", like)], ["key", "value"])
