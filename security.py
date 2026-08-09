"""Autenticacion y cifrado de API keys para el Instalador MLR (app en linea).

- Una sola contrasena maestra: sirve de login y de llave para cifrar/descifrar las API keys.
- La llave Fernet se deriva de la contrasena con PBKDF2-HMAC-SHA256 + salt aleatorio.
- En la base solo se guarda: salt, un verificador (para validar la contrasena) y los
  ciphertext de las API keys. La contrasena NUNCA se guarda.
- Requiere el paquete 'cryptography'.
"""
import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_ITERS = 240000
_VERIFIER_PLAINTEXT = b"MLR_INSTALLER_OK"


def new_salt():
    return base64.urlsafe_b64encode(os.urandom(16)).decode()


def derive_key(password, salt_b64):
    salt = base64.urlsafe_b64decode(salt_b64.encode())
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERS)
    return base64.urlsafe_b64encode(kdf.derive((password or "").encode()))


def make_verifier(fkey):
    return Fernet(fkey).encrypt(_VERIFIER_PLAINTEXT).decode()


def check_verifier(fkey, verifier):
    try:
        return Fernet(fkey).decrypt(verifier.encode()) == _VERIFIER_PLAINTEXT
    except (InvalidToken, Exception):
        return False


def encrypt(fkey, plaintext):
    return Fernet(fkey).encrypt((plaintext or "").encode()).decode()


def decrypt(fkey, token):
    if not token:
        return ""
    try:
        return Fernet(fkey).decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("No se pudo descifrar (contrasena maestra incorrecta).")
