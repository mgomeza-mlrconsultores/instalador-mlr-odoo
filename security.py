"""Seguridad del Instalador MLR: hash de contrasenas de usuario, roles y cifrado de API keys.

- Usuarios: contrasena guardada como hash PBKDF2 (no reversible).
- API keys de conexiones: cifradas con una LLAVE DE APLICACION (Fernet) del servidor,
  independiente del login de cada usuario (necesario para multiusuario). La llave se toma de
  la variable de entorno APP_FERNET_KEY; si no existe, se genera y se guarda en DATA_DIR/.appkey.
"""
import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

_ITERS = 240000


# ---- Contrasenas de usuario (hash PBKDF2) ----------------------------
def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt, _ITERS)
    return "pbkdf2$%d$%s$%s" % (_ITERS, base64.b64encode(salt).decode(),
                                base64.b64encode(dk).decode())


def verify_password(password, stored):
    try:
        algo, iters, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---- Llave de aplicacion para cifrar API keys ------------------------
def load_app_key(data_dir):
    env = os.environ.get("APP_FERNET_KEY")
    if env:
        return env.encode() if isinstance(env, str) else env
    path = os.path.join(data_dir, ".appkey")
    if os.path.isfile(path):
        return open(path, "rb").read().strip()
    key = Fernet.generate_key()
    try:
        with open(path, "wb") as fh:
            fh.write(key)
    except Exception:
        pass
    return key


def encrypt(fkey, plaintext):
    return Fernet(fkey).encrypt((plaintext or "").encode()).decode()


def decrypt(fkey, token):
    if not token:
        return ""
    try:
        return Fernet(fkey).decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("No se pudo descifrar la API key (llave de aplicacion distinta).")
