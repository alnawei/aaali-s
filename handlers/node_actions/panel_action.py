# -*- coding: utf-8 -*-
"""
3x-ui Telegram 面板管控模块 (精简紧凑路由版，解决 Telegram 64 字节 callback_data 限制)
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import requests
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import db

try:
    import paramiko
except ImportError:
    paramiko = None

logger = logging.getLogger(__name__)
router = Router()

try:
    ADMIN_ID = int(config.ADMIN_ID)
except (TypeError, ValueError):
    ADMIN_ID = 0

router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)

XUI_VERSION = "v3.5.0"
XRAY_VERSION = "26.6.27"
XUI_PORT = 54321
XUI_USERNAME = "admin"
XUI_PASSWORD = "admin"
XUI_BASE_PATH = "/"

REALITY_TOTAL = 200 * 1024 * 1024 * 1024
MTP_TOTAL = 500 * 1024 * 1024 * 1024

REALITY_DEST = "www.cloudflare.com:443"
REALITY_SNI = "www.cloudflare.com"
REALITY_FP = "chrome"
REALITY_SPIDERX = "/"

INSTALL_COMMAND = (
    "printf '1\\nn\\n4\\n' | bash <(curl -Ls 'https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh') v3.5.0 && "
    "/usr/local/x-ui/x-ui setting -username admin -password admin -port 54321 -webBasePath / && "
    "x-ui restart"
)

PANEL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS xui_panels (
    instance_id TEXT PRIMARY KEY,
    ip TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT 'admin',
    password TEXT NOT NULL DEFAULT 'admin',
    port INTEGER NOT NULL DEFAULT 54321,
    base_path TEXT NOT NULL DEFAULT '/',
    scheme TEXT NOT NULL DEFAULT 'http',
    installed INTEGER NOT NULL DEFAULT 1,
    version TEXT NOT NULL DEFAULT 'v3.5.0',
    xray_version TEXT NOT NULL DEFAULT '26.6.27',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

def _ensure_panel_table() -> None:
    conn = db.get_connection()
    try:
        conn.execute(PANEL_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()

def get_panel_record(instance_id: str) -> Optional[dict[str, Any]]:
    _ensure_panel_table()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "SELECT instance_id, ip, username, password, port, base_path, scheme, installed, "
            "version, xray_version FROM xui_panels WHERE instance_id = ?",
            (instance_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = [
            "instance_id", "ip", "username", "password", "port",
            "base_path", "scheme", "installed", "version", "xray_version"
        ]
        return dict(zip(keys, row))
    finally:
        conn.close()

def save_panel_record(
    instance_id: str,
    ip: str,
    username: str = XUI_USERNAME,
    password: str = XUI_PASSWORD,
    port: int = XUI_PORT,
    base_path: str = XUI_BASE_PATH,
    scheme: str = "http",
) -> None:
    _ensure_panel_table()
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO xui_panels
              (instance_id, ip, username, password, port, base_path, scheme,
               installed, version, xray_version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(instance_id) DO UPDATE SET
              ip=excluded.ip,
              username=excluded.username,
              password=excluded.password,
              port=excluded.port,
              base_path=excluded.base_path,
              scheme=excluded.scheme,
              installed=1,
              version=excluded.version,
              xray_version=excluded.xray_version,
              updated_at=CURRENT_TIMESTAMP
            """,
            (instance_id, ip, username, password, port, base_path, scheme, XUI_VERSION, XRAY_VERSION),
        )
        conn.commit()
    finally:
        conn.close()

def mark_panel_uninstalled(instance_id: str) -> None:
    _ensure_panel_table()
    conn = db.get_connection()
    try:
        conn.execute("UPDATE xui_panels SET installed=0, updated_at=CURRENT_TIMESTAMP WHERE instance_id=?", (instance_id,))
        conn.commit()
    finally:
        conn.close()

@dataclass
class SSHTarget:
    instance_id: str
    host: str
    username: str
    password: str
    port: int = 22

def resolve_ssh_target(instance_id: str, ip_hint: Optional[str] = None) -> SSHTarget:
    default_pwd = getattr(config, 'SSH_PASSWORD', getattr(config, 'ROOT_PASSWORD', '@QS00008'))
    
    if ip_hint and "0.0.0" not in ip_hint:
        save_panel_record(instance_id, ip_hint)
        return SSHTarget(instance_id, ip_hint, "root", default_pwd, 22)

    conn = db.get_connection()
    try:
        cur = conn.execute("SELECT ip, root_password FROM custom_servers WHERE instance_id = ?", (instance_id,))
        row = cur.fetchone()
        if row and row[0]:
            pwd = str(row[1]) if row[1] else default_pwd
            return SSHTarget(instance_id, str(row[0]), "root", pwd, 22)

        cur = conn.execute("SELECT ip FROM ecs_business WHERE instance_id = ?", (instance_id,))
        row = cur.fetchone()
        if row and row[0]:
            return SSHTarget(instance_id, str(row[0]), "root", default_pwd, 22)
    finally:
        conn.close()

    rec = get_panel_record(instance_id)
    if rec and rec.get("ip"):
        return SSHTarget(instance_id, rec["ip"], "root", default_pwd, 22)

    if instance_id.startswith("ssh_"):
        ip = instance_id[4:].replace("_", ".")
        return SSHTarget(instance_id, ip, "root", default_pwd, 22)

    raise ValueError(f"无法定位实例 `{instance_id}` 的公网 IP，请返回列表重新进入！")

def _ssh_exec_sync(target: SSHTarget, command: str, timeout: int = 300) -> tuple[int, str, str]:
    if paramiko is None:
        raise RuntimeError("缺少 paramiko，请执行：pip install paramiko")

    passwords_to_try = [target.password]
    if target.password != "@QS00008":
        passwords_to_try.append("@QS00008")

    last_exc = None
    client = None

    for pwd in passwords_to_try:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=target.host,
                port=target.port,
                username=target.username,
                password=pwd,
                timeout=12,
                banner_timeout=15,
                auth_timeout=12,
                look_for_keys=False,
                allow_agent=False,
            )
            break
        except paramiko.AuthenticationException as auth_e:
            last_exc = auth_e
            if client: client.close(); client = None
            continue
        except Exception as e:
            last_exc = e
            if client: client.close(); client = None
            break

    if not client:
        raise RuntimeError(f"SSH 连接 {target.host}:22 鉴权失败：{last_exc}")

    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    finally:
        client.close()

async def ssh_exec(target: SSHTarget, command: str, timeout: int = 300) -> tuple[int, str, str]:
    return await asyncio.to_thread(_ssh_exec_sync, target, command, timeout)

class XUIError(RuntimeError):
    pass

class XUIClient:
    def __init__(self, ip: str, username: str, password: str, port: int = XUI_PORT, base_path: str = "/"):
        self.ip = ip
        self.username = username
        self.password = password
        self.port = int(port)
        self.base_path = base_path or "/"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"User-Agent": "aaali-s/3x-ui-manager"})
        self.scheme = "http"
        self._csrf = ""

    @property
    def base_url(self) -> str:
        base = f"{self.scheme}://{self.ip}:{self.port}"
        path = (self.base_path or "").strip()
        if path and path != "/":
            base += f"/{path.strip('/')}"
        return base

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url.rstrip("/") + path

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {}) or {}
        if self._csrf and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers.setdefault("X-CSRF-Token", self._csrf)
        return self.session.request(method, self._url(path), timeout=15, headers=headers, **kwargs)

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            raise XUIError(f"HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            raise XUIError(f"HTTP {response.status_code}: {data}")
        if isinstance(data, dict) and data.get("success") is False:
            raise XUIError(str(data.get("msg") or data))
        return data if isinstance(data, dict) else {"obj": data}

    def login_sync(self) -> dict[str, Any]:
        response = self.session.post(
            self._url("/login"),
            data={"username": self.username, "password": self.password},
            timeout=15,
        )
        data = self._json(response)
        try:
            csrf_resp = self.session.get(self._url("/csrf-token"), timeout=10)
            csrf_data = self._json(csrf_resp)
            token = csrf_data.get("obj")
            if token: self._csrf = str(token)
        except Exception:
            self._csrf = ""
        return data

    async def login(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.login_sync)

    def request_json_sync(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        return self._json(self._request(method, path, **kwargs))

    async def request_json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_json_sync, method, path, **kwargs)

async def detect_xui_client(instance_id: str, ip_hint: Optional[str] = None) -> XUIClient:
    rec = get_panel_record(instance_id)
    if rec:
        candidate = XUIClient(rec["ip"], rec["username"], rec["password"], rec["port"], rec["base_path"])
    else:
        target = resolve_ssh_target(instance_id, ip_hint)
        candidate = XUIClient(target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/")

    for scheme in ("http", "https"):
        candidate.scheme = scheme
        try:
            await candidate.login()
            save_panel_record(instance_id, candidate.ip, candidate.username, candidate.password, candidate.port, candidate.base_path, scheme)
            return candidate
        except Exception:
            continue

    raise XUIError("Authentication failed")

def fmt_bytes(value: int | float) -> str:
    value = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB", "TB")
    n = float(value)
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"

def fmt_total(value: int | float) -> str:
    return "不限" if not value or int(value) <= 0 else fmt_bytes(int(value))

def mask_secret(value: str) -> str:
    if not value: return "-"
    return "********" if len(value) <= 8 else f"{value[:4]}...{value[-4:]}"

def gen_short_id() -> str:
    return secrets.token_hex(4)

def gen_mtproto_secret() -> str:
    return secrets.token_hex(16)

def inline_kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )

# 🌟 精简后的按钮（全面控制在 40 字节以内，彻底解决 BUTTON_DATA_INVALID）
def panel_main_keyboard(instance_id: str, auth_ok: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if auth_ok:
        rows.extend([
            [("⚡️ 一键生成 Reality (200G)", f"p:rel:{instance_id}")],
            [("✨ 一键生成 MTP (500G)", f"p:mtp:{instance_id}")],
            [("🧩 自定义 Reality (端口/流量)", f"p:cus:{instance_id}")],
            [("📋 节点列表与端口管控", f"p:list:{instance_id}:0")],
        ])
    
    rows.extend([
        [("🛠️ 一键安装/修复 3x-ui (v3.5.0)", f"p:ins:{instance_id}")],
        [
            ("🛑 停止服务", f"p:stop:{instance_id}"),
            ("🚀 重启服务", f"p:rst:{instance_id}"),
        ],
        [
            ("🔑 恢复默认账密 (admin/admin)", f"p:pwd:{instance_id}"),
            ("🗑️ 彻底卸载面板", f"p:un:{instance_id}"),
        ],
        [("🔙 返回上一级", f"srv_sel:{instance_id}")],
    ])
    return inline_kb(rows)

def make_panel_text(instance_id: str, ip: str, status: str, client: Optional[XUIClient], auth_error: bool = False) -> str:
    rec = get_panel_record(instance_id)
    user = rec["username"] if rec else XUI_USERNAME
    scheme = client.scheme if client else "http"
    port = rec["port"] if rec else XUI_PORT
    url = f"{scheme}://{ip}:{port}/"
    password = rec["password"] if rec else XUI_PASSWORD

    auth_warning = ""
    if auth_error:
        auth_warning = (
            "\n⚠️ **提示：面板账密未同步**\n"
            "服务正在运行，但 API 登录尚未通过。\n"
            "👉 请点击下方 **【🔑 恢复默认账密】** 按钮即可一键修复！\n"
        )

    return (
        "⚡️ **全能代理面板管控中心 (3x-ui)**\n\n"
        f"🖥 操作实例：`{instance_id}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ 运行状态：{status}\n"
        f"🌐 面板地址：[{url}]({url})\n\n"
        f"👤 账号：`{user}` | 🔑 密码：`{mask_secret(password)}`\n"
        f"{auth_warning}"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 **核心功能**：\n"
        "• **Reality**：固定 200G 流量，一键生成 VLESS+REALITY。\n"
        "• **MTP**：固定 500G 流量，一键生成 Telegram 专属代理。\n"
        "• **节点管控**：查看入站端口、流量清零、一键删除节点。"
    )

async def answer_or_edit(call: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await call.message.answer(text, parse_mode="Markdown", reply_markup=reply_markup, disable_web_page_preview=True)

class PanelFSM(StatesGroup):
    custom_port = State()
    custom_uuid = State()
    custom_traffic = State()

async def install_xui(instance_id: str, ip_hint: Optional[str] = None) -> tuple[bool, str]:
    target = resolve_ssh_target(instance_id, ip_hint)
    code, out, err = await ssh_exec(target, INSTALL_COMMAND, timeout=600)
    output = (out + "\n" + err).strip()
    if code != 0: return False, output[-2000:]

    await asyncio.sleep(3)
    try:
        client = await detect_xui_client(instance_id, target.host)
        await client.request_json("POST", f"/panel/api/server/installXray/{XRAY_VERSION}")
        save_panel_record(instance_id, target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", client.scheme)
        return True, f"✅ 3x-ui {XUI_VERSION} 安装完成，账密已设为 admin/admin！"
    except Exception:
        save_panel_record(instance_id, target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", "http")
        return True, "✅ 3x-ui 安装已完成，服务已拉起！"

# 入口路由
@router.callback_query(F.data.startswith("run_sh:xui:") | F.data.startswith("run_sh:panel:"))
async def show_unified_panel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    instance_id = parts[2]
    ip = parts[3] if len(parts) > 3 else None
    if ip: save_panel_record(instance_id, ip)
    await show_panel(call, instance_id, ip)

async def show_panel(call: CallbackQuery, instance_id: str, ip_hint: Optional[str] = None):
    try:
        target = resolve_ssh_target(instance_id, ip_hint)
        code, out, _ = await ssh_exec(target, "systemctl is-active x-ui 2>/dev/null || true", timeout=15)
        running = out.strip() == "active"
        status = "🟢 运行中 (Running)" if running else "🔴 已停止 (Stopped)"

        client = None
        auth_error = False
        if running:
            try:
                client = await detect_xui_client(instance_id, target.host)
            except Exception:
                auth_error = True

        text = make_panel_text(instance_id, target.host, status, client, auth_error)
        await answer_or_edit(call, text, panel_main_keyboard(instance_id, auth_ok=(not auth_error and running)))
    except Exception as exc:
        await answer_or_edit(call, f"❌ 无法读取服务器信息：`{exc}`")

@router.callback_query(F.data.startswith("p:back:"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":")[2]
    await show_panel(call, instance_id)

@router.callback_query(F.data.startswith("p:ins:"))
async def cb_install(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":")[2]
    await call.answer("正在极速安装 3x-ui v3.5.0...")
    await answer_or_edit(call, f"📦 正在部署 3x-ui 面板 (`{instance_id}`)...\n\n固定版本：`{XUI_VERSION}`\n固定端口：`{XUI_PORT}`\n默认账密：`admin / admin`\n\n请稍候 20-30 秒。")

    try:
        ok, result = await install_xui(instance_id)
        await answer_or_edit(call, f"🎉 **操作结果**\n\n{result}", panel_main_keyboard(instance_id, True))
    except Exception as exc:
        await answer_or_edit(call, f"❌ **安装失败**\n\n`{exc}`", panel_main_keyboard(instance_id, False))

async def ensure_client(instance_id: str) -> XUIClient:
    return await detect_xui_client(instance_id)

async def generate_uuid(client: XUIClient) -> str:
    try:
        data = await client.request_json("GET", "/panel/api/server/getNewUUID")
        obj = data.get("obj")
        if isinstance(obj, str) and obj: return obj
    except Exception: pass
    return str(uuid.uuid4())

async def generate_x25519(client: XUIClient) -> tuple[str, str]:
    data = await client.request_json("GET", "/panel/api/server/getNewX25519Cert")
    obj = data.get("obj") or {}
    pri = obj.get("privateKey") or obj.get("private_key")
    pub = obj.get("publicKey") or obj.get("public_key")
    if not pri or not pub: raise XUIError("生成 X25519 失败")
    return str(pri), str(pub)

async def choose_free_port(client: XUIClient) -> int:
    for _ in range(30):
        port = secrets.randbelow(50000) + 10000
        if port not in {22, XUI_PORT, 80, 443}: return port
    return 20888

# ================= Reality 200G =================
async def create_reality_200g(instance_id: str) -> tuple[bool, str]:
    client = await ensure_client(instance_id)
    port = await choose_free_port(client)
    client_uuid = await generate_uuid(client)
    private_key, public_key = await generate_x25519(client)
    short_id = gen_short_id()
    email = f"reality_{int(time.time())}"

    inbound = {
        "enable": True, "remark": "Reality-200G", "listen": "", "port": port,
        "protocol": "vless", "expiryTime": 0, "total": REALITY_TOTAL,
        "settings": {
            "clients": [{"id": client_uuid, "flow": "xtls-rprx-vision", "email": email, "limitIp": 0, "totalGB": REALITY_TOTAL, "expiryTime": 0, "enable": True}],
            "decryption": "none", "fallbacks": []
        },
        "streamSettings": {
            "network": "tcp", "security": "reality",
            "realitySettings": {
                "show": False, "dest": REALITY_DEST, "xver": 0, "serverNames": [REALITY_SNI],
                "privateKey": private_key, "shortIds": [short_id], "fingerprint": REALITY_FP,
                "settings": {"publicKey": public_key, "fingerprint": REALITY_FP, "spiderX": REALITY_SPIDERX}
            }
        },
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}
    }

    await client.request_json("POST", "/panel/api/inbounds/add", json=inbound)
    link = f"vless://{client_uuid}@{client.ip}:{port}?type=tcp&security=reality&pbk={quote(public_key)}&fp={REALITY_FP}&sni={REALITY_SNI}&sid={short_id}&spx=%2F&flow=xtls-rprx-vision#{quote('Reality-200G')}"
    return True, (
        "⚡️ **Reality 200G 生成成功**\n\n"
        f"端口：`{port}`\nUUID：`{client_uuid}`\nShort ID：`{short_id}`\n流量：`200 GB`\n公钥：`{public_key}`\n\n节点链接：\n`{link}`"
    )

@router.callback_query(F.data.startswith("p:rel:"))
async def cb_add_reality(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":")[2]
    await call.answer("正在生成 Reality…")
    try:
        _, text = await create_reality_200g(instance_id)
        await answer_or_edit(call, text, inline_kb([[("📋 返回面板", f"p:back:{instance_id}")]]))
    except Exception as exc:
        await answer_or_edit(call, f"❌ Reality 创建失败：`{exc}`")

# ================= MTP 500G =================
async def create_mtp_500g(instance_id: str) -> tuple[bool, str]:
    client = await ensure_client(instance_id)
    port = await choose_free_port(client)
    secret = gen_mtproto_secret()
    email = f"mtp_{int(time.time())}"

    inbound = {
        "enable": True, "remark": "MTP-500G", "listen": "", "port": port,
        "protocol": "mtproto", "expiryTime": 0, "total": MTP_TOTAL,
        "settings": {"fakeTlsDomain": "www.cloudflare.com", "clients": [{"email": email, "secret": secret, "enable": True, "totalGB": MTP_TOTAL, "expiryTime": 0, "adTag": ""}]},
        "streamSettings": {}, "sniffing": {"enabled": False}
    }
    await client.request_json("POST", "/panel/api/inbounds/add", json=inbound)
    link = f"tg://proxy?server={client.ip}&port={port}&secret={secret}"
    return True, f"✨ **MTP 500G 生成成功**\n\n端口：`{port}`\nSecret：`{secret}`\n流量：`500 GB`\n\nTelegram 代理链接：\n`{link}`"

@router.callback_query(F.data.startswith("p:mtp:"))
async def cb_add_mtp(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":")[2]
    await call.answer("正在生成 MTP…")
    try:
        _, text = await create_mtp_500g(instance_id)
        await answer_or_edit(call, text, inline_kb([[("📋 返回面板", f"p:back:{instance_id}")]]))
    except Exception as exc:
        await answer_or_edit(call, f"❌ MTP 创建失败：`{exc}`")

# ================= 自定义 Reality =================
@router.callback_query(F.data.startswith("p:cus:"))
async def cb_custom_start(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":")[2]
    await state.clear()
    await state.update_data(instance_id=instance_id)
    await state.set_state(PanelFSM.custom_port)
    await call.answer()
    await answer_or_edit(call, "🧩 **自定义 Reality 节点**\n\n第 1 步：请输入端口 (1-65535)：")

@router.message(PanelFSM.custom_port)
async def custom_port_message(message: Message, state: FSMContext):
    try:
        port = int((message.text or "").strip())
        if not 1 <= port <= 65535: raise ValueError
    except ValueError:
        return await message.answer("❌ 端口必须是 1-65535 的整数。")

    await state.update_data(port=port)
    await state.set_state(PanelFSM.custom_uuid)
    await message.answer("第 2 步：请输入 UUID (留空直接发送 0 自动生成)：")

@router.message(PanelFSM.custom_uuid)
async def custom_uuid_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    client_uuid = str(uuid.uuid4()) if raw in ["", "0"] else raw
    await state.update_data(client_uuid=client_uuid)
    await state.set_state(PanelFSM.custom_traffic)
    await message.answer("第 3 步：请输入流量限制 (GB)，输入 0 为不限流量：")

@router.message(PanelFSM.custom_traffic)
async def custom_traffic_message(message: Message, state: FSMContext):
    try:
        traffic_gb = int((message.text or "").strip())
        if traffic_gb < 0: raise ValueError
    except ValueError:
        return await message.answer("❌ 流量必须是 >= 0 的整数。")

    data = await state.get_data()
    instance_id, port, client_uuid = data["instance_id"], data["port"], data["client_uuid"]
    total_bytes = traffic_gb * 1024 * 1024 * 1024
    await state.clear()
    await message.answer("🧩 正在创建节点...")

    try:
        client = await ensure_client(instance_id)
        private_key, public_key = await generate_x25519(client)
        short_id = gen_short_id()
        email = f"custom_{port}_{int(time.time())}"

        inbound = {
            "enable": True, "remark": f"Reality-Custom-{port}", "listen": "", "port": port,
            "protocol": "vless", "expiryTime": 0, "total": total_bytes,
            "settings": {
                "clients": [{"id": client_uuid, "flow": "xtls-rprx-vision", "email": email, "limitIp": 0, "totalGB": total_bytes, "expiryTime": 0, "enable": True}],
                "decryption": "none", "fallbacks": []
            },
            "streamSettings": {
                "network": "tcp", "security": "reality",
                "realitySettings": {
                    "show": False, "dest": REALITY_DEST, "xver": 0, "serverNames": [REALITY_SNI],
                    "privateKey": private_key, "shortIds": [short_id], "fingerprint": REALITY_FP,
                    "settings": {"publicKey": public_key, "fingerprint": REALITY_FP, "spiderX": REALITY_SPIDERX}
                }
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}
        }
        await client.request_json("POST", "/panel/api/inbounds/add", json=inbound)
        link = f"vless://{client_uuid}@{client.ip}:{port}?type=tcp&security=reality&pbk={quote(public_key)}&fp={REALITY_FP}&sni={REALITY_SNI}&sid={short_id}&spx=%2F&flow=xtls-rprx-vision#Reality-Custom-{port}"
        await message.answer(f"✅ **自定义 Reality 创建成功**\n\n端口：`{port}`\nUUID：`{client_uuid}`\n流量：`{traffic_gb} GB`\n\n节点链接：\n`{link}`", reply_markup=inline_kb([[("📋 返回面板", f"p:back:{instance_id}")]]), parse_mode="Markdown")
    except Exception as exc:
        await message.answer(f"❌ 创建失败：`{exc}`")

# ================= 节点管控与服务操作 =================
async def render_node_list(call: CallbackQuery, instance_id: str, page: int = 0):
    client = await ensure_client(instance_id)
    data = await client.request_json("GET", "/panel/api/inbounds/list")
    items = data.get("obj") or []
    if not isinstance(items, list): items = []

    page_size = 5
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    current = items[page * page_size : (page + 1) * page_size]

    lines = ["📋 **节点列表与端口管控**\n━━━━━━━━━━━━━━━━━━"]
    rows = []

    for item in current:
        iid, protocol, port = item.get("id"), str(item.get("protocol") or "unknown").upper(), item.get("port")
        remark, enable = str(item.get("remark") or f"inbound-{iid}"), item.get("enable", True)
        used = int(item.get("up") or 0) + int(item.get("down") or 0)
        total = int(item.get("total") or 0)
        status = "🟢" if enable else "🔴"

        lines.append(f"{status} **#{iid} {remark}**\n   `{protocol}` · 端口 `{port}` · 流量 `{fmt_bytes(used)}` / `{fmt_total(total)}`\n")
        rows.append([
            (f"🧹 清零 #{iid}", f"pn:rst:{instance_id}:{iid}:{page}"),
            (f"🗑️ 删除 #{iid}", f"pn:del:{instance_id}:{iid}:{page}"),
        ])

    nav = []
    if page > 0: nav.append(("⬅️ 上一页", f"p:list:{instance_id}:{page-1}"))
    nav.append((f"{page+1}/{total_pages}", f"p:list:{instance_id}:{page}"))
    if page < total_pages - 1: nav.append(("下一页 ➡️", f"p:list:{instance_id}:{page+1}"))
    rows.append(nav)
    rows.append([("🔙 返回面板", f"p:back:{instance_id}")])
    await answer_or_edit(call, "\n".join(lines), inline_kb(rows))

@router.callback_query(F.data.startswith("p:list:"))
async def cb_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    instance_id, page = parts[2], int(parts[3])
    await call.answer()
    try:
        await render_node_list(call, instance_id, page)
    except Exception as exc:
        await answer_or_edit(call, f"❌ 节点读取失败：`{exc}`")

@router.callback_query(F.data.startswith("pn:rst:"))
async def cb_reset_node(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    instance_id, inbound_id, page = parts[2], parts[3], int(parts[4])
    await call.answer("正在清零…")
    try:
        client = await ensure_client(instance_id)
        await client.request_json("POST", f"/panel/api/inbounds/resetAllClientTraffics/{inbound_id}")
        await render_node_list(call, instance_id, page)
    except Exception as exc:
        await answer_or_edit(call, f"❌ 清零失败：`{exc}`")

@router.callback_query(F.data.startswith("pn:del:"))
async def cb_del_node(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    instance_id, inbound_id, page = parts[2], parts[3], int(parts[4])
    await call.answer()

    confirm_markup = inline_kb([
        [
            ("⚠️ 确认删除", f"pn:del_cf:{instance_id}:{inbound_id}:{page}"),
            ("取消", f"p:list:{instance_id}:{page}")
        ]
    ])
    await answer_or_edit(call, f"⚠️ **确认删除节点 #{inbound_id}？**", confirm_markup)

@router.callback_query(F.data.startswith("pn:del_cf:"))
async def cb_del_node_confirm(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    instance_id, inbound_id, page = parts[2], parts[3], int(parts[4])
    await call.answer("正在删除…")
    try:
        client = await ensure_client(instance_id)
        await client.request_json("POST", f"/panel/api/inbounds/del/{inbound_id}")
        await render_node_list(call, instance_id, page)
    except Exception as exc:
        await answer_or_edit(call, f"❌ 删除失败：`{exc}`")

# ================= 启停 / 重置账密 / 卸载 =================
async def systemctl_xui(instance_id: str, action: str) -> tuple[bool, str]:
    target = resolve_ssh_target(instance_id)
    code, out, err = await ssh_exec(target, f"systemctl {action} x-ui && systemctl is-active x-ui", timeout=60)
    return code == 0, (out + "\n" + err).strip()

@router.callback_query(F.data.startswith("p:stop:"))
async def cb_stop(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":")[2]
    await call.answer("正在停止…")
    ok, result = await systemctl_xui(instance_id, "stop")
    await answer_or_edit(call, "🛑 **x-ui 服务已停止**" if ok else f"❌ 停止失败：`{result}`", inline_kb([[("🔄 刷新", f"p:back:{instance_id}")]]))

@router.callback_query(F.data.startswith("p:rst:"))
async def cb_restart(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":")[2]
    await call.answer("正在重启…")
    ok, result = await systemctl_xui(instance_id, "restart")
    await answer_or_edit(call, "🚀 **x-ui 重启成功**" if ok else f"❌ 重启失败：`{result}`", inline_kb([[("🔄 刷新", f"p:back:{instance_id}")]]))

async def reset_credentials(instance_id: str) -> tuple[bool, str]:
    target = resolve_ssh_target(instance_id)
    cmd = "/usr/local/x-ui/x-ui setting -username admin -password admin -port 54321 -webBasePath / && x-ui restart"
    code, out, err = await ssh_exec(target, cmd, timeout=30)
    if code == 0:
        save_panel_record(instance_id, target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", "http")
        return True, "管理员账密已强制覆写为 `admin / admin`，端口为 `54321`，根路径为 `/`。"
    return False, (out + err)[-1000:]

@router.callback_query(F.data.startswith("p:pwd:"))
async def cb_reset_credentials(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":")[2]
    await call.answer("正在强制重置账密…")
    ok, result = await reset_credentials(instance_id)
    if ok:
        await answer_or_edit(call, f"✅ **账密重置完成**\n\n{result}", inline_kb([[("🔙 返回面板", f"p:back:{instance_id}")]]))
    else:
        await answer_or_edit(call, f"❌ 恢复失败：`{result}`")

@router.callback_query(F.data.startswith("p:un:"))
async def cb_uninstall(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":")[2]
    markup = inline_kb([
        [("⚠️ 确认彻底卸载", f"p:un_cf:{instance_id}"), ("取消", f"p:back:{instance_id}")]
    ])
    await answer_or_edit(call, "🗑️ **彻底卸载 3x-ui**\n\n此操作将停止服务并删除所有入站与配置，是否继续？", markup)

@router.callback_query(F.data.startswith("p:un_cf:"))
async def cb_uninstall_confirm(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":")[2]
    await call.answer("正在卸载…")
    target = resolve_ssh_target(instance_id)
    command = "systemctl disable --now x-ui 2>/dev/null || true; rm -rf /usr/local/x-ui /etc/x-ui /usr/bin/x-ui 2>/dev/null || true; echo UNINSTALL_DONE"
    code, out, _ = await ssh_exec(target, command, timeout=60)
    mark_panel_uninstalled(instance_id)
    await answer_or_edit(call, "✅ **3x-ui 已彻底卸载**", inline_kb([[("🔄 重新安装", f"p:ins:{instance_id}")]]))

def get_panel_router() -> Router:
    return router

__all__ = ["router", "PanelFSM", "show_panel", "get_panel_router"]
