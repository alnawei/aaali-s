import base64
import asyncio
import time
import json
import random
import uuid
import datetime
import calendar
import paramiko
import sqlite3
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
# from alibabacloud_ecs20140526.client import Client as EcsClient
# from alibabacloud_ecs20140526 import models as ecs_models
# from alibabacloud_tea_openapi import models as open_api_models

import config
from db import get_active_servers

router = Router()

# ================= 🧠 全局 TTL 内存缓存池 =================
XUI_CACHE = {}
CACHE_TTL_SECONDS = 300

# ================= 🛠️ FSM 状态机 =================
class XuiRouteFSM(StatesGroup):
    wait_for_upstream = State()

# # ================= 🛠️ 底层客户端与双引擎工具 =================
# def get_ecs_client(region_id: str) -> EcsClient:
#     config_model = open_api_models.Config(
#         access_key_id=config.ALIYUN_ACCESS_KEY_ID,      
#         access_key_secret=config.ALIYUN_ACCESS_KEY_SECRET 
#     )
#     config_model.endpoint = f'ecs.{region_id}.aliyuncs.com'
#     return EcsClient(config_model)

# def encode_command(command: str) -> str:
#     return base64.b64encode(command.encode('utf-8')).decode('utf-8')

# def get_region_by_instance(user_id: int, instance_id: str) -> str:
#     try:
#         servers = get_active_servers(user_id)
#         for srv in servers:
#             if srv["instance_id"] == instance_id:
#                 return srv["region"]
#     except Exception:
#         pass
#     return "cn-hongkong"

def get_server_ip(instance_id: str) -> str:
    try:
        db_path = getattr(config, 'DB_PATH', '/srv/aali/bot_data.db')
        conn = sqlite3.connect(db_path, timeout=4.0)
        cursor = conn.cursor()
        for table in ["ecs_business", "servers", "ecs_instances", "instances"]:
            try:
                cursor.execute(f"SELECT ip FROM {table} WHERE instance_id = ? LIMIT 1", (instance_id,))
                row = cursor.fetchone()
                if row and row[0] and "0.0.0" not in str(row[0]):
                    conn.close()
                    return row[0].strip()
            except Exception:
                continue
        conn.close()
    except Exception:
        pass
    return ""


def get_server_password(instance_id: str) -> str:
    """从本地数据库获取手动添加的服务器密码 (已完美修复表名与字段盲区)"""
    # 🌟 修复 1：首选直接调用 db.py 中现成的专用方法提取密码
    import db
    try:
        pwd = db.get_custom_server_password(instance_id)
        if pwd:
            return pwd.strip()
    except Exception:
        pass

    # 🌟 修复 2：兼容性兜底逻辑，精准匹配表名与字段
    import config
    import sqlite3
    try:
        # 已经修正为你真实的数据库绝对路径 /srv/Ali/bot_data.db
        db_path = getattr(config, 'DB_PATH', '/srv/Ali/bot_data.db')
        conn = sqlite3.connect(db_path, timeout=4.0)
        cursor = conn.cursor()
        
        # 核心映射：custom_servers 用 root_password 字段，其他老表用 password
        tables_map = {
            "custom_servers": "root_password",
            "ecs_business": "password",
            "servers": "password"
        }
        
        for table, col in tables_map.items():
            try:
                cursor.execute(f"SELECT {col} FROM {table} WHERE instance_id = ? LIMIT 1", (instance_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    conn.close()
                    return row[0].strip()
            except Exception:
                continue
        conn.close()
    except Exception as e:
        print(f"获取密码兜底逻辑报错: {e}")
        
    return ""
# async def fetch_command_output_async(client: EcsClient, region_id: str, invoke_id: str) -> str:
#     req = ecs_models.DescribeInvocationResultsRequest(region_id=region_id, invoke_id=invoke_id)
#     # 🌟 修复：增加轮询次数到 15 次 (30秒)，给服务器重启面板留足时间
#     for _ in range(15):
#         await asyncio.sleep(2.0)
#         try:
#             resp = await asyncio.to_thread(client.describe_invocation_results, req)
#             if resp.body.invocation and resp.body.invocation.invocation_results.invocation_result:
#                 res = resp.body.invocation.invocation_results.invocation_result[0]
#                 if res.invocation_state in ["Success", "Failed", "Finished"]:
#                     output_b64 = res.output or ""
#                     return base64.b64decode(output_b64).decode('utf-8', errors='ignore').strip() if output_b64 else "SUCCESS"
#         except Exception:
#             continue
#     return "⏳底层执行超时_TIMEOUT"

async def execute_xui_hybrid(instance_id: str, user_id: int, shell_script: str) -> str:
    """极速单轨 SSH 执行器 (智能区分密码来源)"""
    
    # 1. 统一获取机器的公网 IP
    ip = get_server_ip(instance_id)
    if not ip: 
        raise Exception("智能路由失败：无法在本地数据库中找到该实例的公网 IP。")
        
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # ================== 🌟 核心修改区域 ==================
        # 2. 智能判断密码来源
        if instance_id.startswith("i-"):
            # 阿里云 API 自动开出的机器：使用预设的固定密码
            pwd = getattr(config, 'SSH_PASSWORD', getattr(config, 'ROOT_PASSWORD', '@QS00008'))
        else:
            # 手动添加的机器：去数据库查它自己的专属密码
            pwd = get_server_password(instance_id)
            if not pwd:
                raise Exception("无法在数据库中读取到该手动实例的 SSH 密码，请检查服务器配置。")
        # ===================================================
        
        # 3. 闪电建立 SSH 握手 (连接超时限制在 8 秒内)
        await asyncio.to_thread(client.connect, hostname=ip, port=22, username="root", password=pwd, timeout=8.0)
        
        # 4. 执行指令 (给予充足的 180 秒执行时间)
        stdin, stdout, stderr = await asyncio.to_thread(client.exec_command, shell_script, timeout=180.0)
        
        # 🌟 物理通道防卡死：强制让 SSH 的数据流读取等待也放宽到 180 秒，保护下载安装等慢动作
        stdout.channel.settimeout(180.0)
        stderr.channel.settimeout(180.0)
        
        # 5. 瞬间拉取执行结果
        out_str = (await asyncio.to_thread(stdout.read)).decode('utf-8').strip()
        err_str = (await asyncio.to_thread(stderr.read)).decode('utf-8').strip()
        client.close()
        
        # 合并标准输出与错误输出，若为空则返回 SUCCESS
        return (out_str + "\n" + err_str).strip() or "SUCCESS"
        
    except Exception as e:
        raise Exception(f"SSH 极速通道执行失败: {str(e)}")


# ================= 🎨 动态 UI 键盘渲染 =================
def build_xui_keyboard(instance_id: str, is_installed: bool = True) -> InlineKeyboardMarkup:
    # 两段式 UI：荒地模式
    if not is_installed:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 一键全新部署 3x-ui (默认端口54321)", callback_data=f"xui_cmd:install:{instance_id}")],
            [InlineKeyboardButton(text="🔄 重新探测面板状态", callback_data=f"run_sh:xui:{instance_id}")],
            [InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"srv_sel:{instance_id}")]
        ])
    
    # 两段式 UI：满血模式
    is_running = True
    if instance_id in XUI_CACHE and time.time() < XUI_CACHE[instance_id]["expire"]:
        is_running = (XUI_CACHE[instance_id].get("panel_status") == "running")

    toggle_btn = InlineKeyboardButton(text="🛑 停止面板服务", callback_data=f"xui_cmd:stop:{instance_id}") if is_running else InlineKeyboardButton(text="🟢 启动面板服务", callback_data=f"xui_cmd:start:{instance_id}")

    builder = [
        [InlineKeyboardButton(text="⚡️ 一键生成 VLESS-Reality (直连 / 200G)", callback_data=f"xui_cmd:add_reality_quick:{instance_id}")],
        [InlineKeyboardButton(text="📋 节点列表与端口管理 (改中转/重置流量)", callback_data=f"xui_cmd:port_list:{instance_id}")],
        [toggle_btn, InlineKeyboardButton(text="🚀 重启面板服务", callback_data=f"xui_cmd:restart:{instance_id}")],
        [InlineKeyboardButton(text="🔑 恢复默认账密", callback_data=f"xui_cmd:reset_pass:{instance_id}"), InlineKeyboardButton(text="🗑️ 彻底卸载 X-UI", callback_data=f"xui_cmd:uninstall:{instance_id}")],
        [InlineKeyboardButton(text="🔙 返回上一级", callback_data=f"srv_sel:{instance_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=builder)


# ================= 🚀 1. 渲染主面板 (带状态嗅探) =================
@router.callback_query(F.data.startswith("run_sh:xui:"))
async def show_xui_panel(call: CallbackQuery):
    try:
        parts = call.data.split(":")
        instance_id = parts[-1]
    except ValueError:
        return await call.answer("解析异常", show_alert=True)
        
    temp_msg = await call.message.edit_text("⏳ 正在探测服务器环境状态，请稍候...", parse_mode="HTML")
    ip = get_server_ip(instance_id) or "未知IP"
    
    # 嗅探面板是否安装 (查核心数据库文件是否存在)
    probe_script = "if [ -f /etc/x-ui/x-ui.db ]; then echo 'INSTALLED'; else echo 'MISSING'; fi"
    try:
        probe_res = await execute_xui_hybrid(instance_id, call.from_user.id, probe_script)
        is_installed = "INSTALLED" in probe_res
    except Exception:
        is_installed = False
        
    if not is_installed:
        text = (
            f"⚡️ <b>3x-ui 代理面板管控中心</b>\n\n🖥 <b>操作实例</b>：<code>{instance_id}</code> | 🌐 <b>IP</b>：<code>{ip}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n⚠️ <b>环境状态</b>：未检测到 3x-ui 核心组件\n━━━━━━━━━━━━━━━━━━\n"
            f"💡 <b>智能引导：</b>\n当前服务器为纯净状态。请点击下方「一键全新部署」按钮，系统将自动配置环境。部署完成后将解锁全部魔法功能！"
        )
    else:
        # 提取当前状态，并拉取防止网页乱码的 BasePath
        shell_script = """
        STATUS=$(systemctl is-active x-ui || true)
        if [ "$STATUS" = "active" ]; then echo "PANEL_STATUS=running"; else echo "PANEL_STATUS=stopped"; fi
        PORT=$(sqlite3 /etc/x-ui/x-ui.db "SELECT value FROM settings WHERE key='webPort';" 2>/dev/null)
        USER=$(sqlite3 /etc/x-ui/x-ui.db "SELECT username FROM users LIMIT 1;" 2>/dev/null)
        BASE=$(sqlite3 /etc/x-ui/x-ui.db "SELECT value FROM settings WHERE key='webBasePath';" 2>/dev/null)
        echo "PORT=${PORT:-54321}"
        echo "USER=${USER:-admin}"
        echo "BASE=${BASE:-/}"
        """
        info_res = await execute_xui_hybrid(instance_id, call.from_user.id, shell_script)
        data_map = {k.strip(): v.strip() for k, v in [line.split("=", 1) for line in info_res.split("\n") if "=" in line]}
        
        is_running = (data_map.get("PANEL_STATUS") == "running")
        status_text = "🟢 运行中 (Running)" if is_running else "🔴 已停止 (Stopped)"
        XUI_CACHE[instance_id] = {"panel_status": data_map.get("PANEL_STATUS", "stopped"), "user": data_map.get('USER', 'admin'), "expire": time.time() + CACHE_TTL_SECONDS}
        
        # 🌟 核心修复：自动拼装带安全后缀的完整 URL，防止网页变下载！
        base_path = data_map.get('BASE', '/')
        if not base_path.startswith('/'): base_path = '/' + base_path
        if not base_path.endswith('/'): base_path = base_path + '/'
        login_url = f"http://{ip}:{data_map.get('PORT', '54321')}{base_path}"
        
        text = (
            f"⚡️ <b>3x-ui 代理面板管控中心</b>\n\n🖥 <b>操作实例</b>：<code>{instance_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n🛡️ <b>运行状态</b>：{status_text}\n"
            f"🌐 <b>面板地址 (点击访问)</b>：\n<code>{login_url}</code>\n\n"
            f"👤 <b>账号</b>：<code>{data_map.get('USER', 'admin')}</code> | 🔑 <b>密码</b>：<code>********</code> <i>(默认admin)</i>\n"
            f"━━━━━━━━━━━━━━━━━━\n💡 <b>核心指南</b>：\n• <b>极速 Reality</b>：一键生成纯净直连节点，默认 200G。\n• <b>节点管理抽屉</b>：随时查看存活端口，可一键清理流量、删节点或配置高级中转。"
        )
        
    await temp_msg.edit_text(text, reply_markup=build_xui_keyboard(instance_id, is_installed), parse_mode="HTML")
    try:
        await call.answer()
    except Exception:
        pass  # 静默吞掉超时或重复响应的异常，保证 UI 正常渲染完毕

# ================= 🚀 2. 核心路由与管理功能 =================
@router.callback_query(F.data.startswith("xui_cmd:"))
async def execute_xui_command(call: CallbackQuery, state: FSMContext):
    try: _, action, instance_id = call.data.split(":", 2)
    except ValueError: return await call.answer("解析异常", show_alert=True)
    
    ip = get_server_ip(instance_id)
    
    # ================= ⚡️ 动作: 一键极速生成 Reality (真正自然月) =================
    if action == "add_reality_quick":
        wait_msg = await call.message.edit_text("⏳ 正在分配随机端口并生成 Reality 节点，配置 200GB 限额...\n<i>(后台处理中，预计需要 10~15 秒，请稍候...)</i>", parse_mode="HTML")
        try: await call.answer("节点生成中，请等待...", show_alert=False)
        except Exception: pass

        port = random.randint(40000, 58000)
        total_bytes = int(200 * 1024**3)
        today_day = datetime.datetime.now().day 
        
        # 🌟 修复 1：真正的自然月到期计算
        now = datetime.datetime.now()
        m = now.month + 1
        y = now.year
        if m > 12: m = 1; y += 1
        d = min(now.day, calendar.monthrange(y, m)[1])
        exp_date = now.replace(year=y, month=m, day=d)
        expiry_ms = int(exp_date.timestamp() * 1000)
        exp_date_str = exp_date.strftime('%Y-%m-%d')
        
        node_name = f"Node-{''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=4))}"
        
        shell_script = f"""python3 -c "
import sqlite3, json, subprocess, uuid, random

try:
    xray_bin = subprocess.check_output('find /usr/local/x-ui/bin -name \\\"xray*\\\" -type f | head -n 1', shell=True).decode().strip()
    key_out = subprocess.check_output(xray_bin + ' x25519', shell=True).decode()
    priv_key, pub_key = [line.split(':')[-1].strip() for line in key_out.split('\\n') if 'Private' in line or 'Public' in line or 'private' in line or 'public' in line]
except Exception:
    priv_key, pub_key = 'yNu1z_fallback', 'zMu1z_fallback'

client_id = str(uuid.uuid4())
short_id = ''.join(random.choices('0123456789abcdef', k=8))

settings_dict = {{
    'clients': [{{
        'id': client_id,
        'flow': 'xtls-rprx-vision',
        'email': 'reality_{port}',
        'limitIp': 0,
        'totalGB': 0,
        'expiryTime': 0,
        'enable': True,
        'tgId': '',
        'subId': '',
        'reset': {today_day}
    }}],
    'decryption': 'none'
}}

stream_settings_dict = {{
    'network': 'tcp',
    'security': 'reality',
    'realitySettings': {{
        'show': False,
        'dest': 'www.apple.com:443',
        'xver': 0,
        'serverNames': ['www.apple.com'],
        'privateKey': priv_key,
        'shortIds': [short_id]
    }}
}}

sniffing_dict = {{
    'enabled': True,
    'destOverride': ['http', 'tls', 'quic']
}}

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
c = conn.cursor()
c.execute(
    '''INSERT INTO inbounds (user_id, up, down, total, remark, enable, expiry_time, listen, port, protocol, settings, stream_settings, tag, sniffing) VALUES (1, 0, 0, ?, ?, 1, ?, '', ?, 'vless', ?, ?, ?, ?)''',
    ({total_bytes}, '{node_name}', {expiry_ms}, {port}, json.dumps(settings_dict), json.dumps(stream_settings_dict), 'inbound-{port}', json.dumps(sniffing_dict))
)
conn.commit()
conn.close()

print('REALITY_RES:{port}|' + client_id + '|' + pub_key + '|' + short_id)
" && systemctl restart x-ui
"""
        try:
            out = await asyncio.wait_for(execute_xui_hybrid(instance_id, call.from_user.id, shell_script), timeout=180.0)
            if "REALITY_RES:" not in out: raise Exception(f"数据库写入异常，回显截取: {out[:80]}")
                
            port_res, client_id, pub_key, short_id = str(port), "", "", ""
            for line in out.split("\n"):
                if line.startswith("REALITY_RES:"):
                    try: port_res, client_id, pub_key, short_id = line.split(":", 1)[1].split("|")
                    except Exception: pass
            
            vless_link = f"vless://{client_id}@{ip}:{port_res}?security=reality&encryption=none&pbk={pub_key}&headerType=none&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni=www.apple.com&sid={short_id}#{node_name}"
            
            await wait_msg.edit_text(
                f"🎉 <b>VLESS-Reality 生成成功！</b>\n\n🖥 <b>实例</b>：<code>{instance_id}</code>\n🔌 <b>分配端口</b>：<code>{port_res}</code>\n"
                f"📊 <b>流量配额</b>：<b>200 GB</b> (每月 {today_day} 号重置)\n"
                f"📅 <b>到期时间</b>：<b>{exp_date_str}</b>\n\n🚀 <b>专属订阅链接 (点击复制)：</b>\n<code>{vless_link}</code>",
                reply_markup=build_xui_keyboard(instance_id), parse_mode="HTML"
            )
        except asyncio.TimeoutError:
            await wait_msg.edit_text("❌ <b>节点创建失败：</b>\n底层通信严重超时。", reply_markup=build_xui_keyboard(instance_id), parse_mode="HTML")
        except Exception as e:
            await wait_msg.edit_text(f"❌ <b>节点创建失败：</b>\n{str(e)}", reply_markup=build_xui_keyboard(instance_id), parse_mode="HTML")
        return


    # ================= 📋 动作: 抽屉式节点列表 =================
    async def render_port_list_ui(message: Message, inst_id: str, u_id: int):
        msg = await message.edit_text("⏳ 正在拉取底层节点大盘数据...", parse_mode="HTML")
        shell_script = """python3 -c "import sqlite3; conn = sqlite3.connect('/etc/x-ui/x-ui.db'); c = conn.cursor(); c.execute('SELECT port, remark, up, down, total, expiry_time FROM inbounds'); rows = c.fetchall(); [print(f'NODE:{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}') for r in rows]; conn.close()" """
        try:
            out = await execute_xui_hybrid(inst_id, u_id, shell_script)
            buttons = []
            for line in out.split("\n"):
                if line.startswith("NODE:"):
                    try:
                        parts = line.replace("NODE:", "").split("|")
                        if len(parts) >= 6:
                            p, rem, u, d, tot, exp = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
                            used_gb = (int(u) + int(d)) / 1024**3
                            tot_gb = int(tot) / 1024**3 if int(tot) > 0 else 0
                            tot_str = f"{tot_gb:.0f}G" if tot_gb > 0 else "不限"
                            
                            exp_ms = int(exp)
                            is_expired = False
                            if exp_ms > 0:
                                exp_date = datetime.datetime.fromtimestamp(exp_ms / 1000).strftime('%m-%d')
                                is_expired = (exp_ms < time.time() * 1000)
                            else:
                                exp_date = "无限期"
                            
                            status_icon = "🔴" if is_expired or (tot_gb > 0 and used_gb >= tot_gb) else "🟢"
                            btn_text = f"{status_icon} 端口 {p} [{rem}] 流量:{used_gb:.1f}G (到期:{exp_date})"
                            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"xui_cmd:port_ctrl-{p}:{inst_id}")])
                    except Exception: pass
            
            buttons.append([InlineKeyboardButton(text="🔙 返回主控制台", callback_data=f"run_sh:xui:{inst_id}")])
            await msg.edit_text("📋 <b>X-UI 节点流量大盘</b>\n\n👇 点击下方任意端口，展开高阶管控抽屉：", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"❌ 节点获取失败：\n{str(e)}", parse_mode=None)

    if action == "port_list":
        await render_port_list_ui(call.message, instance_id, call.from_user.id)
        try: return await call.answer()
        except: return


    # ================= 🎛 动作: 单个端口专属管控抽屉 =================
    if action.startswith("port_ctrl-"):
        port = action.split("-")[1]
        buttons = [
            [InlineKeyboardButton(text="🔗 获取该节点专属订阅/分享链接", callback_data=f"xui_cmd:port_link-{port}:{instance_id}")],
            [InlineKeyboardButton(text="💰 续费该节点 (延长1个自然月)", callback_data=f"xui_cmd:port_renew-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🔄 强制清零该端口已用流量", callback_data=f"xui_cmd:port_reset-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🎛 为该端口配置 SOCKS 住宅中转", callback_data=f"xui_cmd:port_route-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🗑️ 彻底删除此节点 (不可逆)", callback_data=f"xui_cmd:port_del-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🔙 返回节点列表", callback_data=f"xui_cmd:port_list:{instance_id}")]
        ]
        await call.message.edit_text(f"🎛 <b>专属端口管控台：<code>{port}</code></b>\n\n请选择你要对该节点执行的操作：", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        return await call.answer()


    # ================= 🔗 动作: 重新提取专属链接 =================
    if action.startswith("port_link-"):
        port = action.split("-")[1]
        msg = await call.message.edit_text("⏳ 正在反向解析节点安全配置并生成链接...", parse_mode="HTML")
        
        shell_script = f"""python3 -c "
import sqlite3, json, subprocess
port = {port}
try:
    conn = sqlite3.connect('/etc/x-ui/x-ui.db')
    c = conn.cursor()
    c.execute('SELECT remark, settings, stream_settings FROM inbounds WHERE port=?', (port,))
    row = c.fetchone()
    conn.close()
    if row:
        remark, settings_str, stream_str = row
        settings = json.loads(settings_str)
        stream = json.loads(stream_str)
        client_id = settings['clients'][0]['id']
        priv_key = stream['realitySettings']['privateKey']
        short_id = stream['realitySettings']['shortIds'][0]
        sni = stream['realitySettings']['serverNames'][0]

        xray_bin = subprocess.check_output('find /usr/local/x-ui/bin -name \\\"xray*\\\" -type f | head -n 1', shell=True).decode().strip()
        key_out = subprocess.check_output(xray_bin + ' x25519 -i ' + priv_key, shell=True).decode()
        pub_key = ''
        for line in key_out.split('\\n'):
            if 'Public key:' in line: pub_key = line.split('Public key:')[1].strip()

        print('LINK_RES:vless://' + client_id + '@SERVER_IP:{port}?security=reality&encryption=none&pbk=' + pub_key + '&headerType=none&fp=chrome&type=tcp&flow=xtls-rprx-vision&sni=' + sni + '&sid=' + short_id + '#' + remark)
    else:
        print('ERR_NOT_FOUND')
except Exception as e:
    print('ERR_SCRIPT:' + str(e))
" """
        try:
            out = await execute_xui_hybrid(instance_id, call.from_user.id, shell_script)
            link = ""
            for line in out.split('\n'):
                if line.startswith("LINK_RES:"):
                    link = line.replace("LINK_RES:", "").replace("SERVER_IP", ip)
            
            if link: await msg.edit_text(f"🔗 <b>节点 <code>{port}</code> 的专属配置链接：</b>\n\n<code>{link}</code>", reply_markup=build_xui_keyboard(instance_id), parse_mode="HTML")
            else: await msg.edit_text(f"❌ 链接提取失败，请检查是否为标准 Reality 节点。", reply_markup=build_xui_keyboard(instance_id), parse_mode="HTML")
        except Exception as e: await msg.edit_text(f"❌ 提取异常：{str(e)}", reply_markup=build_xui_keyboard(instance_id), parse_mode=None)
        return


    # ================= 💰 动作: 续费节点 (真正自然月算法) =================
    if action.startswith("port_renew-"):
        port = action.split("-")[1]
        shell_script = f"""python3 -c "
import sqlite3, time, datetime, calendar
conn = sqlite3.connect('/etc/x-ui/x-ui.db')
c = conn.cursor()
c.execute('SELECT expiry_time FROM inbounds WHERE port={port}')
row = c.fetchone()
if row:
    exp_ms = row[0]
    now_ms = time.time() * 1000
    base_time = exp_ms if exp_ms > now_ms else now_ms
    dt = datetime.datetime.fromtimestamp(base_time / 1000.0)
    
    m = dt.month + 1
    y = dt.year
    if m > 12: m = 1; y += 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    new_dt = dt.replace(year=y, month=m, day=d)
    new_exp_ms = int(new_dt.timestamp() * 1000)
    
    c.execute('UPDATE inbounds SET expiry_time=? WHERE port=?', (new_exp_ms, {port}))
    conn.commit()
    print('RENEW_OK')
conn.close()
" && systemctl restart x-ui
"""
        await execute_xui_hybrid(instance_id, call.from_user.id, shell_script)
        await call.answer(f"✅ 端口 {port} 已成功续费 1 个自然月！", show_alert=True)
        return await render_port_list_ui(call.message, instance_id, call.from_user.id)


    # ================= 🗑️ 动作: 删除单节点 =================
    if action.startswith("port_del-"):
        port = action.split("-")[1]
        shell_script = f"sqlite3 /etc/x-ui/x-ui.db 'DELETE FROM inbounds WHERE port={port}' && systemctl restart x-ui && echo 'DEL_OK'"
        await execute_xui_hybrid(instance_id, call.from_user.id, shell_script)
        await call.answer(f"🗑️ 端口 {port} 节点已彻底销毁！", show_alert=True)
        return await render_port_list_ui(call.message, instance_id, call.from_user.id)

    # ================= 🔄 动作: 流量一键清零 =================
    if action.startswith("port_reset-"):
        port = action.split("-")[1]
        shell_script = f"sqlite3 /etc/x-ui/x-ui.db 'UPDATE inbounds SET up=0, down=0 WHERE port={port}' && systemctl restart x-ui && echo 'RST_OK'"
        await execute_xui_hybrid(instance_id, call.from_user.id, shell_script)
        await call.answer(f"✅ 端口 {port} 的已用流量已强行清零！", show_alert=True)
        return await render_port_list_ui(call.message, instance_id, call.from_user.id)

    # ================= 🎛 动作: 绑定中转路由 (进入向导) =================
    if action.startswith("port_route-"):
        port = action.split("-")[1]
        await state.update_data(route_port=port, route_instance=instance_id)
        await state.set_state(XuiRouteFSM.wait_for_upstream)
        await call.message.answer(
            f"🎛 <b>为端口 <code>{port}</code> 绑定中转路由</b>\n\n"
            f"👉 请回复上游 SOCKS 住宅中转信息，格式：\n<code>IP:端口:账号:密码</code>\n\n"
            f"<i>(如需取消，发送 0)</i>", parse_mode="HTML"
        )
        return await call.answer()


    # ================= ⚙️ 基础运维启停等指令 =================
    msg_tip = await call.message.edit_text(f"⏳ 正在向实例下发 <code>{action}</code> 指令...\n<i>(若为首次安装，需要 30~60 秒，请耐心等待)</i>", parse_mode="HTML")
    try: await call.answer("指令已开始在后台执行，请耐心等待...", show_alert=False)
    except Exception: pass

    if action == "install": shell_script = """apt-get update -y && apt-get install -y curl wget sqlite3\nbash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) <<< $'y\\nadmin\\nadmin\\n54321\\n' || true\n/usr/local/x-ui/x-ui setting -username admin -password admin -port 54321 || true\nsystemctl enable x-ui && systemctl restart x-ui\necho "INSTALL_XUI_SUCCESS" """
    elif action == "update": shell_script = "bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) <<< $'n\\n' && systemctl restart x-ui"
    elif action == "start": shell_script = "systemctl start x-ui && systemctl restart x-ui && echo 'SUCCESS'"
    elif action == "stop": shell_script = "systemctl stop x-ui && echo 'SUCCESS'"
    elif action == "restart": shell_script = "systemctl restart x-ui && echo 'SUCCESS'"
    elif action == "reset_pass": shell_script = "/usr/local/x-ui/x-ui setting -username admin -password admin -port 54321 && systemctl restart x-ui && echo 'RESET_SUCCESS'"
    elif action == "uninstall": shell_script = "systemctl stop x-ui 2>/dev/null; systemctl disable x-ui 2>/dev/null; rm -rf /etc/x-ui /usr/local/x-ui /usr/bin/x-ui /etc/systemd/system/x-ui.service; systemctl daemon-reload; echo 'UNINSTALL_SUCCESS'"
    else: shell_script = "echo 'Unknown command'"

    try:
        await execute_xui_hybrid(instance_id, call.from_user.id, shell_script)
        if action in ["start", "stop", "restart", "reset_pass", "install", "uninstall"]:
            if action in ["start", "restart", "install"]:
                if instance_id not in XUI_CACHE: XUI_CACHE[instance_id] = {"panel_status": "running", "user": "admin", "pass": "admin", "port": "54321"}
                else: XUI_CACHE[instance_id]["panel_status"] = "running"
            elif action in ["stop", "uninstall"]:
                if instance_id in XUI_CACHE: XUI_CACHE[instance_id]["panel_status"] = "stopped"
                else: XUI_CACHE[instance_id] = {"panel_status": "stopped", "user": "admin", "pass": "admin", "port": "54321"}
            XUI_CACHE[instance_id]["expire"] = time.time() + CACHE_TTL_SECONDS
            await msg_tip.edit_text(f"🎉 <b>指令执行成功！</b>\n\n即将刷新面板状态...", parse_mode="HTML")
            await asyncio.sleep(1.5) 
            return await show_xui_panel(call)
    except Exception as e:
        await msg_tip.edit_text(f"❌ 执行失败：\n{str(e)}", parse_mode=None)

# ================= 🚀 3. FSM：接收中转路由绑定 (含防呆连通性侦测) =================
@router.message(XuiRouteFSM.wait_for_upstream)
async def xui_route_step(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    port = data.get("route_port")
    instance_id = data.get("route_instance")
    await state.clear()
    
    # 🌟 核心优化：临时复刻专属端口管控键盘，配置完直接留在当前抽屉！
    def get_port_keyboard(p, inst_id):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 获取该节点专属订阅/分享链接", callback_data=f"xui_cmd:port_link-{p}:{inst_id}")],
            [InlineKeyboardButton(text="💰 续费该节点 (延长1个自然月)", callback_data=f"xui_cmd:port_renew-{p}:{inst_id}")],
            [InlineKeyboardButton(text="🔄 强制清零该端口已用流量", callback_data=f"xui_cmd:port_reset-{p}:{inst_id}")],
            [InlineKeyboardButton(text="🎛 为该端口重新配置 SOCKS 中转", callback_data=f"xui_cmd:port_route-{p}:{inst_id}")],
            [InlineKeyboardButton(text="🗑️ 彻底删除此节点 (不可逆)", callback_data=f"xui_cmd:port_del-{p}:{inst_id}")],
            [InlineKeyboardButton(text="🔙 返回节点列表", callback_data=f"xui_cmd:port_list:{inst_id}")]
        ])

    port_kb = get_port_keyboard(port, instance_id)

    if text == "0":
        return await message.answer("已取消配置。", reply_markup=port_kb)
        
    wait_msg = await message.answer(f"⏳ 正在为端口 <code>{port}</code> 注入底层出站路由规则...\n<i>(正在探测上游 SOCKS 存活性...)</i>", parse_mode="HTML")
    
    shell_script = f"""python3 -c "
import sqlite3, json, subprocess
port = {port}; upstream_str = '{text}'
parts = upstream_str.split(':')
outbound_tag = f'outbound-fwd-{{port}}'
inbound_tag = f'inbound-{{port}}'

# 利用服务器自带的 curl 直接打向外界测试代理真假
curl_url = 'socks5h://' + (parts[2] + ':' + parts[3] + '@' + parts[0] + ':' + parts[1] if len(parts)>2 else parts[0] + ':' + parts[1])
try:
    res = subprocess.check_output(['curl', '-s', '-m', '5', '-x', curl_url, 'https://api.ipify.org'], timeout=6).decode().strip()
    print('TEST_RES:✅ 代理可用！落地真实 IP: ' + res)
except Exception:
    print('TEST_RES:❌ 代理不可用！(连接超时或密码错误，客户端将无法打开网页)')

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
c = conn.cursor()
c.execute(\\\"SELECT value FROM settings WHERE key='xrayTemplateConfig'\\\")
row = c.fetchone()

if row and row[0]:
    xray_tmpl = json.loads(row[0])
else:
    xray_tmpl = {{'log': {{'loglevel': 'warning'}}, 'inbounds': [], 'outbounds': [{{'tag': 'direct', 'protocol': 'freedom', 'settings': {{}}}}, {{'tag': 'blocked', 'protocol': 'blackhole', 'settings': {{}}}}], 'routing': {{'domainStrategy': 'AsIs', 'rules': [{{'type': 'field', 'outboundTag': 'blocked', 'ip': ['geoip:private']}}]}}}}

if 'outbounds' not in xray_tmpl: xray_tmpl['outbounds'] = []
xray_tmpl['outbounds'] = [ob for ob in xray_tmpl['outbounds'] if ob.get('tag') != outbound_tag]

if 'routing' not in xray_tmpl: xray_tmpl['routing'] = {{'rules': []}}
if 'rules' not in xray_tmpl['routing']: xray_tmpl['routing']['rules'] = []
xray_tmpl['routing']['rules'] = [r for r in xray_tmpl['routing']['rules'] if r.get('outboundTag') != outbound_tag]

user_pass = []
if len(parts) > 2:
    user_pass = [{{'user': parts[2], 'pass': parts[3] if len(parts) > 3 else ''}}]

new_outbound = {{'tag': outbound_tag, 'protocol': 'socks', 'settings': {{'servers': [{{'address': parts[0], 'port': int(parts[1]), 'users': user_pass}}]}}}}
xray_tmpl['outbounds'].append(new_outbound)
xray_tmpl['routing']['rules'].insert(0, {{'type': 'field', 'inboundTag': [inbound_tag], 'outboundTag': outbound_tag}})

if row:
    c.execute(\\\"UPDATE settings SET value=? WHERE key='xrayTemplateConfig'\\\", (json.dumps(xray_tmpl),))
else:
    c.execute(\\\"INSERT INTO settings (key, value) VALUES ('xrayTemplateConfig', ?)\\\", (json.dumps(xray_tmpl),))

conn.commit()
conn.close()
print('ROUTE_OK')
" && systemctl restart x-ui """

    try:
        out = await asyncio.wait_for(execute_xui_hybrid(instance_id, message.from_user.id, shell_script), timeout=180.0)
        if "ROUTE_OK" in out:
            # 解析测试结果并去除多余回显
            test_info = "未知状态"
            for line in out.split('\n'):
                if line.startswith("TEST_RES:"):
                    test_info = line.replace("TEST_RES:", "").strip()
                    
            await wait_msg.edit_text(
                f"✅ <b>端口 {port} 路由绑定成功！</b>\n\n"
                f"🔀 <b>数据流向：</b>\n"
                f"本机端口 <code>{port}</code> -> 中转落地 <code>{text}</code>\n\n"
                f"📡 <b>上游连通性自动侦测：</b>\n"
                f"{test_info}\n\n"
                f"<i>(配置已热重载生效。若侦测失败，您的客户端依然能 Ping 通节点，但将无法打开网页！)</i>", 
                reply_markup=port_kb, parse_mode="HTML"
            )
        else:
            await wait_msg.edit_text(f"⚠️ 路由绑定异常回显：{out[:80]}", reply_markup=port_kb)
    except asyncio.TimeoutError:
        await wait_msg.edit_text("❌ <b>配置失败：</b>\n底层通信严重超时，未能确认路由注入结果。", reply_markup=port_kb, parse_mode="HTML")
    except Exception as e:
        await wait_msg.edit_text(f"❌ <b>路由注入失败：</b>\n{str(e)}", reply_markup=port_kb, parse_mode="HTML")
