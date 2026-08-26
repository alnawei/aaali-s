import sqlite3
import config
from aiogram import Router, F, types
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from db import get_active_servers  
import asyncio
import paramiko
import time
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

class AddServerFSM(StatesGroup):
    wait_for_ip = State()
    wait_for_pwd = State()

router = Router()

REGION_MAP = {
    "cn-hongkong": "香港",
    "ap-northeast-1": "东京",
    "ap-southeast-1": "新加坡",
    "us-west-1": "硅谷",
    "cn-shanghai": "上海",
}

SWAS_REGIONS = [
    "cn-hongkong", "ap-southeast-1", "ap-northeast-1", "ap-northeast-2",
    "us-east-1", "us-west-1", "eu-central-1", "eu-west-1",
    "cn-hangzhou", "cn-beijing", "cn-shanghai", "cn-shenzhen",
    "cn-chengdu", "cn-qingdao", "cn-guangzhou", "cn-heyuan",
    "cn-huhehaote", "cn-wulanchabu"
]

SWAS_LOADING_ACCOUNTS = set()

def get_servers_data(user_id: int):
    try:
        from db import get_active_servers
        servers = get_active_servers(user_id)
    except Exception:
        servers = []

    if not servers:
        try:
            conn = sqlite3.connect(config.DB_PATH, timeout=3.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT instance_id, ip, region as region_id FROM ecs_business")
            servers = [dict(r) for r in cursor.fetchall()]
            conn.close()
        except Exception:
            pass

    for srv in servers:
        inst_id = srv.get("instance_id", "")
        ip_val = str(srv.get("ip", "")).strip()
        
        if ip_val in ["0.0.0.0", "", "None", "IP分配中..."]:
            real_ip = None
            try:
                from utils.aliyun import get_instance_ip 
                real_ip = get_instance_ip(inst_id)
            except Exception:
                pass
                
            if real_ip and "0.0.0" not in real_ip:
                srv["ip"] = real_ip
                try:
                    conn = sqlite3.connect(config.DB_PATH, timeout=3.0)
                    conn.execute("UPDATE ecs_business SET ip = ? WHERE instance_id = ?", (real_ip, inst_id))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

    return servers if servers else []

def build_servers_keyboard(user_id: int):
    servers = get_servers_data(user_id)
    builder = InlineKeyboardBuilder()
    
    REGION_SHORT = {
        "cn-hongkong": "HK", "hongkong": "HK",
        "ap-northeast-1": "JP", "tokyo": "JP",
        "ap-northeast-2": "KR", "seoul": "KR",
        "ap-southeast-1": "SG", "singapore": "SG",
        "us-west-1": "US", "us-east-1": "US",
        "eu-central-1": "DE", "frankfurt": "DE"
    }
    
    grouped_accounts = {}
    ssh_nodes = []
    
    for srv in servers:
        inst_id = str(srv.get("instance_id", ""))
        ip_val = srv.get("ip", "")
        
        if ip_val in ["0.0.0.0", "", "None"] and not inst_id.startswith("ssh_") and not inst_id.startswith("i-"):
            continue 
            
        if "root_password" in srv or inst_id.startswith("ssh_"):
            ssh_nodes.append(srv)
            continue
            
        if inst_id.startswith("i-"):
            acc_id = srv.get('account_id', 1)
            acc_name = f"主账号 (ID:{acc_id})" if acc_id == 1 else f"账号 {acc_id}"
            if acc_id not in grouped_accounts:
                grouped_accounts[acc_id] = {'name': acc_name, 'nodes': []}
            grouped_accounts[acc_id]['nodes'].append(srv)
            
    # 1. ECS 机器
    for acc_id, acc_info in grouped_accounts.items():
        builder.row(InlineKeyboardButton(text=f"━━━ 🏢 阿里云：{acc_info['name']} ━━━", callback_data="ignore_click"))
        row_buttons = []
        for srv in acc_info['nodes']:
            inst_id = srv.get("instance_id", "")
            ip_display = srv.get("ip", "0.0.0.0")
            if ip_display in ["0.0.0.0", "", "None", "IP分配中..."]:
                ip_display = "分配中"
                status_icon = "🔵"
            else:
                status_raw = str(srv.get("status", srv.get("state", "Running"))).lower()
                status_icon = "🟢" if "running" in status_raw or "运行" in status_raw or status_raw == "1" else "🔴"
                
            region = srv.get("region_id", srv.get("region", ""))
            r_short = REGION_SHORT.get(region, region.split('-')[-1][:2].upper() if region else "未知")
            
            btn_text = f"{status_icon} {r_short} | {ip_display}"
            row_buttons.append(InlineKeyboardButton(text=btn_text, callback_data=f"srv_sel:{inst_id}"))
            
            if len(row_buttons) == 2:
                builder.row(*row_buttons)
                row_buttons = []
        if row_buttons:
            builder.row(*row_buttons)

    # 2. 轻量云 (SWAS) 入口
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=3.0)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM cloud_accounts WHERE is_active = 1")
        accounts = cursor.fetchall()
        conn.close()

        if accounts:
            builder.row(InlineKeyboardButton(text="━━━ 🪶 阿里云轻量云 (SWAS) ━━━", callback_data="ignore_click"))
            swas_buttons = []
            for acc in accounts:
                acc_id = acc[0]
                acc_name = f"主账号 (ID:{acc_id})" if acc_id == 1 else f"账号 {acc_id}"
                swas_buttons.append(InlineKeyboardButton(text=f"☁️ 轻量云 ({acc_name})", callback_data=f"node_expand_swas:{acc_id}"))
            
            for i in range(0, len(swas_buttons), 2):
                builder.row(*swas_buttons[i:i+2])
    except Exception as e:
        print(f"⚠️ 渲染轻量云按钮失败: {e}")

    # 3. 自定义 SSH 机器
    if ssh_nodes:
        builder.row(InlineKeyboardButton(text="━━━ 🔌 自定义 SSH 服务器 ━━━", callback_data="ignore_click"))
        row_buttons = []
        for srv in ssh_nodes:
            inst_id = srv.get("instance_id", "")
            ip_display = srv.get("ip", "未知IP")
            btn_text = f"🟢 SSH | {ip_display}"
            row_buttons.append(InlineKeyboardButton(text=btn_text, callback_data=f"srv_sel:{inst_id}"))
            if len(row_buttons) == 2:
                builder.row(*row_buttons)
                row_buttons = []
        if row_buttons:
            builder.row(*row_buttons)
            
    builder.row(InlineKeyboardButton(text="➕ 添加自定义服务器 (SSH)", callback_data="custom_srv:add"))
    return builder.as_markup()

@router.message(F.text == "⚙️ 节点配置")
async def show_node_list(message: types.Message):
    servers = get_servers_data(message.from_user.id)
    if not servers:
        return await message.answer(
            "📭 **当前名下暂无可用机器！**\n\n"
            "请先前往【💻 服务器管理】开通新服务器，或点击下方按钮添加自定义节点。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ 添加自定义服务器 (SSH)", callback_data="custom_srv:add")]
            ]),
            parse_mode="Markdown"
        )

    keyboard = build_servers_keyboard(message.from_user.id)
    await message.answer(
        "⚙️ **节点配置中心 (第一步)**\n\n请在下方悬浮菜单中选择你要操作的服务器：",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# 仅保留 bbr 与 x-ui 的状态探测
def check_all_scripts_status(instance_id: str, ip: str) -> dict:
    res = {"bbr": False, "xui": False}
    if not ip or ip in ["0.0.0.0", "阿里云分配IP中...", "未知IP"]:
        return res
        
    pwd = getattr(config, 'SSH_PASSWORD', getattr(config, 'ROOT_PASSWORD', '@QS00008'))
    if instance_id and instance_id.startswith("ssh_"):
        try:
            import db
            custom_pwd = db.get_custom_server_password(instance_id)
            if custom_pwd: pwd = custom_pwd
        except Exception: pass

    client = None
    for attempt in range(3):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=ip, port=22, username="root", password=pwd, 
                timeout=8.0, banner_timeout=15.0, auth_timeout=8.0
            )
            break 
        except Exception as conn_e:
            if client: 
                client.close()
                client = None
            err_msg = str(conn_e).lower()
            if attempt < 2 and ("banner" in err_msg or "closed" in err_msg or "refused" in err_msg):
                time.sleep(1.5)
                continue
            else:
                return res

    if not client:
        return res

    try:
        combined_cmd = """
        if sysctl net.ipv4.tcp_congestion_control 2>/dev/null | grep -qi bbr; then echo 'RES_bbr:1'; else echo 'RES_bbr:0'; fi
        # 只把 systemd active 当作“运行中”；已安装但停止的 x-ui 必须显示为 🔴。
        if systemctl is-active --quiet x-ui; then echo 'RES_xui:1'; else echo 'RES_xui:0'; fi
        """
        stdin, stdout, stderr = client.exec_command(combined_cmd, timeout=5.0)
        output = stdout.read().decode('utf-8')
        
        for line in output.splitlines():
            if "RES_bbr:1" in line: res["bbr"] = True
            if "RES_xui:1" in line: res["xui"] = True
            
    except Exception:
        pass
    finally:
        if client:
            client.close()
            
    return res

@router.callback_query(F.data.startswith("srv_sel:"))
async def show_script_options(call: types.CallbackQuery):
    try:
        _, instance_id = call.data.split(":")
    except ValueError:
        return await call.answer("数据解析异常！", show_alert=True)
    
    servers = get_servers_data(call.from_user.id)
    srv = next((s for s in servers if s["instance_id"] == instance_id), None)
    
    if not srv:
        try:
            conn = sqlite3.connect(config.DB_PATH, timeout=3.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT instance_id, ip, region_id FROM ecs_business WHERE instance_id=?", (instance_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                srv = dict(row)
        except Exception:
            pass

    if not srv:
        return await call.answer("❌ 无法在本地账本中定位该服务器，数据可能未同步！", show_alert=True)
        
    region_id = srv.get("region_id", srv.get("region", "cn-hongkong"))
    region_name = REGION_MAP.get(region_id, region_id)
    public_ip = srv.get("ip", "0.0.0.0")
    
    if public_ip == "0.0.0.0" or not public_ip:
        public_ip = "⏳ 阿里云分配IP中..."
    
    # 彻底移除 MG 面板
    raw_scripts = [
        {"id": "bbr", "label": "bbr 加速"},
        {"id": "xui", "label": "3x-ui 面板 (全能代理)"},
    ]
    
    status_dict = await asyncio.to_thread(check_all_scripts_status, instance_id, public_ip)
    
    builder = []
    for script in raw_scripts:
        is_running = status_dict.get(script["id"], False)
        status_icon = "🟢" if is_running else "🔴"
        button_text = f"{status_icon} {script['label']}"
        
        # 传递 IP 确保后续面板无需查库直接连接
        if script["id"] == "xui":
            # 告诉 panel_action：这是从 SWAS 进入的，便于修复“返回上一级”路径。(需要你当前作用域有 acc_id)
            cb_data = f"run_sh:xui:{instance_id}:{public_ip}:swas:{acc_id}"
        else:
            cb_data = f"run_sh:{script['id']}:{instance_id}:{public_ip}"
            
        builder.append([InlineKeyboardButton(text=button_text, callback_data=cb_data)])
    
    if not instance_id.startswith("i-"):
        builder.append([InlineKeyboardButton(text="❌ 移除此自定义服务器", callback_data=f"del_custom_srv:{instance_id}")])

    builder.append([InlineKeyboardButton(text="🔙 返回服务器列表", callback_data="back_to_srv_list")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=builder)
    
    await call.message.edit_text(
        f"⚙️ **节点配置中心 (第二步)**\n\n"
        f"选中实例: `{instance_id}`\n"
        f"所属地域: {region_name}\n"
        f"公网IP：`{public_ip}`\n\n"
        f"👉 请选择要向该服务器下发的 Shell 脚本 *(🟢运行中 / 🔴未安装)*：",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await call.answer()

@router.callback_query(F.data == "back_to_srv_list")
async def back_to_servers(call: types.CallbackQuery):
    servers = get_servers_data(call.from_user.id)
    if not servers:
        return await call.message.edit_text(
            "📭 **当前名下暂无可用机器！**\n\n"
            "请先前往【💻 服务器管理】开通新服务器，或点击下方按钮添加自定义节点。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ 添加自定义服务器 (SSH)", callback_data="custom_srv:add")]
            ]),
            parse_mode="Markdown"
        )

    keyboard = build_servers_keyboard(call.from_user.id)
    await call.message.edit_text(
        "⚙️ **节点配置中心 (第一步)**\n\n请在下方悬浮菜单中选择你要操作的服务器：",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await call.answer()

@router.callback_query(F.data.startswith("del_custom_srv:"))
async def process_del_custom_server(call: types.CallbackQuery):
    instance_id = call.data.split(":")[-1]
    try:
        import db
        db.delete_custom_server(instance_id)
    except Exception as e:
        return await call.answer(f"删除失败: {e}", show_alert=True)
    
    await call.answer("❌ 自定义服务器已彻底从控制台移除！", show_alert=True)
    await call.message.edit_text(
        f"✅ **操作成功**\n\n"
        f"实例 `{instance_id}` 的本地信息及业务计费数据已抹除。\n\n"
        f"*(此操作仅解除机器人的管控，不影响服务器本身的运行)*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回节点配置列表", callback_data="back_to_srv_list")]
        ]),
        parse_mode="Markdown"
    )

# ================= ➕ 添加自定义服务器逻辑 (FSM) =================
async def test_ssh_connection(ip: str, password: str, port: int = 22) -> bool:
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        await asyncio.to_thread(client.connect, hostname=ip, port=port, username="root", password=password, timeout=5.0)
        client.close()
        return True
    except Exception:
        return False

@router.callback_query(F.data == "custom_srv:add")
async def add_custom_server_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != config.ADMIN_ID:
        return await call.answer("权限不足！", show_alert=True)
    await state.set_state(AddServerFSM.wait_for_ip)
    await call.message.answer("➕ **添加自定义服务器 (SSH)**\n\n🌐 请输入服务器的公网 IP 地址：\n*(回复 0 取消操作)*", parse_mode="Markdown")
    await call.answer()

@router.message(AddServerFSM.wait_for_ip)
async def add_custom_server_ip(message: types.Message, state: FSMContext):
    ip = message.text.strip()
    if ip == '0':
        await state.clear()
        return await message.answer("已取消操作。")
    await state.update_data(ip=ip)
    await state.set_state(AddServerFSM.wait_for_pwd)
    await message.answer(f"✅ IP `{ip}` 已记录。\n\n🔑 请输入该服务器的 Root 密码：\n*(回复 0 取消操作)*", parse_mode="Markdown")

@router.message(AddServerFSM.wait_for_pwd)
async def add_custom_server_pwd(message: types.Message, state: FSMContext):
    pwd = message.text.strip()
    if pwd == '0':
        await state.clear()
        return await message.answer("已取消操作。")

    data = await state.get_data()
    ip = data['ip']
    
    wait_msg = await message.answer(f"⏳ 正在探测服务器 `{ip}` 的连通性，请稍候...", parse_mode="Markdown")
    is_connected = await test_ssh_connection(ip, pwd)
    
    if is_connected:
        instance_id = f"ssh_{ip.replace('.', '_')}" 
        try:
            import db
            db.add_custom_server(instance_id, ip, pwd)
            await state.clear()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 刷新节点配置列表", callback_data="back_to_srv_list")]
            ])
            await wait_msg.edit_text(
                f"🎉 **自定义服务器添加成功！**\n\n"
                f"✅ **SSH 测试通过**，已确认服务器存活！\n"
                f"🌐 IP: `{ip}`\n"
                f"🆔 实例标识: `{instance_id}`\n\n"
                f"已无缝接入节点配置中心，请点击下方按钮刷新面板。",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ 添加失败，可能是数据库写入异常: {e}")
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 重新填写密码", callback_data="custom_srv:retry_pwd")],
            [InlineKeyboardButton(text="🗑️ 取消添加 (放弃保存)", callback_data="custom_srv:cancel")]
        ])
        await wait_msg.edit_text(
            f"🔴 **连接失败！**\n\n无法通过 SSH 连上 `{ip}`。\n可能是密码错误、22端口未开或禁止 Root 登录。\n\n请选择后续操作：",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

@router.callback_query(F.data == "custom_srv:retry_pwd")
async def retry_custom_server_pwd(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ip = data.get('ip')
    if not ip:
        return await call.message.edit_text("❌ 会话已过期，请重新发起添加操作。")
    await state.set_state(AddServerFSM.wait_for_pwd)
    await call.message.edit_text(
        f"👉 请重新输入服务器 `{ip}` 的 SSH 密码 (root):\n*(回复 0 取消操作)*", 
        parse_mode="Markdown"
    )
    await call.answer()

@router.callback_query(F.data == "custom_srv:cancel")
async def cancel_custom_server_add(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 返回节点配置列表", callback_data="back_to_srv_list")]
    ])
    await call.message.edit_text("🗑️ 操作已取消，无效的服务器信息已被丢弃。", reply_markup=keyboard)
    await call.answer()

# ================= 🪶 轻量云并发查询与展开 =================
def _fetch_single_swas_region(ak: str, sk: str, region_id: str):
    from alibabacloud_swas_open20200601.client import Client as SwasClient
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_swas_open20200601 import models as swas_models

    try:
        ali_config = open_api_models.Config(
            access_key_id=ak.strip(), access_key_secret=sk.strip(),
            endpoint=f'swas.{region_id}.aliyuncs.com'
        )
        client = SwasClient(ali_config)
        req = swas_models.ListInstancesRequest(region_id=region_id, page_size=100)
        resp = client.list_instances(req)
        if resp.body and resp.body.instances:
            return [
                {
                    "id": inst.instance_id,
                    "ip": inst.public_ip_address if inst.public_ip_address else "0.0.0.0",
                    "status": inst.status,
                    "region": region_id
                }
                for inst in resp.body.instances
            ]
    except Exception:
        pass
    return []

async def fetch_all_swas_concurrently(ak: str, sk: str):
    sem = asyncio.Semaphore(6)
    async def _worker(region_id: str):
        async with sem:
            return await asyncio.to_thread(_fetch_single_swas_region, ak, sk, region_id)

    tasks = [_worker(r) for r in SWAS_REGIONS]
    results = await asyncio.gather(*tasks)
    return [inst for sublist in results for inst in sublist]

@router.callback_query(F.data.startswith("node_expand_swas:"))
async def expand_swas_nodes(call: types.CallbackQuery):
    try:
        acc_id = int(call.data.split(":")[1])
    except ValueError:
        return await call.answer("数据解析异常！", show_alert=True)

    if acc_id in SWAS_LOADING_ACCOUNTS:
        return await call.answer("⚠️ 正在检索云端实例中，请稍候...", show_alert=False)

    SWAS_LOADING_ACCOUNTS.add(acc_id)
    await call.answer("🔄 正在并发检索 18 个地域轻量云...", show_alert=False)

    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=3.0)
        cursor = conn.cursor()
        cursor.execute("SELECT access_key, access_secret FROM cloud_accounts WHERE id = ?", (acc_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return await call.message.answer("❌ 找不到对应的云账号凭据！")
            
        ak, sk = row
        swas_instances = await fetch_all_swas_concurrently(ak, sk)

        builder = InlineKeyboardBuilder()
        acc_name = f"主账号 (ID:{acc_id})" if acc_id == 1 else f"账号 {acc_id}"

        if not swas_instances:
            builder.row(InlineKeyboardButton(text="⚠️ 该账号下暂无轻量云服务器", callback_data="ignore_click"))
        else:
            for inst in swas_instances:
                status_emoji = "🟢" if inst['status'] == 'Running' else "🔴"
                builder.row(InlineKeyboardButton(
                    text=f"{status_emoji} SWAS | {inst['ip']}", 
                    callback_data=f"swas_setup:{acc_id}:{inst['id']}:{inst['ip']}" 
                ))
                
        builder.row(InlineKeyboardButton(text="🔙 返回节点配置", callback_data="back_to_srv_list")) 
        text = f"🪶 **轻量云 (SWAS) 节点列表**\n\n所属账号: `{acc_name}` (共找到 {len(swas_instances)} 台)\n请选择你要配置的服务器："

        try:
            await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                await call.answer("✅ 列表已经是最新状态")
            else:
                raise e

    except Exception as e:
        await call.message.answer(f"❌ 展开轻量云失败：`{str(e)}`", parse_mode="Markdown")
    finally:
        SWAS_LOADING_ACCOUNTS.discard(acc_id)

# ================= 🪶 轻量云装机脚本面板 =================
@router.callback_query(F.data.startswith("swas_setup:"))
async def show_swas_script_options(call: types.CallbackQuery):
    try:
        _, acc_id, instance_id, public_ip = call.data.split(":")
    except ValueError:
        return await call.answer("数据解析异常！", show_alert=True)
        
    await call.answer("正在探测服务器状态...", show_alert=False)

    # 彻底移除 MG 面板
    raw_scripts = [
        {"id": "bbr", "label": "bbr 加速"},
        {"id": "xui", "label": "3x-ui 面板 (全能代理)"},
    ]
    
    status_dict = await asyncio.to_thread(check_all_scripts_status, instance_id, public_ip)
    
    builder = []
    for script in raw_scripts:
        is_running = status_dict.get(script["id"], False)
        status_icon = "🟢" if is_running else "🔴"
        button_text = f"{status_icon} {script['label']}"
        # 传递 IP 确保后续面板无需查库直接连接
        cb_data = f"run_sh:{script['id']}:{instance_id}:{public_ip}"
        builder.append([InlineKeyboardButton(text=button_text, callback_data=cb_data)])

    builder.append([InlineKeyboardButton(text="🔙 返回节点列表", callback_data=f"node_expand_swas:{acc_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=builder)
    
    await call.message.edit_text(
        f"⚙️ **轻量云 (SWAS) 节点配置**\n\n"
        f"选中实例: `{instance_id}`\n"
        f"公网IP：`{public_ip}`\n\n"
        f"👉 请选择要向该服务器下发的 Shell 脚本 *(🟢运行中 / 🔴未安装)*：",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
