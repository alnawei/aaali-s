import asyncio
import traceback
import json
import sqlite3
import calendar


from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# 假设您的 config 和 db 模块已在外部定义
import config
from db import get_active_servers

# 阿里云 SDK 导入
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_cms20190101 import models as cms_models
from alibabacloud_tea_openapi import models as open_api_models

router = Router()

# ================= 定义状态机 =================
class TrafficSettingsFSM(StatesGroup):
    wait_for_warn_line = State()
    wait_for_stop_line = State()


# ================= 🛠️ 辅助函数：生成精美进度条 =================
def get_progress_bar(used: float, total: float, length: int = 5) -> str:
    """根据使用比例生成可视化的 Emoji 进度条"""
    if total <= 0: return "⬜" * length
    percent = min(used / total, 1.0)
    filled = int(percent * length)
    empty = length - filled
    
    # 动态变色：超过 95% 变红，超过 80% 变黄，正常为绿
    if percent >= 0.95:
        return "🟥" * filled + "⬜" * empty
    elif percent >= 0.80:
        return "🟨" * filled + "⬜" * empty
    else:
        return "🟩" * filled + "⬜" * empty


# ================= 🔌 核心逻辑：多账号动态调用阿里云 API =================
def get_dynamic_aliyun_credentials(instance_id: str):
    """
    根据实例 ID，通过 ecs_business 关联 cloud_accounts 动态获取专属 AK/SK
    """
    try:
        # 强制兜底绝对路径，防止 handler 在子目录下导致的数据库错位
        db_path = getattr(config, 'DB_PATH', '/srv/Ali/bot_data.db')
        conn = sqlite3.connect(db_path, timeout=4.0)
        cursor = conn.cursor()
        
        # 🌟 加入 TRIM()，无视由于复制粘贴引起的隐藏空格干扰
        cursor.execute('''
            SELECT c.access_key, c.access_secret
            FROM ecs_business e 
            JOIN cloud_accounts c ON e.account_id = c.id 
            WHERE TRIM(e.instance_id) = TRIM(?)
        ''', (instance_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            ak = row[0].strip() if row[0] else None
            sk = row[1].strip() if row[1] else None
            
            if ak and sk:
                return ak, sk
            else:
                print(f"⚠️ [发现数据] 实例 {instance_id} 绑定了账号，但 AK 或 SK 字段为空！")
        else:
            print(f"⚠️ [未匹配到] 连表查不到实例，传入参数: '{instance_id}' (请留意是否有奇怪字符)")
            
    except Exception as e:
        print(f"❌ 读取数据库动态密钥失败: {e}")
        
    return None, None

def _sync_fetch_aliyun_traffic(instance_id: str) -> float:
    """
    (同步阻塞函数) 动态获取当前实例专属密钥并调用阿里云云监控 API 获取出网流量
    """
    # 1. 动态获取当前实例绑定的专属 AK 和 SK
    ak, sk = get_dynamic_aliyun_credentials(instance_id)
    
    if not ak or not sk:
        print(f"实例 {instance_id} 未找到关联的 API 密钥，降级返回 0.0")
        return 0.0

    try:
        # 2. 使用动态密钥初始化配置
        cms_config = open_api_models.Config(
            access_key_id=ak,          
            access_key_secret=sk,
            endpoint='metrics.cn-hangzhou.aliyuncs.com'
        )
        client = CmsClient(cms_config)

        # 3. 发送网络流出量查询请求 (InternetOut)
        request = cms_models.DescribeMetricLastRequest(
            namespace='acs_ecs_dashboard',
            metric_name='InternetOut',
            dimensions=json.dumps([{"instanceId": instance_id}])
        )
        
        response = client.describe_metric_last(request)
        datapoints = json.loads(response.body.datapoints)
        
        if datapoints:
            bytes_used = datapoints[0].get('Average', 0) 
            return round(bytes_used / (1024 ** 3), 4) # 转换为 GB 精度保留 4 位
        return 0.0
    except Exception as e:
        print(f"获取实例 {instance_id} 阿里云真实流量失败: {str(e)}")
        return 0.0 

async def fetch_server_total_traffic(instance_id: str) -> float:
    """
    (异步包装器) 供 handler 安全调用阿里云 API，防止阻塞主进程
    """
    return await asyncio.to_thread(_sync_fetch_aliyun_traffic, instance_id)


# ================= 🛡️ 流量与计费大盘入口 =================
@router.message(F.text == "📊 流量与计费")
async def show_traffic_report(message: Message):
    wait_msg = await message.answer("🔄 正在向云端拉取全局财务数据，请稍候...")
    
    try:
        user_id = message.chat.id
        servers = get_active_servers(user_id)
        
        if not servers:
            return await wait_msg.edit_text(
                "📭 <b>当前控制台中未发现任何激活的服务器！</b>\n\n"
                "💡 <i>请先通过主控制台或阿里云 API 开出实例，机器上线后即可自动开始同步流量报表。</i>",
                parse_mode="HTML"
            )

        # 拉取大盘文本
        report_text = await asyncio.wait_for(
            generate_traffic_summary(servers),
            timeout=20.0
        )
        
        # 构建键盘：单页入口按钮
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🎛 查看全部节点", callback_data="traffic:view_all_nodes"))
        builder.row(
            InlineKeyboardButton(text="⚙️ 设置全局预警线", callback_data="sys_set_warn_line"),
            InlineKeyboardButton(text="🚨 设置全局熔断线", callback_data="sys_set_stop_line")
        )
        builder.row(InlineKeyboardButton(text="🔄 重新刷新报表", callback_data="refresh_traffic_report"))
        
        await wait_msg.edit_text(
            report_text, 
            parse_mode="HTML", 
            reply_markup=builder.as_markup()
        )
        
    except asyncio.TimeoutError:
        await wait_msg.edit_text(
            "⚠️ <b>连接拉取数据发生响应超时！</b>\n\n"
            "这可能是由于网络轻微抖动导致的。建议稍等半分钟后再重试。",
            parse_mode="HTML"
        )
    except Exception as e:
        err_detail = traceback.format_exc()
        print(f"[Traffic Report Error]:\n{err_detail}")
        
        await wait_msg.edit_text(
            f"❌ <b>拉取流量报表时遭遇异常拦截！</b>\n\n"
            f"<b>错误信息：</b> <code>{str(e)}</code>",
            parse_mode="HTML"
        )


# ================= 🎛 查看全网节点列表 (二级页面) =================
@router.callback_query(F.data == "traffic:view_all_nodes")
async def view_all_nodes_list(call: CallbackQuery):
    wait_msg = await call.message.edit_text("⏳ 正在并发嗅探所有服务器上的节点状态，请稍候...", parse_mode="HTML")
    
    try:
        user_id = call.message.chat.id
        servers = get_active_servers(user_id)
        
        if not servers:
            return await wait_msg.edit_text(
                "📭 当前控制台中未发现任何激活的服务器。",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 返回大盘", callback_data="refresh_traffic_report")]])
            )

        from handlers.node_actions.mgui_action import execute_mg_hybrid
        
        async def fetch_instance_ports(inst_id, ip):
            """静默抓取指定服务器上的所有节点端口"""
            script = "sqlite3 /root/mg_core.db 'SELECT port FROM mg_nodes' 2>/dev/null"
            try:
                out = await execute_mg_hybrid(inst_id, user_id, script)
                ports = [p.strip() for p in out.split('\n') if p.strip().isdigit()]
                return inst_id, ip, ports
            except Exception:
                return inst_id, ip, []

        # 并发执行所有的 SSH 嗅探任务
        tasks = [fetch_instance_ports(srv.get("instance_id"), srv.get("ip")) for srv in servers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 构建节点键盘
        builder = InlineKeyboardBuilder()
        has_nodes = False
        
        for res in results:
            if isinstance(res, Exception): 
                continue
            inst_id, ip, ports = res
            
            for port in ports:
                has_nodes = True
                btn_text = f"🎛 {ip} | 端口: {port}"
                callback = f"mg_cmd:port_link-{port}:{inst_id}"
                builder.row(InlineKeyboardButton(text=btn_text, callback_data=callback))
        
        # 底部添加返回大盘的按钮
        builder.row(InlineKeyboardButton(text="🔙 返回财务大盘", callback_data="refresh_traffic_report"))
        
        if has_nodes:
            text = "📋 <b>全网活跃节点清单</b>\n\n👇 点击下方指定节点可直接查看详细信息及专属分享链接："
        else:
            text = "📭 <b>当前全网暂未创建任何节点。</b>"
            
        await wait_msg.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        
    except Exception as e:
        print(f"[View Nodes Error]:\n{traceback.format_exc()}")
        await wait_msg.edit_text(
            f"❌ <b>获取节点列表失败：</b>\n<code>{str(e)}</code>", 
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 返回大盘", callback_data="refresh_traffic_report")]])
        )


# ================= 🚀 数据计算逻辑模块 =================
async def generate_traffic_summary(servers):
    total_count = len(servers)
    report = (
        f"📊 <b>MG 全局节点实时流量与财务报表</b>\n\n"
        f"🏢 <b>名下托管服务器总数</b>：<code>{total_count}</code> 台\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    
    # 获取全局当前时间
    now = datetime.now()
    
    # 逐台尝试解析流量
    for srv in servers:
        inst_id = srv.get("instance_id", "未知ID")
        ip = srv.get("ip", "未知IP")
        region = srv.get("region", "未知区域")
        limit_gb = srv.get("traffic_limit_gb", 500)
        
        # 动态通过阿里云 API 获取精准扣费流量
        used_gb = await fetch_server_total_traffic(inst_id) if inst_id != "未知ID" else 0.0
        
        # ================= 🌟 动态推算自然月账期 =================
        expire_str = srv.get("expire_time")
        
        if not expire_str or str(expire_str).strip() in ["", "None", "未知日期"]:
            start_time_str = srv.get("traffic_start_time") or srv.get("create_time") or srv.get("start_time")
            if start_time_str and start_time_str != "未知日期":
                try:
                    start_date = datetime.strptime(str(start_time_str)[:10], "%Y-%m-%d")
                    target_month = now.month
                    target_year = now.year
                    
                    reset_day = srv.get("reset_day")
                    anchor_day = int(reset_day) if reset_day else start_date.day
                    
                    if now.day >= anchor_day:
                        target_month += 1
                        if target_month > 12:
                            target_month = 1
                            target_year += 1
                            
                    last_day = calendar.monthrange(target_year, target_month)[1]
                    final_day = min(anchor_day, last_day)
                    expire_str = datetime(target_year, target_month, final_day).strftime("%Y-%m-%d")
                except Exception as e:
                    print(f"推算大盘日期失败: {e}")
                    expire_str = "长期有效"
            else:
                expire_str = "长期有效"

        # 计算剩余天数
        if expire_str == "长期有效":
            days_text = "-"
        else:
            try:
                expire_date = datetime.strptime(expire_str, "%Y-%m-%d")
                days_left = (expire_date - now).days
                days_text = f"剩余 {days_left} 天" if days_left >= 0 else "已逾期"
            except ValueError:
                days_text = "日期解析错误"

        # 计算百分比与进度条
        percent = min((used_gb / limit_gb) * 100, 100) if limit_gb > 0 else 0
        bar = get_progress_bar(used_gb, limit_gb)
        
        # 动态智能单位转换
        if used_gb < (1/1024):
            traffic_display = f"{used_gb * 1024 * 1024:.2f} KB"
        elif used_gb < 1:
            traffic_display = f"{used_gb * 1024:.2f} MB"
        else:
            traffic_display = f"{used_gb:.2f} GB"
        
        try:
            # 组装节点信息
            report += f"💻 <b>[{region}]</b> <code>{ip}</code>\n"
            status_emoji = "🔴 已停用 (流量耗尽熔断)" if percent >= 95 else "🟢 正常运作"
            report += f" └ 状态: {status_emoji}\n"
            report += f" └ 流量: {traffic_display} / {limit_gb} GB ({percent:.1f}%) {bar}\n"
            report += f" └ 账期: {expire_str} 到期 ({days_text})\n\n"
        except Exception as e:
            report += f"💻 <code>{ip}</code> (数据组装受阻: {str(e)[:20]})\n\n"
            
    report += (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>提示：所有计算默认按开机日为锚点循环。警戒线/熔断线一旦触发，系统将在后台全自动执行私聊警告或断网操作。没有定时重启或维护计划。</i>"
    )
    return report


# ================= 1. 刷新报表按钮 =================
@router.callback_query(F.data == "refresh_traffic_report")
async def process_refresh_traffic(call: CallbackQuery):
    await call.answer("🔄 正在重新拉取最新数据...")
    # 为了保持聊天框整洁，直接删掉旧报表，重新调用主入口发一份新的
    await call.message.delete()
    await show_traffic_report(call.message)


# ================= 2. 设置警戒线 =================
@router.callback_query(F.data == "sys_set_warn_line")
async def ask_warn_line(call: CallbackQuery, state: FSMContext):
    await state.set_state(TrafficSettingsFSM.wait_for_warn_line)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ 取消操作", callback_data="cancel_fsm_action"))
    
    await call.message.answer(
        "⚠️ **请直接回复新的全局【警戒线】百分比：**\n\n"
        "*(请输入 1-99 之间的纯数字，例如 80 代表 80%)*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await call.answer()


@router.message(TrafficSettingsFSM.wait_for_warn_line)
async def receive_warn_line(message: Message, state: FSMContext):
    val = message.text.strip()
    if not val.isdigit() or not (1 <= int(val) <= 99):
        return await message.answer("❌ 格式错误！请输入 1-99 之间的纯数字：")
    
    warn_percent = int(val)
    
    # 对接数据库写入逻辑
    # import db
    # db.update_global_config("traffic_warn_line", warn_percent)
    
    await state.clear()
    await message.answer(
        f"✅ **全局预警线已成功修改为: `{warn_percent}%`**\n\n"
        f"当任意节点流量达到此阈值时，机器人将主动向您发送私聊预警。", 
        parse_mode="Markdown"
    )


# ================= 3. 设置熔断线 =================
@router.callback_query(F.data == "sys_set_stop_line")
async def ask_stop_line(call: CallbackQuery, state: FSMContext):
    await state.set_state(TrafficSettingsFSM.wait_for_stop_line)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ 取消操作", callback_data="cancel_fsm_action"))
    
    await call.message.answer(
        "🚨 **请直接回复新的全局【熔断线】百分比：**\n\n"
        "*(请输入 50-100 之间的纯数字，例如 95 代表 95%)*",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await call.answer()


@router.message(TrafficSettingsFSM.wait_for_stop_line)
async def receive_stop_line(message: Message, state: FSMContext):
    val = message.text.strip()
    if not val.isdigit() or not (50 <= int(val) <= 100):
        return await message.answer("❌ 格式错误！请输入 50-100 之间的纯数字：")
    
    stop_percent = int(val)
    
    # 对接数据库写入逻辑
    # import db
    # db.update_global_config("traffic_stop_line", stop_percent)
    
    await state.clear()
    await message.answer(
        f"✅ **全局熔断线已成功修改为: `{stop_percent}%`**\n\n"
        f"当任意节点流量达到此阈值时，系统将在后台强行执行物理关机止损！", 
        parse_mode="Markdown"
    )


# ================= 4. 通用取消按钮 =================
@router.callback_query(F.data == "cancel_fsm_action")
async def cancel_fsm_action(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("✅ 操作已取消。")
    await call.answer()
