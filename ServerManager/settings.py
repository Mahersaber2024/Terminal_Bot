import json
import os
import logging
import uuid

from crypto_utils import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)


# Every one of these fields is sensitive (SSH password, private key material,
# and the passphrase protecting it) and gets encrypted at rest via crypto_utils.
_SENSITIVE_FIELDS = ("password", "private_key", "key_passphrase")


def _encrypt_servers_for_disk(servers: list) -> list:
    out = []
    for s in servers:
        s2 = dict(s)
        for field in _SENSITIVE_FIELDS:
            if s2.get(field):
                s2[field] = encrypt_value(s2[field])
        out.append(s2)
    return out


def _decrypt_servers_in_memory(servers: list) -> list:
    out = []
    for s in servers:
        s2 = dict(s)
        for field in _SENSITIVE_FIELDS:
            if s2.get(field):
                s2[field] = decrypt_value(s2[field])
        out.append(s2)
    return out


# این فایل داخل پوشه‌ی ServerManager قرار دارد؛ فایل تنظیمات هم همین‌جا ساخته می‌شود.
# این ماژول برای همه‌ی کاربران رباته - هر کاربر فقط سرورهای خودش را می‌بیند،
# بنابراین همه چیز زیر کلید user_id ذخیره می‌شود (نه یک لیست مشترک).
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_manager_settings.json")

DEFAULT_SETTINGS = {
    "users": {}   # {"<user_id>": [{"id","label","host","port","username","password"}, ...]}
}

_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading server_manager_settings.json: {e}")
            data = {}
    merged = {**DEFAULT_SETTINGS, **data}
    merged["users"] = {
        uid: _decrypt_servers_in_memory(servers)
        for uid, servers in merged.get("users", {}).items()
    }
    _cache = merged
    return merged


def _save(data):
    global _cache
    on_disk = dict(data)
    on_disk["users"] = {
        uid: _encrypt_servers_for_disk(servers)
        for uid, servers in data.get("users", {}).items()
    }
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(on_disk, f, ensure_ascii=False, indent=2)
    _cache = data


def _uid_key(user_id) -> str:
    return str(user_id)


# ====================== Servers (scoped per Telegram user) ======================

def get_servers(user_id) -> list:
    return _load().get("users", {}).get(_uid_key(user_id), [])


def get_server(user_id, server_id: str):
    for s in get_servers(user_id):
        if s["id"] == server_id:
            return s
    return None


def add_server(
    user_id,
    label: str,
    host: str,
    port: int,
    username: str,
    password: str = "",
    private_key: str = "",
    key_passphrase: str = "",
) -> dict:
    """Registers a server, authenticated with either a password OR an SSH
    private key (pass whichever one applies; leave the other at its default).
    private_key takes priority if both happen to be set."""
    data = _load()
    users = data.setdefault("users", {})
    key = _uid_key(user_id)
    servers = users.get(key, [])
    server = {
        "id": uuid.uuid4().hex[:8],
        "label": (label or host).strip(),
        "host": host.strip(),
        "port": int(port) if port else 22,
        "username": (username or "root").strip(),
        "auth_type": "key" if private_key else "password",
        # نگه‌داری plaintext در حافظه (همان چیزی که به caller برمی‌گردد)،
        # اما رمزنگاری‌شده با crypto_utils قبل از نوشتن روی دیسک - به _save() نگاه کنید.
        "password": password or "",
        "private_key": private_key or "",
        "key_passphrase": key_passphrase or "",
        # Health monitoring (see server_manager_health.py) - on by default,
        # toggleable per-server from the "🔔 Alerts" button in server detail.
        "monitor_enabled": True,
    }
    servers.append(server)
    users[key] = servers
    data["users"] = users
    _save(data)
    return server


def get_all_servers() -> list:
    """Every server across every user, as (user_id_str, server) pairs -
    used by the health-monitor background job, which sweeps every
    registered server regardless of owner (each owner is still only ever
    DMed about their own servers - see server_manager_health.py)."""
    data = _load()
    out = []
    for uid, servers in data.get("users", {}).items():
        for s in servers:
            out.append((uid, s))
    return out


def set_monitor_enabled(user_id, server_id: str, enabled: bool) -> bool:
    data = _load()
    users = data.setdefault("users", {})
    key = _uid_key(user_id)
    servers = users.get(key, [])
    for s in servers:
        if s["id"] == server_id:
            s["monitor_enabled"] = bool(enabled)
            users[key] = servers
            data["users"] = users
            _save(data)
            return True
    return False


def remove_server(user_id, server_id: str) -> bool:
    data = _load()
    users = data.setdefault("users", {})
    key = _uid_key(user_id)
    servers = users.get(key, [])
    new_servers = [s for s in servers if s["id"] != server_id]
    if len(new_servers) == len(servers):
        return False
    users[key] = new_servers
    data["users"] = users
    _save(data)
    return True
