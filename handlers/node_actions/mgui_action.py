import asyncio
import time
import datetime
import calendar
import random
import sqlite3
import paramiko
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import config
from db import get_active_servers

router = Router()

# ================= 🧠 全局 TTL 内存缓存池 =================
MG_CACHE = {}
CACHE_TTL_SECONDS = 300

# ================= 🛠️ FSM 状态机 =================
class MguiBindBotFSM(StatesGroup):
    wait_for_custom_token = State()
    wait_for_custom_admin = State()

class MguiPortFSM(StatesGroup):
    wait_for_custom_secret = State()
    wait_for_ad_tag = State()

class MguiCustomNodeFSM(StatesGroup):
    wait_for_port = State()
    wait_for_pwd = State()
    wait_for_traffic = State()

# ================= 🛠️ 底层客户端与双引擎工具 =================
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
    """从本地数据库获取手动添加的服务器密码"""
    import db
    try:
        pwd = db.get_custom_server_password(instance_id)
        if pwd:
            return pwd.strip()
    except Exception:
        pass

    import config
    import sqlite3
    try:
        db_path = getattr(config, 'DB_PATH', '/srv/Ali/bot_data.db')
        conn = sqlite3.connect(db_path, timeout=4.0)
        cursor = conn.cursor()
        
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

async def execute_mg_hybrid(instance_id: str, user_id: int, shell_script: str) -> str:
    """极速单轨 SSH 执行器 (彻底接管 MG 面板的所有服务器)"""
    
    def _sync_ssh_task():
        # ✅ 修复：将同步 I/O 移入子线程，防止阻塞 asyncio 主事件循环
        ip = get_server_ip(instance_id)
        if not ip: 
            raise Exception("智能路由失败：无法在本地数据库中找到该实例的公网 IP。")
            
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            if instance_id.startswith("i-"):
                pwd = getattr(config, 'SSH_PASSWORD', getattr(config, 'ROOT_PASSWORD', '@QS00008'))
            else:
                pwd = get_server_password(instance_id)
                if not pwd:
                    raise Exception("无法在数据库中读取到该手动实例的 SSH 密码。")
            
            client.connect(hostname=ip, port=22, username="root", password=pwd, timeout=8.0)
            
            stdin, stdout, stderr = client.exec_command(shell_script, timeout=180.0)
            
            stdout.channel.settimeout(180.0)
            stderr.channel.settimeout(180.0)
            
            out_str = stdout.read().decode('utf-8', errors='ignore').strip()
            err_str = stderr.read().decode('utf-8', errors='ignore').strip()
            return (out_str + "\n" + err_str).strip() or "SUCCESS"
            
        except Exception as exec_e:
            err_msg = str(exec_e) or repr(exec_e)
            if "timeout" in err_msg.lower():
                raise Exception("指令执行超时 (耗时任务可能仍在后台运行，请等待片刻后重试)")
            raise Exception(f"SSH底层报错: {err_msg}")
        finally:
            if client:
                client.close()

    try:
        return await asyncio.to_thread(_sync_ssh_task)
    except Exception as e:
        raise Exception(str(e))

# ================= 🎨 动态 UI 键盘渲染 =================
def build_mg_keyboard(instance_id: str, is_installed: bool = True) -> InlineKeyboardMarkup:
    if not is_installed:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 一键全新部署 MG-UI (纯净版)", callback_data=f"mg_cmd:install:{instance_id}")],
            [InlineKeyboardButton(text="🔄 重新探测面板状态", callback_data=f"run_sh:mgui:{instance_id}")],
            [InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"srv_sel:{instance_id}")]
        ])
    
    is_running = True
    if instance_id in MG_CACHE and time.time() < MG_CACHE[instance_id]["expire"]:
        is_running = (MG_CACHE[instance_id].get("panel_status") == "running")

    toggle_btn = InlineKeyboardButton(text="🛑 停止面板服务", callback_data=f"mg_cmd:stop:{instance_id}") if is_running else InlineKeyboardButton(text="🟢 启动面板服务", callback_data=f"mg_cmd:start:{instance_id}")

    builder = [
        [InlineKeyboardButton(text="⚡ 一键生成 MG 专属节点 (直连 / 500G)", callback_data=f"mg_cmd:add_mtp_quick:{instance_id}")],
        [InlineKeyboardButton(text="🛠️ 生成自定义节点 (自定义端口/密码/流量)", callback_data=f"mg_cmd:add_mtp_custom:{instance_id}")],
        [InlineKeyboardButton(text="📋 节点列表与端口管理 (改配置/重置流量)", callback_data=f"mg_cmd:port_list:{instance_id}")],
        [InlineKeyboardButton(text="🔄 一键重启/修复所有 MTP 节点", callback_data=f"mg_cmd:fix_all_mtp:{instance_id}")],
        [toggle_btn, InlineKeyboardButton(text="🔑 恢复默认账密", callback_data=f"mg_cmd:reset_pass:{instance_id}")],
        [InlineKeyboardButton(text="🤖 设置全局预警 Bot", callback_data=f"mg_cmd:set_bot:{instance_id}"), InlineKeyboardButton(text="🤖 一键下发绑定", callback_data=f"mg_cmd:bind_bot:{instance_id}")],
        [InlineKeyboardButton(text="🗑️ 彻底卸载 MG-UI", callback_data=f"mg_cmd:uninstall:{instance_id}")],
        [InlineKeyboardButton(text="🔙 返回上一级", callback_data=f"srv_sel:{instance_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=builder)

# ================= 🚀 1. 渲染主面板 (带状态嗅探) =================
@router.callback_query(F.data.startswith("run_sh:mgui:"))
async def show_mg_panel(call: CallbackQuery):
    try:
        parts = call.data.split(":")
        instance_id = parts[-1]
    except ValueError:
        return await call.answer("解析异常", show_alert=True)
        
    temp_msg = await call.message.edit_text("⏳ 正在探测服务器 MG-UI 环境状态，请稍候...", parse_mode="HTML")
    ip = await asyncio.to_thread(get_server_ip, instance_id)
    ip = ip or "未知IP"
    
    probe_script = "if [ -f /root/mg_panel.py ]; then echo 'INSTALLED'; else echo 'MISSING'; fi"
    try:
        probe_res = await execute_mg_hybrid(instance_id, call.from_user.id, probe_script)
        if "INSTALLED" in probe_res:
            is_installed = True
        elif "MISSING" in probe_res:
            is_installed = False
        else:
            return await temp_msg.edit_text(
                f"⚠️ <b>探测超时或被拒绝</b>\n\n未能成功连接到服务器 <code>{instance_id}</code>，底层回显：\n<code>{probe_res[:100]}</code>\n\n👉 请点击下方按钮重试。",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 重新探测", callback_data=f"run_sh:mgui:{instance_id}")]]),
                parse_mode="HTML"
            )
    except Exception as e:
        return await temp_msg.edit_text(
            f"⚠️ <b>连接服务器失败</b>\n\n可能遇到网络波动或底层服务未响应：\n<code>{str(e)[:100]}</code>\n\n👉 这不代表面板已卸载，请点击重试。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 重新连接", callback_data=f"run_sh:mgui:{instance_id}")]]),
            parse_mode="HTML"
        )
        
    if not is_installed:
        text = (
            f"🔴 <b>MG 私有化面板管控中心</b>\n\n🖥 <b>操作实例</b>：<code>{instance_id}</code> | 🌐 <b>IP</b>：<code>{ip}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n⚠️ <b>环境状态</b>：未检测到 MG-UI 核心组件\n━━━━━━━━━━━━━━━━━━\n"
            f"💡 <b>智能引导：</b>\n当前服务器为纯净状态。请点击下方「一键全新部署」按钮，系统将自动配置环境并拉起面板！"
        )
    else:
        shell_script = """
STATUS=$(systemctl is-active mg-panel 2>/dev/null || true)
BOT_STATUS=$(systemctl is-active mg-bot 2>/dev/null || true)
HAS_TOKEN=$(sqlite3 /root/mg_core.db "SELECT value FROM mg_settings WHERE key='bot_token'" 2>/dev/null)

if [ "$STATUS" = "active" ]; then echo "PANEL_STATUS=running"; else echo "PANEL_STATUS=stopped"; fi

if [ -n "$HAS_TOKEN" ] && [ "$HAS_TOKEN" != "None" ]; then
    if [ "$BOT_STATUS" = "active" ]; then
        echo "BOT=🟢 已绑定并运行中"
    else
        echo "BOT=🔴 已绑定但未启动"
    fi
else
    echo "BOT=未绑定 / 未运行"
fi

python3 -c "
import re
user, pwd, port = 'admin', 'admin', '8888'
try:
    with open('/root/mg_panel.py') as f:
        c = f.read()
        u = re.search(r'PANEL_USER\s*=\s*[\"\'](.+?)[\"\']', c); user = u.group(1) if u else user
        p = re.search(r'PANEL_PASS\s*=\s*[\"\'](.+?)[\"\']', c); pwd = p.group(1) if p else pwd
        pt = re.search(r'PANEL_PORT\s*=\s*(\d+)', c); port = pt.group(1) if pt else port
except: pass
print(f'PORT={port}\\nUSER={user}\\nPASS={pwd}')
"
"""
        info_res = await execute_mg_hybrid(instance_id, call.from_user.id, shell_script)
        data_map = {k.strip(): v.strip() for k, v in [line.split("=", 1) for line in info_res.split("\n") if "=" in line]}
        
        is_running = (data_map.get("PANEL_STATUS") == "running")
        status_text = "🟢 运行中 (Running)" if is_running else "🔴 已停止 (Stopped)"
        MG_CACHE[instance_id] = {"panel_status": data_map.get("PANEL_STATUS", "stopped"), "expire": time.time() + CACHE_TTL_SECONDS}
        
        login_url = f"http://{ip}:{data_map.get('PORT', '8888')}/"
        
        text = (
            f"🔴 <b>MG 私有化面板管控中心</b>\n\n🖥 <b>操作实例</b>：<code>{instance_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n🛡️ <b>运行状态</b>：{status_text}\n"
            f"🤖 <b>预警管家</b>：{data_map.get('BOT', '未绑定')}\n"
            f"🌐 <b>面板地址 (点击访问)</b>：\n<code>{login_url}</code>\n\n"
            f"👤 <b>账号</b>：<code>{data_map.get('USER', 'admin')}</code> | 🔑 <b>密码</b>：<code>{data_map.get('PASS', 'admin')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n💡 <b>核心指南</b>：\n• <b>极速节点</b>：一键生成专属协议，默认 500G 流量。\n• <b>节点管理</b>：针对不同端口进行节点调整、流量管控。"
        )
        
    await temp_msg.edit_text(text, reply_markup=build_mg_keyboard(instance_id, is_installed), parse_mode="HTML")
    try: await call.answer()
    except Exception: pass

# ================= 🚀 2. 核心路由与管理功能 =================
@router.callback_query(F.data.startswith("mg_cmd:"))
async def execute_mg_command(call: CallbackQuery, state: FSMContext):
    try: _, action, instance_id = call.data.split(":", 2)
    except ValueError: return await call.answer("解析异常", show_alert=True)
    
    ip = await asyncio.to_thread(get_server_ip, instance_id)
# ================= 🔄 批量一键重启并注入高并发 =================
    if action == "fix_all_mtp":
        wait_msg = await call.message.edit_text("⏳ 正在扫描底层数据库，准备批量清理僵尸进程并注入高并发权限...\n<i>(后台处理中，请稍候...)</i>", parse_mode="HTML")
        try: await call.answer("一键修复中...", show_alert=False)
        except: pass

        shell_script = """
cat << 'EOF' > /tmp/fix_mtp.py
import sqlite3, os
try:
    conn = sqlite3.connect('/root/mg_core.db')
    c = conn.cursor()
    c.execute("SELECT port, secret FROM mg_nodes")
    rows = c.fetchall()
    count = 0
    for r in rows:
        port, secret = r[0], r[1]
        os.system(f'pkill -9 -f "0.0.0.0:{port}" 2>/dev/null || true')
        cmd = f'nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} \'{secret}\'" >/dev/null 2>&1 &'
        os.system(cmd)
        count += 1
    print(f"FIX_OK|{count}")
except Exception as e:
    print(f"FIX_ERR|{str(e)}")
EOF
python3 /tmp/fix_mtp.py
rm -f /tmp/fix_mtp.py
"""
        try:
            out = await asyncio.wait_for(execute_mg_hybrid(instance_id, call.from_user.id, shell_script), timeout=180.0)
            if "FIX_OK" in out:
                count = [line.split("|")[1] for line in out.split('\n') if line.startswith("FIX_OK|")][0]
                await wait_msg.edit_text(
                    f"✅ <b>一键修复/重启完成！</b>\n\n"
                    f"🖥 <b>实例</b>：<code>{instance_id}</code>\n"
                    f"🛠 <b>处理结果</b>：已强制清理并使用超高并发权限重新拉起了 <b>{count}</b> 个 MTProto 节点。\n"
                    f"💡 <i>此操作已生效，节点假死与断流问题已被强制修复。</i>",
                    reply_markup=build_mg_keyboard(instance_id), parse_mode="HTML"
                )
            else:
                await wait_msg.edit_text(f"⚠️ <b>修复异常：</b>\n回显：<code>{out[:100]}</code>", reply_markup=build_mg_keyboard(instance_id), parse_mode="HTML")
        except Exception as e:
            await wait_msg.edit_text(f"❌ <b>修复失败：</b>\nSSH底层执行异常: <code>{str(e)}</code>", reply_markup=build_mg_keyboard(instance_id), parse_mode="HTML")
        return
    # ================= ⚡ 一键极速生成 MTP 节点 =================
    if action == "add_mtp_quick":
        wait_msg = await call.message.edit_text("⏳ 正在分配随机端口并生成 MTP 节点，配置 500GB 限额...\n<i>(后台处理中，请稍候...)</i>", parse_mode="HTML")
        try: await call.answer("节点生成中...", show_alert=False)
        except: pass

        port = random.randint(10000, 60000)
        today_day = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).day
        
        # ✅ 修复：添加 nohup 绝对持久化
        shell_script = f"""
python3 -c "
import sqlite3, datetime, subprocess
port = {port}; limit_gb = 500.0
try:
    secret = subprocess.check_output('/usr/local/bin/mg generate-secret --hex icloud.com', shell=True).decode().strip()
except:
    secret = 'ee' + 'a' * 30 + '69636c6f75642e636f6d'

conn = sqlite3.connect('/root/mg_core.db')
c = conn.cursor()
import calendar
# 强制使用 UTC+8 (东八区) 时间
now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
m = now.month + 1; y = now.year
if m > 12: m = 1; y += 1
d = min(now.day, calendar.monthrange(y, m)[1])
exp_date = now.replace(year=y, month=m, day=d).strftime('%Y-%m-%d %H:%M:%S')
c.execute('INSERT INTO mg_nodes (port, secret, limit_gb, used_bytes, status, reset_cycle, expiry_date) VALUES (?, ?, ?, 0, ?, ?, ?)', (port, secret, limit_gb, 'running', 'monthly', exp_date))
conn.commit(); conn.close()
print(f'MTP_RES:{{port}}|{{secret}}|{{exp_date}}')
"
iptables -C OUTPUT -p tcp --sport {port} 2>/dev/null || iptables -I OUTPUT -p tcp --sport {port}
nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} $(sqlite3 /root/mg_core.db 'SELECT secret FROM mg_nodes WHERE port={port}')" >/dev/null 2>&1 &
"""
        try:
            out = await asyncio.wait_for(execute_mg_hybrid(instance_id, call.from_user.id, shell_script), timeout=180.0)
            if "MTP_RES:" not in out: raise Exception(f"底层调度异常: {out[:80]}")
            
            port_res, secret, exp_date_str = "", "", ""
            for line in out.split("\n"):
                if line.startswith("MTP_RES:"):
                    port_res, secret, exp_date_str = line.split(":", 1)[1].split("|")
            
            mtp_link = f"tg://proxy?server={ip}&port={port_res}&secret={secret}"
            
            await wait_msg.edit_text(
                f"🎉 <b>MG 节点生成成功！</b>\n\n🖥 <b>实例</b>：<code>{instance_id}</code>\n🔌 <b>分配端口</b>：<code>{port_res}</code>\n"
                f"📊 <b>流量配额</b>：<b>500 GB</b> (每月 {today_day} 号重置)\n"
                f"📅 <b>到期时间</b>：<b>{exp_date_str}</b>\n\n🚀 <b>专属订阅链接 (点击自动唤起 TG 连接)：</b>\n<code>{mtp_link}</code>",
                reply_markup=build_mg_keyboard(instance_id), parse_mode="HTML"
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ <b>节点创建失败：</b>\n{str(e)}", reply_markup=build_mg_keyboard(instance_id), parse_mode="HTML")
        return

    # ================= 📋 节点列表与抽屉 =================
    async def render_mg_port_list(message: Message, inst_id: str, u_id: int):
        msg = await message.edit_text("⏳ 正在拉取底层节点大盘数据...", parse_mode="HTML")
        shell_script = """
python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('/root/mg_core.db')
    c = conn.cursor()
    c.execute('SELECT port, limit_gb, used_bytes, expiry_date, status FROM mg_nodes')
    rows = c.fetchall()
    for r in rows:
        ub = r[2] if r[2] is not None else 0
        print(f'NODE:{r[0]}|{r[1]}|{ub}|{r[3]}|{r[4]}')
    conn.close()
except:
    pass
"
"""
        try:
            out = await execute_mg_hybrid(inst_id, u_id, shell_script)
            buttons = []
            for line in out.split("\n"):
                if line.startswith("NODE:"):
                    try:
                        p, lim, used_b, exp, st = line.replace("NODE:", "").split("|")
                        used_b_float = float(used_b)
                        
                        if used_b_float < 1024**2:
                            traffic_display = f"{used_b_float/1024:.1f}K"
                        elif used_b_float < 1024**3:
                            traffic_display = f"{used_b_float/(1024**2):.1f}M"
                        else:
                            traffic_display = f"{used_b_float/(1024**3):.1f}G"
                            
                        lim_gb = float(lim)
                        status_icon = "🟢" if st == "running" else "🔴"
                        btn_text = f"{status_icon} 端口 {p} | 流量:{traffic_display}/{lim_gb:.0f}G | 到期:{exp[:10]}"
                        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"mg_cmd:port_ctrl-{p}:{inst_id}")])
                    except: pass
            
            buttons.append([InlineKeyboardButton(text="🔙 返回主控制台", callback_data=f"run_sh:mgui:{inst_id}")])
            
            if not buttons[:-1]:
                await msg.edit_text("📋 <b>MG-UI 节点流量大盘</b>\n\n当前尚未创建任何节点。", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
            else:
                await msg.edit_text("📋 <b>MG-UI 节点流量大盘</b>\n\n👇 点击下方任意端口，展开管控抽屉：", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"❌ 数据拉取失败：\n{str(e)}", parse_mode=None)

    # ================= 🛠️ 自定义生成 MTP 节点 =================
    if action == "add_mtp_custom":
        await state.update_data(instance_id=instance_id)
        await state.set_state(MguiCustomNodeFSM.wait_for_port)
        await call.message.answer(
            "🛠️ **生成自定义节点 (1/3)**\n\n"
            "🌐 请输入您想要设置的**端口号** (范围 1-65535)：\n"
            "*(回复 0 取消操作)*", 
            parse_mode="Markdown"
        )
        return await call.answer()

    if action == "port_list":
        await render_mg_port_list(call.message, instance_id, call.from_user.id)
        try: return await call.answer()
        except: return

    # ================= 🎛 单个端口专属管控抽屉 =================
    if action.startswith("port_ctrl-"):
        port = action.split("-")[1]
        await call.message.edit_text(f"⏳ 正在查询端口 <code>{port}</code> 的实时流量与详情...", parse_mode="HTML")
        
        script = f"""
python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('/root/mg_core.db')
    c = conn.cursor()
    c.execute('SELECT limit_gb, used_bytes, expiry_date, status FROM mg_nodes WHERE port={port}')
    row = c.fetchone()
    if row:
        used_b = float(row[1] if row[1] else 0)
        
        if used_b < 1024**2:
            used_str = f'{{used_b/1024:.2f}} KB'
        elif used_b < 1024**3:
            used_str = f'{{used_b/(1024**2):.2f}} MB'
        else:
            used_str = f'{{used_b/(1024**3):.2f}} GB'
            
        limit_str = '不限' if row[0] == 0 else f'{{row[0]:.0f}} GB'
        
        print(f'INFO:{{limit_str}}|{{used_str}}|{{row[2]}}|{{row[3]}}')
    conn.close()
except: pass
"
"""
        try:
            out = await execute_mg_hybrid(instance_id, call.from_user.id, script)
            info_str = ""
            for line in out.split("\n"):
                if line.startswith("INFO:"):
                    info_str = line.replace("INFO:", "")
            
            if info_str:
                limit_gb, used_gb, exp_date, status = info_str.split("|")
                status_cn = "🟢 正常运行" if status == "running" else f"🔴 {status}"
                detail_text = (
                    f"🎛 <b>专属端口管控台：<code>{port}</code></b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💡 <b>运行状态</b>：{status_cn}\n"
                    f"📊 <b>流量配额</b>：已用 <b>{used_gb}</b> / 总额 {limit_gb}\n"
                    f"📅 <b>到期时间</b>：<code>{exp_date}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👇 请选择你要对该节点执行的操作："
                )
            else:
                detail_text = f"🎛 <b>专属端口管控台：<code>{port}</code></b>\n\n⚠️ (未能获取到详细数据，节点可能已被删除)\n\n👇 请选择你要执行的操作："
                
        except Exception:
            detail_text = f"🎛 <b>专属端口管控台：<code>{port}</code></b>\n\n⚠️ (查询详情超时，请稍后重试)\n\n👇 请选择你要执行的操作："

        buttons = [
            [InlineKeyboardButton(text="🔗 获取该节点专属分享链接", callback_data=f"mg_cmd:port_link-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🔄 更换随机密钥", callback_data=f"mg_cmd:port_rand_sec-{port}:{instance_id}"),
             InlineKeyboardButton(text="✍️ 更换指定密钥", callback_data=f"mg_cmd:port_cust_sec-{port}:{instance_id}")],
            [InlineKeyboardButton(text="📢 绑定 MTP 置顶广告 (Ad Tag)", callback_data=f"mg_cmd:port_ad_tag-{port}:{instance_id}")],
            [InlineKeyboardButton(text="💰 续费该节点 (延长1个月)", callback_data=f"mg_cmd:port_renew-{port}:{instance_id}"),
             InlineKeyboardButton(text="🔄 强制清零已用流量", callback_data=f"mg_cmd:port_reset-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🗑️ 彻底删除此节点 (不可逆)", callback_data=f"mg_cmd:port_del-{port}:{instance_id}")],
            [InlineKeyboardButton(text="🔙 返回节点列表", callback_data=f"mg_cmd:port_list:{instance_id}")]
        ]
        
        await call.message.edit_text(detail_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        return await call.answer()

    if action.startswith("port_link-"):
        port = action.split("-")[1]
        
        script = f"""
python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('/root/mg_core.db')
    c = conn.cursor()
    c.execute('SELECT limit_gb, used_bytes, expiry_date, status, reset_cycle, secret FROM mg_nodes WHERE port={port}')
    row = c.fetchone()
    if row:
        used_b = float(row[1] if row[1] else 0)
        
        if used_b < 1024**2:
            used_str = f'{{used_b/1024:.2f}} KB'
        elif used_b < 1024**3:
            used_str = f'{{used_b/(1024**2):.2f}} MB'
        else:
            used_str = f'{{used_b/(1024**3):.2f}} GB'
            
        limit_str = '不限' if row[0] == 0 else f'{{row[0]:.1f}} GB'
        
        print(f'LINK_INFO:{{limit_str}}|{{used_str}}|{{row[2][:10]}}|{{row[3]}}|{{row[4]}}|{{row[5]}}')
    conn.close()
except: pass
"
"""
        out = await execute_mg_hybrid(instance_id, call.from_user.id, script)
        
        link_info = ""
        for line in out.split("\n"):
            if line.startswith("LINK_INFO:"):
                link_info = line.replace("LINK_INFO:", "")
        
        if link_info:
            limit_gb, used_str, exp_date, status, reset_cycle, secret = link_info.split("|")
            status_cn = "🟢 运行中" if status == "running" else f"🔴 {status}"
            
            buttons = [
                [InlineKeyboardButton(text="🔄 更换随机密钥", callback_data=f"mg_cmd:port_rand_sec-{port}:{instance_id}"),
                 InlineKeyboardButton(text="✍️ 更换指定密钥", callback_data=f"mg_cmd:port_cust_sec-{port}:{instance_id}")],
                [InlineKeyboardButton(text="📢 绑定 MTP 置顶广告 (Ad Tag)", callback_data=f"mg_cmd:port_ad_tag-{port}:{instance_id}")],
                [InlineKeyboardButton(text="💰 续费该节点 (延长1个月)", callback_data=f"mg_cmd:port_renew-{port}:{instance_id}"),
                 InlineKeyboardButton(text="🔄 强制清零已用流量", callback_data=f"mg_cmd:port_reset-{port}:{instance_id}")],
                [InlineKeyboardButton(text="🗑️ 彻底删除此节点 (不可逆)", callback_data=f"mg_cmd:port_del-{port}:{instance_id}")],
                [InlineKeyboardButton(text="🔙 返回节点列表", callback_data=f"mg_cmd:port_list:{instance_id}")]
            ]
            
            text = (
                f"📄 <b>节点详情</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🖥 <b>IP:</b> <code>{ip}</code>\n"
                f"🔌 <b>端口:</b> <code>{port}</code>\n"
                f"🕒 <b>到期:</b> <code>{exp_date}</code>\n"
                f"📊 <b>流量:</b> {used_str} / {limit_gb}\n"
                f"♻️ <b>重置:</b> {reset_cycle}\n"
                f"📈 <b>状态:</b> {status_cn}\n\n"
                f"🔑 <b>密钥:</b>\n<code>{secret}</code>\n\n"
                f"🔗 <b>链接:</b>\n<code>tg://proxy?server={ip}&port={port}&secret={secret}</code>"
            )
            await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
        else:
            await call.answer("解析失败，未找到该节点详细信息", show_alert=True)
        return

    if action.startswith("port_renew-"):
        port = action.split("-")[1]
        # ✅ 修复：添加 nohup
        script = f"""
python3 -c "
import sqlite3, datetime, calendar
conn = sqlite3.connect('/root/mg_core.db')
c = conn.cursor()
c.execute('SELECT expiry_date FROM mg_nodes WHERE port={port}')
row = c.fetchone()
if row and row[0]:
    # 强制获取当前的 UTC+8 时间作为基准
    real_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    try: dt = datetime.datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
    except: dt = real_now
    if dt < real_now: dt = real_now
    m = dt.month + 1; y = dt.year
    if m > 12: m = 1; y += 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    new_dt = dt.replace(year=y, month=m, day=d).strftime('%Y-%m-%d %H:%M:%S')
    c.execute('UPDATE mg_nodes SET expiry_date=?, status=\\'running\\' WHERE port=?', (new_dt, {port}))
    conn.commit()
    print('RENEW_OK')
    conn.close()
    "
    nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} $(sqlite3 /root/mg_core.db 'SELECT secret FROM mg_nodes WHERE port={port}')" >/dev/null 2>&1 &
    """
        await execute_mg_hybrid(instance_id, call.from_user.id, script)
        await call.answer(f"✅ 端口 {port} 已成功续费 1 个自然月！", show_alert=True)
        return await render_mg_port_list(call.message, instance_id, call.from_user.id)

    if action.startswith("port_rand_sec-"):
        port = action.split("-")[1]
        # ✅ 修复：添加 nohup
        script = f"""
python3 -c "
import sqlite3, subprocess, random
try:
    secret = subprocess.check_output('/usr/local/bin/mg generate-secret --hex icloud.com', shell=True).decode().strip()
except:
    secret = 'ee' + ''.join(random.choices('0123456789abcdef', k=30)) + '69636c6f75642e636f6d'
conn = sqlite3.connect('/root/mg_core.db')
c = conn.cursor()
c.execute('UPDATE mg_nodes SET secret=? WHERE port=?', (secret, {port}))
conn.commit(); conn.close()
"
bash /root/mg_executor.sh delete {port}
# 🌟 新增：确保老密钥进程死透，再启动新密钥进程
pkill -9 -f "0.0.0.0:{port}" 2>/dev/null || true
nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} $(sqlite3 /root/mg_core.db 'SELECT secret FROM mg_nodes WHERE port={port}')" >/dev/null 2>&1 &
echo 'RAND_SEC_OK'
"""
        await execute_mg_hybrid(instance_id, call.from_user.id, script)
        await call.answer(f"✅ 端口 {port} 的密钥已随机更换并重启！", show_alert=True)
        
        return await call.message.edit_text(
            f"✅ <b>密钥重置成功！</b>\n\n端口 <code>{port}</code> 的密钥已随机更换。\n👉 请点击下方按钮返回控制台，重新获取最新链接。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🔙 返回端口 {port} 管控台", callback_data=f"mg_cmd:port_ctrl-{port}:{instance_id}")]
            ]),
            parse_mode="HTML"
        )

    if action.startswith("port_cust_sec-"):
        port = action.split("-")[1]
        await state.update_data(bind_instance_id=instance_id, bind_port=port)
        await state.set_state(MguiPortFSM.wait_for_custom_secret)
        await call.message.answer(
            f"✍️ <b>更换端口 <code>{port}</code> 的指定密钥</b>\n\n"
            f"请回复您要设置的新密钥 (Secret)。\n"
            f"<i>⚠️ 建议使用 ee 开头的 TLS 伪装密钥（32位以上十六进制字符）。</i>\n"
            f"(回复 0 取消操作)",
            parse_mode="HTML"
        )
        return await call.answer()

    if action.startswith("port_ad_tag-"):
        port = action.split("-")[1]
        
        script = f"sqlite3 /root/mg_core.db 'SELECT secret FROM mg_nodes WHERE port={port}'"
        out = await execute_mg_hybrid(instance_id, call.from_user.id, script)
        secret = out.strip()
        
        core_hex = secret[2:34] if secret.startswith("ee") and len(secret) > 34 else secret
        
        await state.update_data(bind_instance_id=instance_id, bind_port=port)
        await state.set_state(MguiPortFSM.wait_for_ad_tag)
        await call.message.answer(
            f"📢 <b>绑定端口 <code>{port}</code> 的置顶广告频道</b>\n\n"
            f"🤖 @MTProxybot 注册需要用到当前端口的密钥 (Secret)。\n"
            f"🔑 <b>完整密钥 (点击复制)：</b>\n<code>{secret}</code>\n"
            f"📌 <b>纯 16 进制密钥</b> <i>(若官方 Bot 提示格式不对，请复制这串)</i>：\n<code>{core_hex}</code>\n\n"
            f"1️⃣ 请前往 Telegram 官方 @MTProxybot，发送服务器 IP、端口 <code>{port}</code> 和上方密钥进行注册，获取 <b>Ad Tag</b>。\n"
            f"2️⃣ 将获取到的 Ad Tag (通常为 32 位字符) 发送给我：\n\n"
            f"(回复 0 取消操作)",
            parse_mode="HTML"
        )
        return await call.answer()

    if action.startswith("port_reset-"):
        port = action.split("-")[1]
        script = f"""
sqlite3 /root/mg_core.db "UPDATE mg_nodes SET used_bytes=0 WHERE port={port}"
iptables -D OUTPUT -p tcp --sport {port} 2>/dev/null || true
iptables -I OUTPUT -p tcp --sport {port}
echo 'RST_OK'
"""
        await execute_mg_hybrid(instance_id, call.from_user.id, script)
        await call.answer(f"✅ 端口 {port} 的已用流量已强行清零！", show_alert=True)
        return await render_mg_port_list(call.message, instance_id, call.from_user.id)

    if action.startswith("port_del-"):
        port = action.split("-")[1]
        script = f"""
bash /root/mg_executor.sh delete {port}
# 🌟 新增：强制物理超度僵尸进程，无视 PID 文件错误
pkill -9 -f "0.0.0.0:{port}" 2>/dev/null || true
# 🌟 新增：双向清理防火墙规则
iptables -D OUTPUT -p tcp --sport {port} 2>/dev/null || true
iptables -D INPUT -p tcp --dport {port} 2>/dev/null || true
sqlite3 /root/mg_core.db "DELETE FROM mg_nodes WHERE port={port}"
echo 'DEL_OK'
"""
        await execute_mg_hybrid(instance_id, call.from_user.id, script)
        await call.answer(f"🗑️ 端口 {port} 节点已彻底销毁！", show_alert=True)
        return await render_mg_port_list(call.message, instance_id, call.from_user.id)

    if action == "set_bot":
        await state.update_data(bind_instance_id=instance_id)
        await state.set_state(MguiBindBotFSM.wait_for_custom_token)
        await call.message.answer(
            f"🤖 <b>配置全局预警 Bot 模板</b>\n\n"
            f"此操作将把预警机器人信息保存在主控系统中。\n后续在任何服务器点击【一键下发绑定】均会使用此配置。\n\n"
            f"👉 请回复您在 @BotFather 申请的<b>全新预警 Bot Token</b>：\n<i>(发送 0 取消操作)</i>",
            parse_mode="HTML"
        )
        return await call.answer()

    if action == "bind_bot":
        db_path = getattr(config, 'DB_PATH', '/srv/aali/bot_data.db')
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT token, admin_id FROM mg_global_bot LIMIT 1")
            row = c.fetchone()
            conn.close()
        except:
            row = None
            
        if not row or not row[0]:
            return await call.answer("⚠️ 您还未配置过全局预警 Bot！\n\n请先点击旁边的【设置全局预警 Bot】填写凭证。", show_alert=True)
            
        token, admin_id = row[0], row[1]
        msg_tip = await call.message.edit_text(f"⏳ 正在向实例下发专属预警 Bot 凭证并唤醒管家...", parse_mode="HTML")
        script = f"""
python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('/root/mg_core.db')
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS mg_settings (key TEXT PRIMARY KEY, value TEXT)')
    c.execute('REPLACE INTO mg_settings (key, value) VALUES (?, ?)', ('bot_token', '{token}'))
    c.execute('REPLACE INTO mg_settings (key, value) VALUES (?, ?)', ('admin_id', '{admin_id}'))
    conn.commit()
    conn.close()
except: pass
"
systemctl enable mg-bot
systemctl restart mg-bot
echo 'SUCCESS'
"""
        await execute_mg_hybrid(instance_id, call.from_user.id, script)
        await call.answer("✅ 一键下发成功！预警管家已上线。", show_alert=True)
        return await show_mg_panel(call)

    # ================= ⚙️ 基础面板管控 (安装/卸载/启停) =================
    msg_tip = await call.message.edit_text(f"⏳ 正在向实例下发 <code>{action}</code> 指令...\n<i>(后台静默执行中，请耐心等待)</i>", parse_mode="HTML")
    try: await call.answer("指令已开始在后台执行...", show_alert=False)
    except: pass

    if action == "install": 
        shell_script = """
apt-get install -y sqlite3 curl
bash <(curl -sL https://raw.githubusercontent.com/alnawei/sh/main/MG-UI/install.sh)
systemctl stop mg-bot 2>/dev/null || true
systemctl disable mg-bot 2>/dev/null || true
sqlite3 /root/mg_core.db "DELETE FROM mg_settings WHERE key IN ('bot_token', 'admin_id')" 2>/dev/null || true
systemctl enable mg-panel
systemctl restart mg-panel
"""
    elif action == "start": 
        shell_script = "systemctl start mg-panel && echo 'SUCCESS'"
    elif action == "stop": 
        shell_script = "systemctl stop mg-panel && echo 'SUCCESS'"
    elif action == "reset_pass": 
        shell_script = """
sed -i 's/^ADMIN_USER = .*/ADMIN_USER = "admin"/' /root/mg_panel.py
sed -i 's/^ADMIN_PASS = .*/ADMIN_PASS = "admin"/' /root/mg_panel.py
sed -i 's/^WEB_PORT = .*/WEB_PORT = 8888/' /root/mg_panel.py
systemctl restart mg-panel
echo 'RESET_SUCCESS'
"""
    elif action == "uninstall": 
        shell_script = "bash <(curl -sL https://raw.githubusercontent.com/alnawei/sh/main/MG-UI/uninstall.sh) && echo 'UNINSTALL_SUCCESS'"
    else: 
        shell_script = "echo 'Unknown command'"

    try:
        await execute_mg_hybrid(instance_id, call.from_user.id, shell_script)
        if action in ["start", "stop", "reset_pass", "install", "uninstall"]:
            if action in ["start", "install", "reset_pass"]:
                MG_CACHE[instance_id] = {"panel_status": "running", "expire": time.time() + CACHE_TTL_SECONDS}
            elif action in ["stop", "uninstall"]:
                MG_CACHE[instance_id] = {"panel_status": "stopped", "expire": time.time() + CACHE_TTL_SECONDS}
                
            await msg_tip.edit_text(f"🎉 <b>指令执行成功！</b>\n\n即将刷新面板状态...", parse_mode="HTML")
            await asyncio.sleep(2.5) 
            return await show_mg_panel(call)
    except Exception as e:
        await msg_tip.edit_text(f"❌ 执行失败：\n{str(e)}", parse_mode=None)


# ==========================================================
# ============ 🛠️ 自定义节点生成逻辑 (FSM分步) ============
# ==========================================================

# ✅ 修复：拦截非文本消息，防止崩溃
@router.message(MguiCustomNodeFSM.wait_for_port, F.text)
async def custom_node_port(message: Message, state: FSMContext):
    port_text = message.text.strip()
    if port_text == '0':
        await state.clear()
        return await message.answer("已取消自定义节点生成。")
        
    if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
        return await message.answer("❌ 端口格式错误，请输入 1 到 65535 之间的纯数字：")

    await state.update_data(port=port_text)
    await state.set_state(MguiCustomNodeFSM.wait_for_pwd)
    
    await message.answer(
        f"✅ 端口 `{port_text}` 已记录。\n\n"
        "🔑 **(2/3)** 请输入该节点的**专属密码 (UUID / 密钥)**：\n"
        "*(回复 0 取消)*", 
        parse_mode="Markdown"
    )

@router.message(MguiCustomNodeFSM.wait_for_pwd, F.text)
async def custom_node_pwd(message: Message, state: FSMContext):
    pwd_text = message.text.strip()
    if pwd_text == '0':
        await state.clear()
        return await message.answer("已取消操作。")

    await state.update_data(pwd=pwd_text)
    await state.set_state(MguiCustomNodeFSM.wait_for_traffic)
    
    await message.answer(
        f"✅ 密码已记录。\n\n"
        "📶 **(3/3)** 请输入该节点的**流量限制 (GB)**：\n"
        "*(例如输入 500 代表 500G，输入 0 代表不限流；回复 00 取消操作)*", 
        parse_mode="Markdown"
    )

@router.message(MguiCustomNodeFSM.wait_for_traffic, F.text)
async def custom_node_traffic(message: Message, state: FSMContext):
    traffic_text = message.text.strip()
    if traffic_text == '00':
        await state.clear()
        return await message.answer("已取消操作。")
        
    if not traffic_text.isdigit():
        return await message.answer("❌ 流量格式错误，请输入纯数字 (GB)：")

    data = await state.get_data()
    instance_id = data['instance_id']
    port = data['port']
    pwd = data['pwd']
    traffic_gb = float(traffic_text)
    
    await state.clear()
    wait_msg = await message.answer(f"🔄 **正在向实例下发自定义节点...**\n⏳ 端口: `{port}` | 流量: `{traffic_gb} GB`", parse_mode="Markdown")

    ip = await asyncio.to_thread(get_server_ip, instance_id)
    today_day = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).day
    
    # ✅ 修复：加入 nohup 持久化
    shell_script = f"""
python3 -c "
import sqlite3, datetime
port = {port}; limit_gb = {traffic_gb}; secret = '{pwd}'

conn = sqlite3.connect('/root/mg_core.db')
c = conn.cursor()
import calendar
# 强制使用 UTC+8 (东八区) 时间
now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
m = now.month + 1; y = now.year
if m > 12: m = 1; y += 1
d = min(now.day, calendar.monthrange(y, m)[1])
exp_date = now.replace(year=y, month=m, day=d).strftime('%Y-%m-%d %H:%M:%S')

c.execute('DELETE FROM mg_nodes WHERE port=?', (port,))
c.execute('INSERT INTO mg_nodes (port, secret, limit_gb, used_bytes, status, reset_cycle, expiry_date) VALUES (?, ?, ?, 0, ?, ?, ?)', (port, secret, limit_gb, 'running', 'monthly', exp_date))
conn.commit(); conn.close()
print(f'MTP_RES:{{port}}|{{secret}}|{{exp_date}}')
"
iptables -C OUTPUT -p tcp --sport {port} 2>/dev/null || iptables -I OUTPUT -p tcp --sport {port}
nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} '{pwd}'" >/dev/null 2>&1 &
"""

    try:
        # ✅ 修复：修改 call.from_user.id 为 message.from_user.id，避免静默崩溃
        out = await asyncio.wait_for(execute_mg_hybrid(instance_id, message.from_user.id, shell_script), timeout=180.0)
        
        if "MTP_RES:" not in out: 
            raise Exception(f"底层调度异常: {out[:80]}")
            
        port_res, secret, exp_date_str = "", "", ""
        for line in out.split("\n"):
            if line.startswith("MTP_RES:"):
                port_res, secret, exp_date_str = line.split(":", 1)[1].split("|")
        
        mtp_link = f"tg://proxy?server={ip}&port={port_res}&secret={secret}"
        traffic_display = "不限流量" if traffic_gb == 0 else f"{traffic_gb} GB"
        
        await wait_msg.edit_text(
            f"🎉 <b>自定义节点生成成功！</b>\n\n"
            f"🖥 <b>实例</b>：<code>{instance_id}</code>\n"
            f"🔌 <b>分配端口</b>：<code>{port_res}</code>\n"
            f"🔑 <b>节点密钥</b>：<code>{secret}</code>\n"
            f"📊 <b>流量配额</b>：<b>{traffic_display}</b> (每月 {today_day} 号重置)\n"
            f"📅 <b>到期时间</b>：<b>{exp_date_str}</b>\n\n"
            f"🚀 <b>专属订阅链接 (点击自动唤起 TG 连接)：</b>\n<code>{mtp_link}</code>",
            reply_markup=build_mg_keyboard(instance_id), 
            parse_mode="HTML"
        )
        
    except Exception as e:
        await wait_msg.edit_text(
            f"❌ <b>节点创建失败：</b>\n<code>{str(e)}</code>", 
            reply_markup=build_mg_keyboard(instance_id), 
            parse_mode="HTML"
        )

# ================= 🚀 3. FSM：接收全局预警 Bot 绑定 =================
@router.message(MguiBindBotFSM.wait_for_custom_token, F.text)
async def mgui_bind_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if token == '0':
        await state.clear()
        return await message.answer("已取消操作。")
        
    await state.update_data(bot_token=token)
    await state.set_state(MguiBindBotFSM.wait_for_custom_admin)
    await message.answer("👤 <b>请输入接收告警的 Admin ID (您的 TG 数字ID)：</b>", parse_mode="HTML")

@router.message(MguiBindBotFSM.wait_for_custom_admin, F.text)
async def mgui_bind_admin(message: Message, state: FSMContext):
    admin_id = message.text.strip()
    data = await state.get_data()
    token = data.get('bot_token')
    instance_id = data.get('bind_instance_id')
    await state.clear()
    
    # ✅ 修复：将同步 I/O 移入子线程，防止阻塞
    def _save_db():
        db_path = getattr(config, 'DB_PATH', '/srv/aali/bot_data.db')
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS mg_global_bot (id INTEGER PRIMARY KEY, token TEXT, admin_id TEXT)")
        c.execute("DELETE FROM mg_global_bot")
        c.execute("INSERT INTO mg_global_bot (id, token, admin_id) VALUES (1, ?, ?)", (token, admin_id))
        conn.commit()
        conn.close()

    try:
        await asyncio.to_thread(_save_db)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 返回面板控制台", callback_data=f"run_sh:mgui:{instance_id}")]
        ])
        
        await message.answer(
            "✅ <b>全局预警 Bot 模板已安全保存！</b>\n\n"
            "此配置仅存在于主控数据库中。\n"
            "👉 您现在可以返回控制台，点击<b>【🤖 一键下发绑定】</b>将其部署到该服务器上。",
            reply_markup=keyboard, parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ 模板保存失败：{str(e)}")

# ================= 🚀 4. FSM：端口高级属性配置 =================
@router.message(MguiPortFSM.wait_for_custom_secret, F.text)
async def mgui_set_custom_secret(message: Message, state: FSMContext):
    secret = message.text.strip()
    if secret == '0':
        await state.clear()
        return await message.answer("已取消修改密钥。")
        
    data = await state.get_data()
    instance_id = data.get('bind_instance_id')
    port = data.get('bind_port')
    await state.clear()
    
    wait_msg = await message.answer(f"⏳ 正在为端口 <code>{port}</code> 写入自定义密钥并重启...", parse_mode="HTML")
    
    # ✅ 修复：加入 pkill 和 nohup 持久化
    script = f"""
sqlite3 /root/mg_core.db "UPDATE mg_nodes SET secret='{secret}' WHERE port={port}"
bash /root/mg_executor.sh delete {port}
# 🌟 新增：强制超度
pkill -9 -f "0.0.0.0:{port}" 2>/dev/null || true
nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} '{secret}'" >/dev/null 2>&1 &
echo 'SET_SEC_OK'
"""
    try:
        await execute_mg_hybrid(instance_id, message.from_user.id, script)
        await wait_msg.edit_text(f"✅ <b>密钥修改成功！</b>\n\n端口 <code>{port}</code> 已使用新密钥重启。\n请返回控制台重新获取直连链接。", parse_mode="HTML")
    except Exception as e:
        await wait_msg.edit_text(f"❌ 修改失败：\n{str(e)}")

@router.message(MguiPortFSM.wait_for_ad_tag, F.text)
async def mgui_set_ad_tag(message: Message, state: FSMContext):
    ad_tag = message.text.strip()
    if ad_tag == '0':
        await state.clear()
        return await message.answer("已取消绑定广告。")
        
    data = await state.get_data()
    instance_id = data.get('bind_instance_id')
    port = data.get('bind_port')
    await state.clear()
    
    wait_msg = await message.answer(f"⏳ 正在为端口 <code>{port}</code> 注入 Ad Tag 广告凭证...", parse_mode="HTML")
    
    # ✅ 修复：加入 nohup 持久化
    script = f"""
python3 -c "
import sqlite3
conn = sqlite3.connect('/root/mg_core.db')
c = conn.cursor()
try: c.execute('ALTER TABLE mg_nodes ADD COLUMN ad_tag TEXT')
except: pass
c.execute('UPDATE mg_nodes SET ad_tag=? WHERE port=?', ('{ad_tag}', {port}))
conn.commit(); conn.close()
"
bash /root/mg_executor.sh delete {port}
# 🌟 新增：强制超度
pkill -9 -f "0.0.0.0:{port}" 2>/dev/null || true
nohup bash -c "ulimit -n 65535 2>/dev/null; bash /root/mg_executor.sh start {port} $(sqlite3 /root/mg_core.db 'SELECT secret FROM mg_nodes WHERE port={port}') '{ad_tag}'" >/dev/null 2>&1 &
echo 'SET_AD_OK'
"""
    try:
        await execute_mg_hybrid(instance_id, message.from_user.id, script)
        await wait_msg.edit_text(
            f"📢 <b>广告 Tag 绑定下发成功！</b>\n\n"
            f"已将凭证 <code>{ad_tag}</code> 挂载至端口 <code>{port}</code>。\n"
            f"<i>注：置顶广告的生效需要 @MTProxybot 端的审核，通常存在几分钟的延迟。</i>", 
            parse_mode="HTML"
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ 绑定失败：\n{str(e)}")
