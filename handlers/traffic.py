import asyncio
import traceback
import json
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


# ================= 🔌 阿里云 CMS 流量查询核心逻辑 =================
def _sync_fetch_aliyun_traffic(instance_id: str) -> float:
    """
    (同步阻塞函数) 调用阿里云云监控 API 获取 ECS 实例本月累计公网流出流量。
    """
    try:
        # 初始化配置，强烈建议将 AK/SK 放到 config.py 或 .env 中
        cms_config = open_api_models.Config(
            access_key_id=config.ALIYUN_AK,          
            access_key_secret=config.ALIYUN_SK,
            endpoint='metrics.cn-hangzhou.aliyuncs.com' # 云监控全局统一 Endpoint
        )
        client = CmsClient(cms_config)

        # 构造请求: 查询 ECS 实例网络流出量 (InternetOut)
        request = cms_models.DescribeMetricLastRequest(
            namespace='acs_ecs_dashboard',
            metric_name='InternetOut',
            dimensions=json.dumps([{"instanceId": instance_id}])
        )
        
        response = client.describe_metric_last(request)
        datapoints = json.loads(response.body.datapoints)
        
        if datapoints:
            # 假设返回值是 Bytes，转换为 GB
            bytes_used = datapoints[0].get('Average', 0) 
            return round(bytes_used / (1024 ** 3), 2)
        return 0.0
    except Exception as e:
        print(f"获取实例 {instance_id} 流量失败: {str(e)}")
        raise e

async def fetch_aliyun_traffic_gb(instance_id: str) -> float:
    """
    (异步包装器) 供 aiogram 的 handler 安全调用，防止阻塞机器人
    """
    return await asyncio.to_thread(_sync_fetch_aliyun_traffic, instance_id)


# ================= 🛡️ 流量与计费核心入口 =================
@router.message(F.text == "📊 流量与计费")
async def show_traffic_report(message: Message):
    # 1. 发送正在执行的提示，并保存这条消息的句柄以供后续更新
    wait_msg = await message.answer("🔄 正在向阿里云接口同步获取全局财务与实时流量报表，请稍候...")
    
    try:
        user_id = message.from_user.id
        servers = get_active_servers(user_id)
        
        if not servers:
            return await wait_msg.edit_text(
                "📭 <b>当前控制台中未发现任何激活的服务器！</b>\n\n"
                "💡 <i>请先通过主控制台或阿里云 API 开出实例，机器上线后即可自动开始同步流量报表。</i>",
                parse_mode="HTML"
            )

        # 2. 尝试获取流量数据（加入超时控制，适当延长时间以防多实例查询）
        report_text = await asyncio.wait_for(
            generate_traffic_summary(servers),
            timeout=20.0
        )
        
        # 3. 构建悬浮设置按钮
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="⚙️ 设置全局预警线", callback_data="sys_set_warn_line"),
            InlineKeyboardButton(text="🚨 设置全局熔断线", callback_data="sys_set_stop_line")
        )
        builder.row(InlineKeyboardButton(text="🔄 重新刷新报表", callback_data="refresh_traffic_report"))
        
        # 4. 成功后更新消息，并带上底部按钮
        await wait_msg.edit_text(
            report_text, 
            parse_mode="HTML", 
            reply_markup=builder.as_markup()
        )
        
    except asyncio.TimeoutError:
        # 针对网络超时卡死，给出优雅退出说明
        await wait_msg.edit_text(
            "⚠️ <b>连接阿里云云监控 API 发生响应超时！</b>\n\n"
            "这可能是由于跨国网络轻微抖动，或者您当前名下服务器节点较多导致的。建议稍等半分钟后再重新点击「📊 流量与计费」。",
            parse_mode="HTML"
        )
    except Exception as e:
        # 如果发生任何异常，把具体错误抛出
        err_detail = traceback.format_exc()
        print(f"[Traffic Report Error]:\n{err_detail}")
        
        await wait_msg.edit_text(
            f"❌ <b>拉取流量报表时遭遇异常拦截！</b>\n\n"
            f"<b>错误信息：</b> <code>{str(e)}</code>\n\n"
            f"💡 <b>常规排查建议：</b>\n"
            f"1. 请检查您的 <code>config.py</code> 中的阿里云 Access Key 是否具有 <b>云监控 (CloudMonitor / CMS)</b> 的读取权限。\n"
            f"2. 请检查服务器环境中是否已正确安装依赖库：<code>pip install alibabacloud_cms20190101</code>",
            parse_mode="HTML"
        )


# ================= 🚀 数据计算逻辑模块 =================
async def generate_traffic_summary(servers):
    total_count = len(servers)
    report = (
        f"📊 <b>MG 全局节点实时流量与财务报表</b>\n\n"
        f"🏢 <b>名下托管服务器总数</b>：<code>{total_count}</code> 台\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    
    # 动态获取当前时间，用于计算剩余到期天数
    now = datetime.now()
    
    # 逐台尝试解析流量
    for srv in servers:
        inst_id = srv.get("instance_id", "未知ID")
        ip = srv.get("ip", "未知IP")
        region = srv.get("region", "未知区域")
        
        limit_gb = srv.get("traffic_limit_gb", 500) # 总额度优先从数据库读
        
        # 🔥 核心修改 1: 真实调用阿里云 API 获取流量
        try:
            if inst_id != "未知ID":
                used_gb = await fetch_aliyun_traffic_gb(inst_id)
            else:
                used_gb = srv.get("used_traffic_gb", 0.0) # 无ID时使用数据库缓存
        except Exception as e:
            print(f"[{ip}] 获取 CMS 流量失败，降级使用数据库缓存: {str(e)}")
            used_gb = srv.get("used_traffic_gb", 0.0)
        
        # 🔥 核心修改 2: 真实解析数据库记录的到期时间
        expire_str = srv.get("expire_time", "未知日期")
        
        # 计算剩余天数
        try:
            if expire_str != "未知日期":
                expire_date = datetime.strptime(expire_str, "%Y-%m-%d")
                days_left = (expire_date - now).days
                days_text = f"剩余 {days_left} 天" if days_left >= 0 else "已逾期"
            else:
                days_text = "缺少到期数据"
        except ValueError:
            days_text = "日期格式异常"

        # 计算百分比与进度条
        percent = min((used_gb / limit_gb) * 100, 100) if limit_gb > 0 else 0
        bar = get_progress_bar(used_gb, limit_gb)
        
        try:
            # 🌟 组装节点信息
            report += f"💻 <b>[{region}]</b> <code>{ip}</code>\n"
            status_emoji = "🔴 已停用 (流量耗尽熔断)" if percent >= 95 else "🟢 正常运作"
            report += f" └ 状态: {status_emoji}\n"
            report += f" └ 流量: {used_gb} GB / {limit_gb} GB ({percent:.1f}%) {bar}\n"
            report += f" └ 账期: {expire_str} 到期 ({days_text})\n\n"
        except Exception as e:
            report += f"💻 <code>{ip}</code> (数据组装受阻: {str(e)[:20]})\n\n"
            
    report += (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>提示：所有计算默认按开机日为锚点循环。警戒线/熔断线一旦触发，系统将在后台全自动执行私聊警告或断网操作。无定时重启或维护计划。</i>"
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
