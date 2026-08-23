# -*- coding: utf-8 -*-
"""
3x-ui Telegram 面板管控模块

目标：
1. 一键安装/重装 3x-ui v3.5.0
2. 安装时固定：SQLite / 自定义设置 / admin / admin / 54321 / 根路径 / IP SSL
3. 安装后强制 Xray 26.6.27
4. Telegram Inline Keyboard 管理：
   - Reality 200G
   - MTP 500G
   - 自定义 Reality（端口 / UUID / 流量）
   - 节点列表 / 清零流量 / 删除
   - 停止 / 重启 x-ui
   - 恢复 bot 记录中的默认账密
   - 卸载

依赖：
    pip install requests paramiko

本文件按 aaali-s 当前项目结构编写：
    - db.py 提供 get_connection()
    - config.py 提供 ADMIN_ID
    - ecs_business 保存 ECS 实例 IP/account/region
    - custom_servers 保存自定义 SSH 服务器 root 密码

注意：
    - 本模块通过 SSH 在服务器上执行安装与 systemctl 操作。
    - 3x-ui API 用于节点创建、节点查询、流量清零、删除、Xray 版本固定等。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shlex
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
except ImportError:  # pragma: no cover
    paramiko = None


logger = logging.getLogger(__name__)
router = Router()

# 与你现有 server.py 一致：这里只允许管理员使用。
try:
    ADMIN_ID = int(config.ADMIN_ID)
except (TypeError, ValueError):
    ADMIN_ID = 0

router.message.filter(F.from_user.id == ADMIN_ID)
router.callback_query.filter(F.from_user.id == ADMIN_ID)


# ============================================================
# 固定版本与安装配置
# ============================================================

XUI_VERSION = "v3.5.0"
XRAY_VERSION = "26.6.27"
XUI_PORT = 54321
XUI_USERNAME = "admin"
XUI_PASSWORD = "admin"
XUI_BASE_PATH = "/"
INSTALL_URL = "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"

REALITY_TOTAL = 200 * 1024 * 1024 * 1024
MTP_TOTAL = 500 * 1024 * 1024 * 1024

REALITY_DEST = "www.cloudflare.com:443"
REALITY_SNI = "www.cloudflare.com"
REALITY_FP = "chrome"
REALITY_SPIDERX = "/"

# 安装脚本的交互顺序按用户指定固定。
# 1 = SQLite
# y = 自定义设置
# admin/admin
# 54321
# 空 = Base URI /（默认）
# 2 = 申请 IP 证书
INSTALL_COMMAND = (
    "printf '%s\\n' "
    "'1' 'y' 'admin' 'admin' '54321' '' '2' "
    f"| bash <(curl -Ls {shlex.quote(INSTALL_URL)}) {shlex.quote(XUI_VERSION)}"
)


# ============================================================
# 本地 DB：记录每台机器的 3x-ui 管理信息
# ============================================================

PANEL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS xui_panels (
    instance_id TEXT PRIMARY KEY,
    ip TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT 'admin',
    password TEXT NOT NULL DEFAULT 'admin',
    port INTEGER NOT NULL DEFAULT 54321,
    base_path TEXT NOT NULL DEFAULT '/',
    scheme TEXT NOT NULL DEFAULT 'https',
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
            "instance_id",
            "ip",
            "username",
            "password",
            "port",
            "base_path",
            "scheme",
            "installed",
            "version",
            "xray_version",
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
    scheme: str = "https",
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
            (
                instance_id,
                ip,
                username,
                password,
                port,
                base_path,
                scheme,
                XUI_VERSION,
                XRAY_VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_panel_uninstalled(instance_id: str) -> None:
    _ensure_panel_table()
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE xui_panels SET installed=0, updated_at=CURRENT_TIMESTAMP WHERE instance_id=?",
            (instance_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# SSH 目标解析
# ============================================================

@dataclass
class SSHTarget:
    instance_id: str
    host: str
    username: str
    password: str
    port: int = 22


def _get_sys_config(key: str, default: str = "") -> str:
    conn = db.get_connection()
    try:
        cur = conn.execute("SELECT value FROM sys_config WHERE key = ?", (key,))
        row = cur.fetchone()
        return str(row[0]) if row and row[0] is not None else default
    except Exception:
        return default
    finally:
        conn.close()


def resolve_ssh_target(instance_id: str) -> SSHTarget:
    """兼容 custom_servers / ecs_business 两种现有服务器记录。"""
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "SELECT ip, root_password FROM custom_servers WHERE instance_id = ?",
            (instance_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            return SSHTarget(
                instance_id=instance_id,
                host=str(row[0]),
                username="root",
                password=str(row[1] or ""),
                port=22,
            )

        cur = conn.execute(
            "SELECT ip FROM ecs_business WHERE instance_id = ?",
            (instance_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            # 你的 server.py 创建 ECS 时使用的默认 root 密码。
            password = _get_sys_config("default_password", "@QS00008")
            return SSHTarget(
                instance_id=instance_id,
                host=str(row[0]),
                username="root",
                password=password,
                port=22,
            )
    finally:
        conn.close()

    # 兼容“ssh_47_83_13_85”这种回调 ID。
    if instance_id.startswith("ssh_"):
        ip = instance_id[4:].replace("_", ".")
        try:
            socket.inet_aton(ip)
        except OSError:
            raise ValueError(f"无效的 SSH IP：{ip}")
        password = _get_sys_config("default_password", "@QS00008")
        return SSHTarget(instance_id, ip, "root", password, 22)

    raise ValueError(f"找不到服务器 {instance_id} 的 SSH 信息")


def _ssh_exec_sync(target: SSHTarget, command: str, timeout: int = 300) -> tuple[int, str, str]:
    if paramiko is None:
        raise RuntimeError("缺少 paramiko，请执行：pip install paramiko")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.username,
            password=target.password,
            timeout=15,
            banner_timeout=20,
            auth_timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    finally:
        client.close()


async def ssh_exec(target: SSHTarget, command: str, timeout: int = 300) -> tuple[int, str, str]:
    return await asyncio.to_thread(_ssh_exec_sync, target, command, timeout)


# ============================================================
# 3x-ui API Client
# ============================================================

class XUIError(RuntimeError):
    pass


class XUIClient:
    def __init__(self, ip: str, username: str, password: str, port: int = XUI_PORT, base_path: str = "/"):
        self.ip = ip
        self.username = username
        self.password = password
        self.port = int(port)
        self.base_path = base_path or "/"
        if not self.base_path.startswith("/"):
            self.base_path = "/" + self.base_path
        if not self.base_path.endswith("/"):
            self.base_path += "/"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"User-Agent": "aaali-s/3x-ui-manager"})
        self.scheme = "https"
        self._csrf = ""

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.ip}:{self.port}{self.base_path.rstrip('/') or '/'}"

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url.rstrip("/") + path

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {}) or {}
        if self._csrf and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers.setdefault("X-CSRF-Token", self._csrf)
        response = self.session.request(method, self._url(path), timeout=25, headers=headers, **kwargs)
        return response

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
        # login 不依赖 CSRF。
        response = self.session.post(
            self._url("/login"),
            json={"username": self.username, "password": self.password},
            timeout=20,
        )
        data = self._json(response)

        # 新版 API 提供 CSRF token；Bearer token 可跳过，但本模块使用 session cookie。
        try:
            csrf_resp = self.session.get(self._url("/csrf-token"), timeout=15)
            csrf_data = self._json(csrf_resp)
            token = csrf_data.get("obj")
            if token:
                self._csrf = str(token)
        except Exception:
            # 旧版本没有该 endpoint 时继续使用 session。
            self._csrf = ""

        return data

    async def login(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.login_sync)

    def request_json_sync(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        return self._json(response)

    async def request_json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_json_sync, method, path, **kwargs)


async def detect_xui_client(instance_id: str) -> XUIClient:
    rec = get_panel_record(instance_id)
    if rec:
        candidate = XUIClient(
            rec["ip"], rec["username"], rec["password"], rec["port"], rec["base_path"]
        )
    else:
        target = resolve_ssh_target(instance_id)
        candidate = XUIClient(target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/")

    # 优先 HTTPS，IP SSL 失败则自动回退 HTTP。
    for scheme in ("https", "http"):
        candidate.scheme = scheme
        try:
            await candidate.login()
            if not rec:
                save_panel_record(instance_id, candidate.ip, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", scheme)
            else:
                # 若存量记录 scheme 错误，也自动修正。
                save_panel_record(
                    instance_id,
                    candidate.ip,
                    candidate.username,
                    candidate.password,
                    candidate.port,
                    candidate.base_path,
                    scheme,
                )
            return candidate
        except Exception:
            continue

    raise XUIError(f"无法登录 3x-ui：{instance_id}")


# ============================================================
# 通用工具
# ============================================================

def fmt_bytes(value: int | float) -> str:
    value = max(0, int(value or 0))
    units = ("B", "KB", "MB", "GB", "TB")
    n = float(value)
    for unit in units:
        if n < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_total(value: int | float) -> str:
    if not value or int(value) <= 0:
        return "不限"
    return fmt_bytes(int(value))


def mask_secret(value: str) -> str:
    if not value:
        return "-"
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}...{value[-4:]}"


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


def panel_main_keyboard(instance_id: str, installed: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            ("⚡️ 一键生成 Reality (200G)", f"panel_cmd:add_reality:{instance_id}"),
        ],
        [
            ("✨ 一键生成 MTP (500G)", f"panel_cmd:add_mtp:{instance_id}"),
        ],
        [
            ("🧩 生成自定义节点（端口/密钥/流量）", f"panel_cmd:custom:{instance_id}"),
        ],
        [
            ("📋 节点列表与端口管控 (统一管理)", f"panel_cmd:list:{instance_id}:0"),
        ],
        [
            ("🛠️ 安装/修复 3x-ui (v3.5.0)", f"panel_cmd:install:{instance_id}"),
        ],
        [
            ("🛑 停止面板服务", f"panel_cmd:stop:{instance_id}"),
            ("🚀 重启面板服务", f"panel_cmd:restart:{instance_id}"),
        ],
        [
            ("🔑 恢复默认账密", f"panel_cmd:reset:{instance_id}"),
            ("🗑️ 彻底卸载面板", f"panel_cmd:uninstall:{instance_id}"),
        ],
        [("🔙 返回上一级", f"srv_sel:{instance_id}")],
    ]
    return inline_kb(rows)


def make_panel_text(instance_id: str, ip: str, status: str, client: Optional[XUIClient]) -> str:
    rec = get_panel_record(instance_id)
    if rec:
        user = rec["username"]
        scheme = rec["scheme"]
        port = rec["port"]
        path = rec["base_path"].lstrip("/")
        path_text = f"/{path}" if path else "/"
    else:
        user = XUI_USERNAME
        scheme = client.scheme if client else "https"
        port = XUI_PORT
        path_text = "/"

    url = f"{scheme}://{ip}:{port}{path_text}"
    password = rec["password"] if rec else XUI_PASSWORD

    return (
        "⚡️ **全能代理面板管控中心 (3x-ui)**\n\n"
        f"🖥 操作实例：`{instance_id}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ 运行状态：{status}\n"
        "🌐 面板地址 (点击访问)：\n"
        f"[{url}]({url})\n\n"
        f"👤 账号：`{user}` | 🔑 密码：`{mask_secret(password)}`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 **核心指南**：\n"
        "• Reality：固定 200G 流量，一键生成 VLESS + REALITY。\n"
        "• MTP：固定 500G 流量，一键生成 MTProto。\n"
        "• 统一管理：节点列表可查看端口、协议、流量，并支持清零/删除。\n"
        "• 面板维护：支持安装、停止、重启、恢复默认账密、彻底卸载。"
    )


async def answer_or_edit(call: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await call.message.answer(text, parse_mode="Markdown", reply_markup=reply_markup, disable_web_page_preview=True)


async def confirm_and_update(call: CallbackQuery, text: str) -> None:
    try:
        await call.message.edit_text(text, parse_mode="Markdown")
    except TelegramBadRequest:
        await call.message.answer(text, parse_mode="Markdown")


# ============================================================
# FSM
# ============================================================

class PanelFSM(StatesGroup):
    custom_port = State()
    custom_uuid = State()
    custom_traffic = State()


# ============================================================
# 安装 / 重装
# ============================================================

async def install_xui(instance_id: str) -> tuple[bool, str]:
    target = resolve_ssh_target(instance_id)

    code, out, err = await ssh_exec(
        target,
        INSTALL_COMMAND,
        timeout=1800,
    )
    output = (out + "\n" + err).strip()

    if code != 0:
        return False, output[-3500:]

    # 等待 systemd / x-ui 起起来。
    await asyncio.sleep(5)

    # 安装完成后，用官方 API 固定 Xray 版本 26.6.27。
    try:
        client = await detect_xui_client(instance_id)
        await client.request_json("POST", f"/panel/api/server/installXray/{XRAY_VERSION}")
        await asyncio.sleep(3)
        # 再拉一次服务器状态，尽可能验证版本。
        status = await client.request_json("GET", "/panel/api/server/status")
        obj = status.get("obj") or {}
        version = obj.get("xrayVersion") or obj.get("version") or XRAY_VERSION
        save_panel_record(instance_id, target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", client.scheme)
        return True, f"安装完成\nXray：{version}\n输出：{output[-1800:]}"
    except Exception as exc:
        # 安装本身可能成功，但公网 API 尚未立即可用。
        save_panel_record(instance_id, target.host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", "https")
        return True, f"3x-ui 安装已完成，但版本校验/API 尚未稳定：{exc}\n\n{output[-1800:]}"


@router.callback_query(F.data.startswith("run_sh:panel:"))
async def show_unified_panel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await show_panel(call, instance_id)


@router.callback_query(F.data.startswith("panel_cmd:install:"))
async def cb_install(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer("正在安装 3x-ui v3.5.0，请稍候…")
    await answer_or_edit(call, f"📦 正在安装 `{instance_id}`\n\n版本：`{XUI_VERSION}`\n端口：`{XUI_PORT}`\nXray：`{XRAY_VERSION}`\n\n请不要重复点击。")

    try:
        ok, result = await install_xui(instance_id)
        if ok:
            await answer_or_edit(call, f"✅ **3x-ui 部署完成**\n\n{result}", panel_main_keyboard(instance_id, True))
        else:
            await answer_or_edit(call, f"❌ **安装失败**\n\n```text\n{result}\n```")
    except Exception as exc:
        logger.exception("x-ui install failed")
        await answer_or_edit(call, f"❌ **安装失败**\n\n`{exc}`")


# ============================================================
# 面板首页
# ============================================================

async def show_panel(call: CallbackQuery, instance_id: str):
    try:
        target = resolve_ssh_target(instance_id)
        code, out, _ = await ssh_exec(target, "systemctl is-active x-ui 2>/dev/null || true", timeout=20)
        running = out.strip() == "active"
        status = "🟢 运行中 (Running)" if running else "🔴 已停止 (Stopped)"

        client = None
        rec = get_panel_record(instance_id)
        if rec and running:
            try:
                client = await detect_xui_client(instance_id)
            except Exception:
                client = None

        text = make_panel_text(instance_id, target.host, status, client)
        await answer_or_edit(call, text, panel_main_keyboard(instance_id, bool(rec and rec.get("installed", 1))))
    except Exception as exc:
        await answer_or_edit(call, f"❌ 无法读取面板信息：`{exc}`")


@router.callback_query(F.data.startswith("panel_cmd:back:"))
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await show_panel(call, instance_id)


# ============================================================
# XUI API helpers
# ============================================================

async def ensure_client(instance_id: str) -> XUIClient:
    client = await detect_xui_client(instance_id)
    return client


async def generate_uuid(client: XUIClient) -> str:
    data = await client.request_json("GET", "/panel/api/server/getNewUUID")
    obj = data.get("obj")
    if isinstance(obj, str) and obj:
        return obj
    if isinstance(obj, dict):
        return str(obj.get("uuid") or obj.get("id"))
    # 最后兜底：自己生成 UUID v4。
    return str(uuid.uuid4())


async def generate_x25519(client: XUIClient) -> tuple[str, str]:
    data = await client.request_json("GET", "/panel/api/server/getNewX25519Cert")
    obj = data.get("obj") or {}
    if not isinstance(obj, dict):
        raise XUIError(f"getNewX25519Cert 返回异常：{data}")
    private_key = obj.get("privateKey") or obj.get("private_key")
    public_key = obj.get("publicKey") or obj.get("public_key")
    if not private_key or not public_key:
        raise XUIError(f"无法取得 X25519 keypair：{data}")
    return str(private_key), str(public_key)


async def choose_free_port(client: XUIClient, minimum: int = 10000, maximum: int = 59999) -> int:
    # API 本身会在 add 时再次拒绝冲突端口；这里随机挑选减少碰撞。
    for _ in range(30):
        port = secrets.randbelow(maximum - minimum + 1) + minimum
        if port in {22, XUI_PORT, 80, 443}:
            continue
        return port
    return 20888


# ============================================================
# Reality 200G
# ============================================================

async def create_reality_200g(instance_id: str) -> tuple[bool, str]:
    client = await ensure_client(instance_id)
    port = await choose_free_port(client)
    client_uuid = await generate_uuid(client)
    private_key, public_key = await generate_x25519(client)
    short_id = gen_short_id()
    email = f"reality_200g_{int(time.time())}"

    inbound = {
        "enable": True,
        "remark": "Reality-200G",
        "listen": "",
        "port": port,
        "protocol": "vless",
        "expiryTime": 0,
        "total": REALITY_TOTAL,
        "settings": {
            "clients": [
                {
                    "id": client_uuid,
                    "flow": "xtls-rprx-vision",
                    "email": email,
                    "limitIp": 0,
                    "totalGB": REALITY_TOTAL,
                    "expiryTime": 0,
                    "enable": True,
                }
            ],
            "decryption": "none",
            "fallbacks": [],
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": REALITY_DEST,
                "xver": 0,
                "serverNames": [REALITY_SNI],
                "privateKey": private_key,
                "shortIds": [short_id],
                "fingerprint": REALITY_FP,
                "settings": {
                    "publicKey": public_key,
                    "fingerprint": REALITY_FP,
                    "spiderX": REALITY_SPIDERX,
                },
            },
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        },
    }

    data = await client.request_json("POST", "/panel/api/inbounds/add", json=inbound)
    if not data.get("success", True):
        raise XUIError(data.get("msg", str(data)))

    link = (
        f"vless://{client_uuid}@{client.ip}:{port}"
        f"?type=tcp&security=reality"
        f"&pbk={quote(public_key)}"
        f"&fp={quote(REALITY_FP)}"
        f"&sni={quote(REALITY_SNI)}"
        f"&sid={short_id}"
        f"&spx={quote(REALITY_SPIDERX)}"
        f"&flow=xtls-rprx-vision"
        f"#{quote('Reality-200G') }"
    )

    return True, (
        "⚡️ **Reality 200G 生成成功**\n\n"
        f"端口：`{port}`\n"
        f"UUID：`{client_uuid}`\n"
        f"Short ID：`{short_id}`\n"
        f"流量：`200 GB`\n\n"
        f"公钥：`{public_key}`\n\n"
        f"节点：\n`{link}`"
    )


@router.callback_query(F.data.startswith("panel_cmd:add_reality:"))
async def cb_add_reality(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer("正在生成 Reality…")
    await answer_or_edit(call, "⚡️ 正在创建 Reality 200G…")
    try:
        _, text = await create_reality_200g(instance_id)
        await answer_or_edit(call, text, inline_kb([[("📋 返回面板", f"panel_cmd:back:{instance_id}")]]))
    except Exception as exc:
        logger.exception("add reality failed")
        await answer_or_edit(call, f"❌ Reality 创建失败：`{exc}`")


# ============================================================
# MTProto 500G
# ============================================================

async def create_mtp_500g(instance_id: str) -> tuple[bool, str]:
    client = await ensure_client(instance_id)
    port = await choose_free_port(client)
    email = f"mtp_500g_{int(time.time())}"
    secret = gen_mtproto_secret()

    inbound = {
        "enable": True,
        "remark": "MTP-500G",
        "listen": "",
        "port": port,
        "protocol": "mtproto",
        "expiryTime": 0,
        "total": MTP_TOTAL,
        "settings": {
            "fakeTlsDomain": "www.cloudflare.com",
            "clients": [
                {
                    "email": email,
                    "secret": secret,
                    "enable": True,
                    "totalGB": MTP_TOTAL,
                    "expiryTime": 0,
                    "adTag": "",
                }
            ],
        },
        "streamSettings": {},
        "sniffing": {"enabled": False},
    }

    data = await client.request_json("POST", "/panel/api/inbounds/add", json=inbound)
    if not data.get("success", True):
        raise XUIError(data.get("msg", str(data)))

    link = f"tg://proxy?server={client.ip}&port={port}&secret={secret}"
    return True, (
        "✨ **MTP 500G 生成成功**\n\n"
        f"端口：`{port}`\n"
        f"Secret：`{secret}`\n"
        f"流量：`500 GB`\n\n"
        f"Telegram：\n`{link}`"
    )


@router.callback_query(F.data.startswith("panel_cmd:add_mtp:"))
async def cb_add_mtp(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer("正在生成 MTP…")
    await answer_or_edit(call, "✨ 正在创建 MTP 500G…")
    try:
        _, text = await create_mtp_500g(instance_id)
        await answer_or_edit(call, text, inline_kb([[("📋 返回面板", f"panel_cmd:back:{instance_id}")]]))
    except Exception as exc:
        logger.exception("add mtp failed")
        await answer_or_edit(call, f"❌ MTP 创建失败：`{exc}`")


# ============================================================
# 自定义 Reality
# ============================================================

@router.callback_query(F.data.startswith("panel_cmd:custom:"))
async def cb_custom_start(call: CallbackQuery, state: FSMContext):
    instance_id = call.data.split(":", 2)[-1]
    await state.clear()
    await state.update_data(instance_id=instance_id)
    await state.set_state(PanelFSM.custom_port)
    await call.answer()
    await answer_or_edit(
        call,
        "🧩 **自定义 Reality 节点**\n\n"
        "第 1 步：请输入端口。\n"
        "例如：`443`、`8443`、`20888`\n\n"
        "端口必须是 1-65535，且不能与已有服务冲突。"
    )


@router.message(PanelFSM.custom_port)
async def custom_port_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        await message.answer("❌ 端口格式不正确，请输入 1-65535 的整数。")
        return

    await state.update_data(port=port)
    await state.set_state(PanelFSM.custom_uuid)
    await message.answer(
        "第 2 步：请输入 UUID。\n\n"
        "留空会自动生成 UUID v4。\n"
        "例如：`8a9c...`"
    )


@router.message(PanelFSM.custom_uuid)
async def custom_uuid_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw:
        try:
            parsed = uuid.UUID(raw)
            client_uuid = str(parsed)
        except ValueError:
            await message.answer("❌ UUID 格式不正确，请重新输入。")
            return
    else:
        client_uuid = str(uuid.uuid4())

    await state.update_data(client_uuid=client_uuid)
    await state.set_state(PanelFSM.custom_traffic)
    await message.answer(
        "第 3 步：请输入流量（GB）。\n\n"
        "例如：`200`\n"
        "输入 `0` 表示不限流量。"
    )


@router.message(PanelFSM.custom_traffic)
async def custom_traffic_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        traffic_gb = int(raw)
        if traffic_gb < 0 or traffic_gb > 10240:
            raise ValueError
    except ValueError:
        await message.answer("❌ 流量必须是 0-10240 的整数 GB。")
        return

    data = await state.get_data()
    instance_id = data["instance_id"]
    port = data["port"]
    client_uuid = data["client_uuid"]
    total_bytes = traffic_gb * 1024 * 1024 * 1024

    await state.clear()
    await message.answer("🧩 正在创建自定义 Reality…")

    try:
        client = await ensure_client(instance_id)
        private_key, public_key = await generate_x25519(client)
        short_id = gen_short_id()
        email = f"custom_{port}_{int(time.time())}"

        inbound = {
            "enable": True,
            "remark": f"Reality-Custom-{port}",
            "listen": "",
            "port": port,
            "protocol": "vless",
            "expiryTime": 0,
            "total": total_bytes,
            "settings": {
                "clients": [
                    {
                        "id": client_uuid,
                        "flow": "xtls-rprx-vision",
                        "email": email,
                        "limitIp": 0,
                        "totalGB": total_bytes,
                        "expiryTime": 0,
                        "enable": True,
                    }
                ],
                "decryption": "none",
                "fallbacks": [],
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": REALITY_DEST,
                    "xver": 0,
                    "serverNames": [REALITY_SNI],
                    "privateKey": private_key,
                    "shortIds": [short_id],
                    "fingerprint": REALITY_FP,
                    "settings": {
                        "publicKey": public_key,
                        "fingerprint": REALITY_FP,
                        "spiderX": REALITY_SPIDERX,
                    },
                },
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        }

        await client.request_json("POST", "/panel/api/inbounds/add", json=inbound)
        link = (
            f"vless://{client_uuid}@{client.ip}:{port}"
            f"?type=tcp&security=reality"
            f"&pbk={quote(public_key)}&fp={REALITY_FP}"
            f"&sni={quote(REALITY_SNI)}&sid={short_id}"
            f"&spx=%2F&flow=xtls-rprx-vision"
            f"#Reality-Custom-{port}"
        )
        await message.answer(
            "✅ **自定义 Reality 创建成功**\n\n"
            f"端口：`{port}`\n"
            f"UUID：`{client_uuid}`\n"
            f"流量：`{traffic_gb} GB`\n"
            f"Short ID：`{short_id}`\n"
            f"公钥：`{public_key}`\n\n"
            f"节点：\n`{link}`",
            parse_mode="Markdown",
            reply_markup=inline_kb([[("📋 返回面板", f"panel_cmd:back:{instance_id}")]]),
        )
    except Exception as exc:
        logger.exception("custom reality failed")
        await message.answer(f"❌ 创建失败：`{exc}`")


# ============================================================
# 节点列表
# ============================================================

async def render_node_list(call: CallbackQuery, instance_id: str, page: int = 0):
    client = await ensure_client(instance_id)
    data = await client.request_json("GET", "/panel/api/inbounds/list")
    items = data.get("obj") or []
    if not isinstance(items, list):
        items = []

    page_size = 5
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    current = items[page * page_size : (page + 1) * page_size]

    lines = [
        "📋 **节点列表与端口管控**",
        "━━━━━━━━━━━━━━━━━━",
        f"实例：`{instance_id}`",
        f"节点数：`{len(items)}`",
        "",
    ]
    rows: list[list[tuple[str, str]]] = []

    for item in current:
        iid = item.get("id")
        protocol = str(item.get("protocol") or "unknown").upper()
        port = item.get("port")
        remark = str(item.get("remark") or item.get("tag") or f"inbound-{iid}")
        enable = item.get("enable", True)

        used = int(item.get("up") or 0) + int(item.get("down") or 0)
        total = int(item.get("total") or 0)

        settings = item.get("settings")
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except Exception:
                settings = {}
        settings = settings or {}

        clients = settings.get("clients") or []
        if clients and total <= 0:
            totals = [int(c.get("totalGB") or 0) for c in clients if isinstance(c, dict)]
            total = max(totals) if totals else 0

        status = "🟢" if enable else "🔴"
        lines.append(
            f"{status} **#{iid} {remark}**\n"
            f"   `{protocol}` · 端口 `{port}` · 已用 `{fmt_bytes(used)}` / `{fmt_total(total)}`"
        )
        rows.append(
            [
                (f"🧹 清零 #{iid}", f"panel_node:reset:{instance_id}:{iid}:{page}"),
                (f"🗑️ 删除 #{iid}", f"panel_node:del:{instance_id}:{iid}:{page}"),
            ]
        )
        lines.append("")

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("⬅️ 上一页", f"panel_cmd:list:{instance_id}:{page-1}"))
    nav.append((f"{page+1}/{total_pages}", f"panel_cmd:list:{instance_id}:{page}"))
    if page < total_pages - 1:
        nav.append(("下一页 ➡️", f"panel_cmd:list:{instance_id}:{page+1}"))
    rows.append(nav)
    rows.append([("🔙 返回面板", f"panel_cmd:back:{instance_id}")])

    await answer_or_edit(call, "\n".join(lines), inline_kb(rows))


@router.callback_query(F.data.startswith("panel_cmd:list:"))
async def cb_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, instance_id, page = call.data.split(":", 3)
    await call.answer()
    try:
        await render_node_list(call, instance_id, int(page))
    except Exception as exc:
        await answer_or_edit(call, f"❌ 节点列表读取失败：`{exc}`")


@router.callback_query(F.data.startswith("panel_node:reset:"))
async def cb_reset_node_traffic(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, instance_id, inbound_id, page = call.data.split(":", 4)
    await call.answer("正在清零…")
    try:
        client = await ensure_client(instance_id)
        await client.request_json("POST", f"/panel/api/inbounds/resetAllClientTraffics/{inbound_id}")
        await answer_or_edit(call, f"✅ 节点 `#{inbound_id}` 已清零流量。")
        await render_node_list(call, instance_id, int(page))
    except Exception as exc:
        await answer_or_edit(call, f"❌ 清零失败：`{exc}`")


@router.callback_query(F.data.startswith("panel_node:del:"))
async def cb_delete_node(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, instance_id, inbound_id, page = call.data.split(":", 4)
    await call.answer()

    confirm_markup = inline_kb(
        [
            [
                ("⚠️ 确认删除", f"panel_node:del_confirm:{instance_id}:{inbound_id}:{page}"),
                ("取消", f"panel_cmd:list:{instance_id}:{page}"),
            ]
        ]
    )
    await answer_or_edit(
        call,
        f"⚠️ **确认删除节点 #{inbound_id}？**\n\n删除后无法通过本 Bot 恢复此入站及其客户端配置。",
        confirm_markup,
    )


@router.callback_query(F.data.startswith("panel_node:del_confirm:"))
async def cb_delete_node_confirm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, instance_id, inbound_id, page = call.data.split(":", 4)
    await call.answer("正在删除…")
    try:
        client = await ensure_client(instance_id)
        await client.request_json("POST", f"/panel/api/inbounds/del/{inbound_id}")
        await answer_or_edit(call, f"✅ 节点 `#{inbound_id}` 已删除。")
        await render_node_list(call, instance_id, int(page))
    except Exception as exc:
        await answer_or_edit(call, f"❌ 删除失败：`{exc}`")


# ============================================================
# 面板服务：stop / restart
# ============================================================

async def systemctl_xui(instance_id: str, action: str) -> tuple[bool, str]:
    if action not in {"stop", "restart", "start"}:
        raise ValueError("invalid x-ui action")
    target = resolve_ssh_target(instance_id)
    code, out, err = await ssh_exec(
        target,
        f"systemctl {action} x-ui && systemctl is-active x-ui",
        timeout=60,
    )
    return code == 0, (out + "\n" + err).strip()


@router.callback_query(F.data.startswith("panel_cmd:stop:"))
async def cb_stop(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer("正在停止…")
    try:
        ok, result = await systemctl_xui(instance_id, "stop")
        if ok:
            await answer_or_edit(call, "🛑 **x-ui 已停止。**", inline_kb([[("🔄 刷新面板", f"run_sh:panel:{instance_id}")]]))
        else:
            await answer_or_edit(call, f"❌ 停止失败：`{result[-1200:]}`")
    except Exception as exc:
        await answer_or_edit(call, f"❌ 停止失败：`{exc}`")


@router.callback_query(F.data.startswith("panel_cmd:restart:"))
async def cb_restart(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer("正在重启…")
    try:
        ok, result = await systemctl_xui(instance_id, "restart")
        if ok:
            await answer_or_edit(call, "🚀 **x-ui 重启成功。**", inline_kb([[("🔄 刷新面板", f"run_sh:panel:{instance_id}")]]))
        else:
            await answer_or_edit(call, f"❌ 重启失败：`{result[-1200:]}`")
    except Exception as exc:
        await answer_or_edit(call, f"❌ 重启失败：`{exc}`")


# ============================================================
# 恢复默认账密
# ============================================================

async def reset_credentials(instance_id: str) -> tuple[bool, str]:
    rec = get_panel_record(instance_id)
    if not rec:
        # 新机默认就是 admin/admin。
        save_panel_record(instance_id, resolve_ssh_target(instance_id).host, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", "https")
        return True, "本地没有旧记录，按新装默认账密 `admin / admin` 记录。"

    client = await detect_xui_client(instance_id)
    payload = {
        "oldUsername": rec["username"],
        "oldPassword": rec["password"],
        "newUsername": XUI_USERNAME,
        "newPassword": XUI_PASSWORD,
        "twoFactorCode": "",
    }
    await client.request_json("POST", "/panel/api/setting/updateUser", json=payload)
    save_panel_record(instance_id, client.ip, XUI_USERNAME, XUI_PASSWORD, XUI_PORT, "/", client.scheme)
    return True, "管理员账密已恢复为 `admin / admin`。"


@router.callback_query(F.data.startswith("panel_cmd:reset:"))
async def cb_reset_credentials(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer("正在恢复…")
    try:
        ok, result = await reset_credentials(instance_id)
        if ok:
            await answer_or_edit(call, f"✅ **恢复默认账密完成**\n\n{result}", inline_kb([[("🔙 返回面板", f"panel_cmd:back:{instance_id}")]]))
        else:
            await answer_or_edit(call, f"❌ 恢复失败：`{result}`")
    except Exception as exc:
        await answer_or_edit(
            call,
            "❌ **恢复默认账密失败**\n\n"
            f"原因：`{exc}`\n\n"
            "如果你曾在 Web 面板手动改过密码，而 Bot 本地没有同步最新密码，"
            "请先更新 xui_panels 表中的当前密码，或直接在 Web 面板恢复后再使用 Bot。"
        )


# ============================================================
# 卸载
# ============================================================

@router.callback_query(F.data.startswith("panel_cmd:uninstall:"))
async def cb_uninstall_prompt(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 2)[-1]
    await call.answer()
    markup = inline_kb(
        [
            [
                ("⚠️ 确认彻底卸载", f"panel_cmd:uninstall_confirm:{instance_id}"),
                ("取消", f"panel_cmd:back:{instance_id}"),
            ]
        ]
    )
    await answer_or_edit(
        call,
        "🗑️ **彻底卸载 3x-ui**\n\n"
        "此操作将停止并卸载 x-ui 服务，并删除面板程序/数据库。\n"
        "此操作不可逆，请确认。",
        markup,
    )


@router.callback_query(F.data.startswith("panel_cmd:uninstall_confirm:"))
async def cb_uninstall_confirm(call: CallbackQuery, state: FSMContext):
    await state.clear()
    instance_id = call.data.split(":", 3)[-1]
    await call.answer("正在卸载…")

    target = resolve_ssh_target(instance_id)
    # 官方 CLI uninstall 优先，失败时做兜底清理。
    command = (
        "set -o pipefail; "
        "if command -v x-ui >/dev/null 2>&1; then printf 'y\\n' | x-ui uninstall; else true; fi; "
        "systemctl disable --now x-ui 2>/dev/null || true; "
        "rm -f /etc/systemd/system/x-ui.service /lib/systemd/system/x-ui.service 2>/dev/null || true; "
        "systemctl daemon-reload 2>/dev/null || true; "
        "rm -rf /usr/local/x-ui /etc/x-ui 2>/dev/null || true; "
        "rm -f /usr/bin/x-ui 2>/dev/null || true; "
        "echo UNINSTALL_DONE"
    )

    try:
        code, out, err = await ssh_exec(target, command, timeout=180)
        mark_panel_uninstalled(instance_id)
        if code == 0 or "UNINSTALL_DONE" in out:
            await answer_or_edit(call, "✅ **3x-ui 已彻底卸载。**", inline_kb([[("🔄 重新安装", f"panel_cmd:install:{instance_id}")]]))
        else:
            await answer_or_edit(call, f"⚠️ 卸载命令已执行，但返回异常：\n```text\n{(out+err)[-1800:]}\n```")
    except Exception as exc:
        await answer_or_edit(call, f"❌ 卸载失败：`{exc}`")



# ============================================================
# 公开 helper：供其他 handler 调用
# ============================================================

def get_panel_router() -> Router:
    return router


__all__ = [
    "router",
    "PanelFSM",
    "show_panel",
    "get_panel_router",
    "create_reality_200g",
    "create_mtp_500g",
]
