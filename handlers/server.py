import os
import asyncio
import random
import time
import json
import sqlite3  # 🛠️ 修正 1：补充遗漏的 sqlite3 模块导入，防止底层删机改库时报 NameError
import resend
import config
import glob
import db  # 导入本地账本

import calendar

from aiogram.filters import StateFilter  # 🌟 必须加上这行
from aiogram.types import Message, CallbackQuery
from handlers.common import get_dynamic_ecs_client

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from alibabacloud_cms20190101.client import Client as CmsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_cms20190101 import models as cms_models
from datetime import datetime
from dateutil.relativedelta import relativedelta
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# 阿里云 SDK 依赖
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_ecs20140526 import models as ecs_models

# 实例化当前模块的路由器
router = Router()

# ================= 新增：🛡️ 全局最高权限拦截 =================
# 只要发消息或点击按钮的人不是 ADMIN_ID，这个 router 里的所有函数都不会被触发，直接无视！
router.message.filter(F.from_user.id == int(config.ADMIN_ID))
router.callback_query.filter(F.from_user.id == int(config.ADMIN_ID))
# =========================================================

# ================= 全局地域大名单 =================
# 涵盖了你菜单中支持的所有亚洲、欧美、中东等 16 个地区
GLOBAL_REGIONS = [
    "cn-hongkong", "ap-northeast-1", "ap-northeast-2", 
    "ap-southeast-1", "ap-southeast-3", "ap-southeast-5", 
    "ap-southeast-6", "ap-southeast-7", "eu-central-1", 
    "eu-west-1", "eu-west-2", "eu-west-3", "us-west-1", 
    "us-east-1", "me-east-1", "me-central-1", "na-south-1"
]

# ==================================================

# 定义 FSM 状态机
class ServerManagement(StatesGroup):
    waiting_for_code = State()
    waiting_for_region = State()
    
# ================= 新增：云账号添加状态 =================
class CloudAccountStates(StatesGroup):
    waiting_for_credentials = State()

def get_account_selection_keyboard():
    """动态生成云账号选择键盘"""
    builder = InlineKeyboardBuilder()
    
    # 实时查库获取活跃账号
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, alias FROM cloud_accounts WHERE is_active = 1")
    accounts = cursor.fetchall()
    conn.close()

    if not accounts:
        builder.row(InlineKeyboardButton(text="⚠️ 暂无可用云账号，请先添加", callback_data="no_action"))
    else:
        # 遍历账号生成按钮
        for acc_id, alias in accounts:
            builder.row(InlineKeyboardButton(text=f"🏢 {alias}", callback_data=f"select_acc:{acc_id}"))
    
    # 修正逻辑：这里应该放“添加云账号”，而不是 SSH
    builder.row(InlineKeyboardButton(text="➕ 添加云账号", callback_data="add_cloud_account"))
    builder.row(InlineKeyboardButton(text="关闭菜单", callback_data="close_menu"))
    
    return builder.as_markup()

# ================= 1. 底层 API 与发信函数 =================
async def send_email_async(code: str) -> bool:
    """
    ⚡️ 异步封装的 Resend API 发信 (彻底解决机器人卡死问题)
    """
    resend.api_key = config.RESEND_API_KEY 
    
    def _send():
        params = {
            "from": "onboarding@resend.dev",
            "to": [config.RECIPIENT],
            "subject": "MG 控制台 V2.0 - 极速 API 验证码",
            "text": f"【MG 控制台】\n\n您的开服验证码是：{code}\n请在 5 分钟内返回 TG 进行验证。",
        }
        return resend.Emails.send(params)
        
    try:
        email = await asyncio.to_thread(_send)
        print(f"✅ API 发信成功: {email}")
        return True
    except Exception as e:
        print(f"❌ Resend API 发信失败: {e}")
        return False

def get_template_id(account_id: int, region_id: str) -> str:
    """
    ⚡️ 已升级多账号架构：从 bot_data.db 的 launch_templates 表精准匹配指定账号和地域的模板
    彻底斩断对 .env 假占位符的任何依赖！
    """
    
    # 1. 把真正的目标数据库 bot_data.db 放在第一优先级
    candidate_dbs = ['/srv/aali/bot_data.db']
    if hasattr(config, 'DB_PATH') and config.DB_PATH not in candidate_dbs:
        candidate_dbs.append(config.DB_PATH)
    candidate_dbs.extend(glob.glob('/srv/aali/*.db'))

    # 2. 遍历查找真实启动模板 ID
    for db_file in candidate_dbs:
        if not os.path.exists(db_file):
            continue
        try:
            conn = sqlite3.connect(db_file, timeout=3.0)
            cursor = conn.cursor()
            
            # 🌟 核心升级：利用刚才数据库补充的 account_id，实现精准定向双重匹配！
            cursor.execute(
                "SELECT template_id FROM launch_templates WHERE account_id = ? AND (region_id = ? OR region_name = ?)", 
                (account_id, region_id, region_id)
            )
            result = cursor.fetchone()
            conn.close()
            
            # 只要查到了，并且是合法的 lt- 开头，直接返回
            if result and result[0] and str(result[0]).strip().startswith("lt-") and "xxxx" not in str(result[0]):
                return str(result[0]).strip() 
                
        except Exception:
            continue

    # 3. 如果未配置，直接抛错阻断
    raise ValueError(f"提示：未在系统数据库中找到 [账号ID:{account_id}] 在 [{region_id}] 的启动模板 ID！请在面板的「启动模板管理」中重新配置。")

def create_ecs_instance_sync(account_id: int, region_id: str, template_id: str) -> dict:
    """同步调用阿里云 API 创建实例并轮询获取 IP (后台线程执行)"""
    try:
        # 🌟 核心替换：删掉原来的静态 AK/SK，直接调用动态客户端工厂
        client = get_dynamic_ecs_client(account_id, region_id)

        run_request = ecs_models.RunInstancesRequest(
            region_id=region_id,
            launch_template_id=template_id,
            amount=1,
            password="@QS00008"
        )
        run_response = client.run_instances(run_request)
        instance_id = run_response.body.instance_id_sets.instance_id_set[0]
        
        describe_request = ecs_models.DescribeInstancesRequest(
            region_id=region_id,
            instance_ids=json.dumps([instance_id])
        )
        
        for _ in range(15):
            time.sleep(5)
            desc_resp = client.describe_instances(describe_request)
            instances = desc_resp.body.instances.instance
            if not instances:
                continue
                
            instance = instances[0]
            status = instance.status
            
            if status == "Running":
                public_ip = "无公网IP"
                if instance.public_ip_address and instance.public_ip_address.ip_address:
                    public_ip = instance.public_ip_address.ip_address[0]
                return {"success": True, "instance_id": instance_id, "ip": public_ip}
            elif status in ["Stopped", "Deleted"]:
                return {"success": False, "error": f"实例状态异常: {status}"}
                
        return {"success": False, "error": "轮询超时，机器可能还在创建中，请稍后去控制台查看。"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _fetch_single_region_sync(account_id: int, region_id: str) -> list:
    """内部同步函数：只负责去单个地域拿数据"""
    try:
        client = get_dynamic_ecs_client(account_id, region_id)
        req = ecs_models.DescribeInstancesRequest(region_id=region_id, page_size=50)
        resp = client.describe_instances(req)
        
        instances = []
        if resp.body.instances and resp.body.instances.instance:
            for inst in resp.body.instances.instance:
                ip = "无公网IP"
                if inst.public_ip_address and inst.public_ip_address.ip_address:
                    ip = inst.public_ip_address.ip_address[0]
                instances.append({
                    "id": inst.instance_id,
                    "ip": ip,
                    "status": inst.status,
                    "region": region_id
                })
        return instances
    except Exception as e:
        print(f"❌ 获取实例列表失败 (Account ID: {account_id}, Region: {region_id}): {e}")
        return []

def _fetch_swas_single_region_sync(account_id: int, region_id: str) -> list:
    """内部同步函数：去单个地域拿轻量云(SWAS)数据"""
    try:
        from alibabacloud_swas_open20200601.client import Client as SwasClient
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_swas_open20200601 import models as swas_models
        import sqlite3
        import config

        conn = sqlite3.connect(config.DB_PATH, timeout=2.0)
        cursor = conn.cursor()
        cursor.execute("SELECT access_key, access_secret FROM cloud_accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row: return []
        ak, sk = row
        
        ali_config = open_api_models.Config(
            access_key_id=ak.strip(),
            access_key_secret=sk.strip(),
            endpoint=f'swas.{region_id}.aliyuncs.com'
        )
        client = SwasClient(ali_config)
        req = swas_models.ListInstancesRequest(region_id=region_id)
        resp = client.list_instances(req)
        
        instances = []
        if resp.body.instances:
            for inst in resp.body.instances:
                ip = inst.public_ip_address if inst.public_ip_address else "无公网IP"
                instances.append({
                    "id": inst.instance_id,
                    "ip": ip,
                    "status": inst.status,
                    "region": region_id,
                    "is_swas": True  # 🌟 重点：给轻量云打上专属烙印
                })
        return instances
    except Exception as e:
        return []

async def get_instances_by_account_async(account_id: int) -> list:
    """⚡️ 极限优化版：同时并发向 ECS 和 轻量云 发起狙击请求"""
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=2.0)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT region_id FROM ecs_business WHERE account_id = ?", (account_id,))
        active_regions = [row[0] for row in cursor.fetchall()]
        conn.close()
    except Exception as e:
        active_regions = []
        
    # 为了防止轻量云没写入 DB 导致查不到，强制加上几个核心节点作为底座
    default_regions = {"cn-hongkong", "ap-southeast-1", "us-east-1", "ap-northeast-1"}
    active_regions = list(set(active_regions) | default_regions)
        
    # 🎯 并发请求：一半查 ECS，一半查 SWAS
    ecs_tasks = [asyncio.to_thread(_fetch_single_region_sync, account_id, r) for r in active_regions]
    swas_tasks = [asyncio.to_thread(_fetch_swas_single_region_sync, account_id, r) for r in active_regions]
    
    results = await asyncio.gather(*(ecs_tasks + swas_tasks))
    
    return [inst for region_list in results for inst in region_list]

# ================= 2. 动态折叠菜单 UI 构建器 =================

def get_region_main_menu():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    
    # 🌟 修复关键：暗号必须是 region_cn-hongkong
    builder.row(InlineKeyboardButton(text="🇭🇰 中国香港", callback_data="region_cn-hongkong"))
    
    # 下面这些是控制展开二级菜单的，对应你的 F.data.startswith("menu_")，不用动
    builder.row(
        InlineKeyboardButton(text="🌏 亚洲地区", callback_data="menu_asia"),
        InlineKeyboardButton(text="🌍 欧美地区", callback_data="menu_eu_us")
    )
    builder.row(InlineKeyboardButton(text="🐪 中东及其他", callback_data="menu_others"))
    
    # 返回主菜单
    builder.row(InlineKeyboardButton(text="🔙 取消并返回", callback_data="cancel_add_server"))
    
    return builder.as_markup()

def get_region_asia_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇯🇵 日本(东京)", callback_data="region_ap-northeast-1"),
        InlineKeyboardButton(text="🇰🇷 韩国(首尔)", callback_data="region_ap-northeast-2")
    )
    builder.row(
        InlineKeyboardButton(text="🇸🇬 新加坡", callback_data="region_ap-southeast-1"),
        InlineKeyboardButton(text="🇲🇾 马来西亚(吉隆坡)", callback_data="region_ap-southeast-3")
    )
    builder.row(
        InlineKeyboardButton(text="🇮🇩 印尼(雅加达)", callback_data="region_ap-southeast-5"),
        InlineKeyboardButton(text="🇵🇭 菲律宾(马尼拉)", callback_data="region_ap-southeast-6")
    )
    builder.row(
        InlineKeyboardButton(text="🇹🇭 泰国(曼谷)", callback_data="region_ap-southeast-7"),
        InlineKeyboardButton(text="🇲🇾 马来西亚(柔佛)", callback_data="region_ap-southeast-8")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="menu_main"))
    return builder.as_markup()

def get_region_eu_us_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇺🇸 美国(弗吉尼亚)", callback_data="region_us-east-1"),
        InlineKeyboardButton(text="🇺🇸 美国(硅谷)", callback_data="region_us-west-1")
    )
    builder.row(
        InlineKeyboardButton(text="🇲🇽 墨西哥", callback_data="region_na-south-1"),
        InlineKeyboardButton(text="🇬🇧 英国(伦敦)", callback_data="region_eu-west-1")
    )
    builder.row(
        InlineKeyboardButton(text="🇫🇷 法国(巴黎)", callback_data="region_eu-west-2"),
        InlineKeyboardButton(text="🇩🇪 德国(法兰克福)", callback_data="region_eu-central-1")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="menu_main"))
    return builder.as_markup()

def get_region_others_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇦🇪 阿联酋(迪拜)", callback_data="region_me-east-1"),
        InlineKeyboardButton(text="🇸🇦 沙特(利雅得)", callback_data="region_me-central-1")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="menu_main"))
    return builder.as_markup()



# ================= 🚀 主菜单：服务器管理入口 =================
@router.message(F.text == "💻 服务器管理")
async def cmd_server_management(message: types.Message, state: FSMContext):
    await state.clear()
    
    # 1. ⚡️ 将同步查库丢进后台线程池，绝对不卡主线程
    def _fetch_accounts():
        import sqlite3
        import config
        try:
            conn = sqlite3.connect(config.DB_PATH, timeout=2.0)
            cursor = conn.cursor()
            cursor.execute("SELECT id, alias FROM cloud_accounts WHERE is_active = 1")
            accs = cursor.fetchall()
            conn.close()
            return accs
        except Exception as e:
            print(f"读取账号表失败: {e}")
            return []
            
    import asyncio
    accounts = await asyncio.to_thread(_fetch_accounts)
    
    # 2. 动态生成键盘
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    
    if not accounts:
        builder.row(InlineKeyboardButton(text="⚠️ 暂无可用云账号，请先添加", callback_data="no_action"))
    else:
        for acc_id, alias in accounts:
            builder.row(InlineKeyboardButton(text=f"🏢 {alias}", callback_data=f"select_acc:{acc_id}"))
            
    builder.row(InlineKeyboardButton(text="➕ 添加云账号", callback_data="add_cloud_account"))
    # builder.row(InlineKeyboardButton(text="关闭菜单", callback_data="close_menu")) # 如果不需要关闭按钮，这行可以删掉

    # 3. 发送瞬间响应
    text = "📊 **服务器配置中心**\n\n请先选择你要操作的资源池 / 云账号："
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# ==========================================
# 🌟 已移除所有内存缓存逻辑，每次点击强制实时查云端
# ==========================================

@router.callback_query(F.data.startswith("select_acc:"))
async def process_account_selection(call: types.CallbackQuery, state: FSMContext):
    
    # 1. 🌟 核心优化：立刻响应 TG，告诉用户正在实时拉取
    await call.answer("🔄 正在向云端实时拉取最新数据，请稍候...", show_alert=False)

    # 2. 提取账号 ID
    account_id_str = call.data.split(":")[1]
    account_id = int(account_id_str)
    
    # 3. 将当前选中的 account_id 存入状态机
    await state.update_data(current_account_id=account_id)
    
    try:
        # 🌟 核心修改：没有任何缓存判断！直接无脑向阿里云拉取最真实的数据！
        instances = await get_instances_by_account_async(account_id)
        
        running_count = sum(1 for i in instances if i['status'] == 'Running')
        stopped_count = sum(1 for i in instances if i['status'] in ['Stopped', 'Stopping'])
        pending_count = sum(1 for i in instances if i['status'] in ['Pending', 'Starting'])

        # 界面去掉“极速缓存”字眼，明确告诉用户这是实时数据
        text = (
            f"🏢 **当前账号 ECS 概览** 🔄(实时云端数据)\n\n"
            f"🟢 运行中: {running_count} 台\n"
            f"🔴 已停用: {stopped_count} 台\n"
            f"🔵 部署/开机中: {pending_count} 台\n"
        )
        
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        
        # ⚠️ 注意：这里彻底删掉了“强制同步最新数据”按钮，因为现在每一次点击，本身就是最暴力的强制同步！
        builder.row(InlineKeyboardButton(text="☁️ 创建 阿里云 ECS 实例", callback_data="add_server"))
        builder.row(InlineKeyboardButton(text="🪶 创建 阿里云轻量云 (SWAS)", callback_data="add_swas_server"))
        
        region_map = {
            "cn-hongkong": "HK", "ap-northeast-1": "JP", "ap-northeast-2": "KR",    
            "ap-southeast-1": "SG", "ap-southeast-3": "MY", "ap-southeast-5": "ID",    
            "ap-southeast-6": "PH", "ap-southeast-7": "TH", "eu-central-1": "DE",      
            "eu-west-1": "UK", "eu-west-3": "FR", "us-west-1": "US",         
            "us-east-1": "US", "me-east-1": "AE", "me-central-1": "SA"       
        }

        for inst in instances:
            if inst['status'] == 'Running':
                status_emoji = "🟢"
            elif inst['status'] in ['Stopped', 'Stopping']:
                status_emoji = "🔴"
            else:
                status_emoji = "🔵"
                
            short_region = region_map.get(inst['region'], inst['region'])
            
            # 🌟 核心判断：如果是轻量云，不仅要改前缀标签，还要改变它点击后的专属路由！
            if inst.get("is_swas"):
                btn_text = f"{status_emoji} [轻量—{short_region}] IP: {inst['ip']}"
                btn_data = f"manage_swas_{inst['id']}"  # 👈 注意这里改成了 manage_swas_
            else:
                btn_text = f"{status_emoji} [{short_region}] IP: {inst['ip']}"
                btn_data = f"manage_ecs_{inst['id']}"   # 👈 保持原有的 ECS 通道
                
            # 下面这行不用动
            builder.row(InlineKeyboardButton(text=btn_text, callback_data=btn_data))
            
        # 👇 这个返回按钮原封不动保留
        builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="back_to_accounts"))
        
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        
    except Exception as e:
        await call.message.edit_text(f"❌ 获取服务器列表失败：{str(e)}\n请检查该账号的 API 密钥配置。")

# ================= 强制同步最新数据 =================
@router.callback_query(F.data.startswith("force_sync_acc:"))
async def process_force_sync(call: types.CallbackQuery, state: FSMContext):
    # 1. 立刻响应 TG，告诉用户正在拉取
    await call.answer("🔄 正在强制绕过缓存，向阿里云请求最新数据...", show_alert=False)
    
    # 2. 提取账号 ID
    account_id = int(call.data.split(":")[1])
    
    try:
        import time
        # 3. 🌟 核心动作：直接强制调用最新的高并发函数去阿里云拉数据 (绝对不读本地缓存)
        instances = await get_instances_by_account_async(account_id)
        
        # ================= 下面是原汁原味的重新渲染面板逻辑 =================
        running_count = sum(1 for i in instances if i['status'] == 'Running')
        stopped_count = sum(1 for i in instances if i['status'] in ['Stopped', 'Stopping'])
        pending_count = sum(1 for i in instances if i['status'] in ['Pending', 'Starting'])

        text = (
            f"🏢 **当前账号 ECS 概览** ⚡️(已强制刷新至最新状态)\n\n"
            f"🟢 运行中: {running_count} 台\n"
            f"🔴 已停用: {stopped_count} 台\n"
            f"🔵 部署/开机中: {pending_count} 台\n"
        )
        
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        
        # 依然保留强制同步按钮
        builder.row(InlineKeyboardButton(text="🔄 强制同步最新数据", callback_data=f"force_sync_acc:{account_id}"))
        builder.row(InlineKeyboardButton(text="➕ 新增服务器", callback_data="add_server"))
        
        region_map = {
            "cn-hongkong": "HK", "ap-northeast-1": "JP", "ap-northeast-2": "KR",    
            "ap-southeast-1": "SG", "ap-southeast-3": "MY", "ap-southeast-5": "ID",    
            "ap-southeast-6": "PH", "ap-southeast-7": "TH", "eu-central-1": "DE",      
            "eu-west-1": "UK", "eu-west-3": "FR", "us-west-1": "US",         
            "us-east-1": "US", "me-east-1": "AE", "me-central-1": "SA"       
        }

        for inst in instances:
            if inst['status'] == 'Running':
                status_emoji = "🟢"
            elif inst['status'] in ['Stopped', 'Stopping']:
                status_emoji = "🔴"
            else:
                status_emoji = "🔵"
                
            short_region = region_map.get(inst['region'], inst['region'])
            
            # 🌟 核心判断：如果是轻量云，不仅要改前缀标签，还要改变它点击后的专属路由！
            if inst.get("is_swas"):
                btn_text = f"{status_emoji} [轻量—{short_region}] IP: {inst['ip']}"
                btn_data = f"manage_swas_{inst['id']}"  # 👈 注意这里改成了 manage_swas_
            else:
                btn_text = f"{status_emoji} [{short_region}] IP: {inst['ip']}"
                btn_data = f"manage_ecs_{inst['id']}"   # 👈 保持原有的 ECS 通道
                
            # 下面这行不用动
            builder.row(InlineKeyboardButton(text=btn_text, callback_data=btn_data))
            
        # 👇 这个返回按钮原封不动保留
        builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="back_to_accounts"))
        
        # 用最新拿到的数据，替换掉旧面板
        await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
        
    except Exception as e:
        await call.message.edit_text(f"❌ 强制同步失败：{str(e)}\n可能是阿里云 API 遭遇限流或网络波动。")
# ================= 菜单导航联动拦截器 =================

# 1. 🔙 返回上级 (从二级机器列表回到一级账号列表)
# ================= ↩️ 返回账号列表 =================
@router.callback_query(F.data == "back_to_accounts", StateFilter("*"))
async def process_back_to_accounts(call: types.CallbackQuery, state: FSMContext):
    # 强制清空残余状态
    await state.clear()
    
    def _fetch_accounts():
        try:
            conn = sqlite3.connect(config.DB_PATH, timeout=2.0)
            cursor = conn.cursor()
            cursor.execute("SELECT id, alias FROM cloud_accounts WHERE is_active = 1")
            accs = cursor.fetchall()
            conn.close()
            return accs
        except Exception:
            return []
            
    accounts = await asyncio.to_thread(_fetch_accounts)
    
    builder = InlineKeyboardBuilder()
    if not accounts:
        builder.row(InlineKeyboardButton(text="⚠️ 暂无可用云账号，请先添加", callback_data="no_action"))
    else:
        for acc_id, alias in accounts:
            builder.row(InlineKeyboardButton(text=f"🏢 {alias}", callback_data=f"select_acc:{acc_id}"))
            
    builder.row(InlineKeyboardButton(text="➕ 添加云账号", callback_data="add_cloud_account"))
    
    text = "📊 **服务器配置中心**\n\n请先选择你要操作的资源池 / 云账号："
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await call.answer()

# 2. ❌ 关闭菜单
@router.callback_query(F.data == "close_menu", StateFilter("*"))
async def process_close_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear() # 关闭菜单的同时清空所有缓存状态，防止幽灵 BUG
    await call.message.delete()
    await call.answer()

# 无效按钮的弹窗拦截
@router.callback_query(F.data == "no_action", StateFilter("*"))
async def process_no_action(callback: types.CallbackQuery):
    await callback.answer("请点击下方【➕ 添加云账号】按钮开始操作", show_alert=True)

# 3. ➕ 添加云账号入口
@router.callback_query(F.data == "add_cloud_account")
async def process_add_cloud_account(call: types.CallbackQuery, state: FSMContext):
    # 激活状态，告诉机器人下一步该接收文本消息了
    await state.set_state(CloudAccountStates.waiting_for_credentials)
    
    text = (
        "📝 **新增云账号**\n\n"
        "请直接回复新账号的信息，使用半角逗号分隔，格式如下：\n"
        "`别名,AccessKey,AccessSecret`\n\n"
        "📖 **如何获取阿里云 API 密钥？**\n"
        "1. 登录阿里云控制台，鼠标悬停在右上角头像。\n"
        "2. 进入 **AccessKey 管理** (推荐使用 RAM 子账号以确保安全)。\n"
        "3. 点击 **创建 AccessKey**，复制生成的 AK 和 SK。\n\n"
        "💡 **回复示例：**\n"
        "`香港主力机房,LTAI5t...,your_secret_key...`\n\n"
        "*(随时回复 /cancel 取消操作)*"
    )
    await call.message.edit_text(text, parse_mode="Markdown")
    await call.answer()

# 4. 📝 接收用户输入的账号信息并写入数据库
@router.message(CloudAccountStates.waiting_for_credentials)
async def process_credentials_input(message: types.Message, state: FSMContext):
    # 如果用户输入了 /cancel，则直接退出状态
    if message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("已取消添加云账号操作。")
        return

    text = message.text.strip()
    # 兼容中文逗号和英文逗号，统一替换为英文逗号
    text = text.replace("，", ",")
    parts = text.split(",")
    
    if len(parts) != 3:
        await message.answer("⚠️ 格式错误！请确保包含别名、AccessKey 和 AccessSecret，并用逗号隔开。\n\n请重新发送，或回复 /cancel 取消。")
        return
        
    alias, ak, sk = [p.strip() for p in parts]
    
    if not ak or not sk:
        await message.answer("⚠️ AccessKey 或 Secret 不能为空，请重新发送。")
        return
        
    wait_msg = await message.answer("🔄 正在加密保存并刷新账号列表...")
    
    try:
        # 直接连接数据库写入新账号 (is_active 默认为 1 启用)
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO cloud_accounts (alias, access_key, access_secret, is_active) VALUES (?, ?, ?, 1)",
            (alias, ak, sk)
        )
        conn.commit()
        conn.close()
        
        # 任务完成，清除 FSM 状态
        await state.clear()
        await wait_msg.delete()
        
        # 组装成功提示并重新调出账号选择键盘（新账号会瞬间出现在键盘上）
        success_text = (
            f"✅ **云账号添加成功！**\n\n"
            f"🏢 资源池别名: {alias}\n"
            f"🔑 识别码: {ak[:4]}****{ak[-4:] if len(ak)>8 else ''}\n\n"
            f"请在下方选择你要操作的账号："
        )
        
        # 重新渲染主界面
        await message.answer(
            success_text,
            reply_markup=get_account_selection_keyboard(),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await wait_msg.delete()
        await message.answer(f"❌ 数据库写入失败: {str(e)}\n请重试，或回复 /cancel 取消。")

    # 提示：下一步我们需要在这里激活一个 FSM 状态，等待用户打字回复

    
# =====================================================================
# ================= 🚀 新增服务器 (已关闭邮箱验证版) =================
# =====================================================================

@router.callback_query(F.data == "add_server")
async def trigger_add_server(callback: types.CallbackQuery, state: FSMContext):
    """处理【新增服务器】点击事件（直达选地域）"""
    # 1. 权限拦截
    if callback.from_user.id != config.ADMIN_ID: 
        return await callback.answer("权限不足！", show_alert=True)

    # 2. 确保当前 FSM 记忆里有刚选中的账号 ID
    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")
    if not account_id:
        return await callback.answer("⚠️ 会话已过期，请退回主菜单重新选择账号", show_alert=True)

    # 3. 检查是否有可用模板
    import db
    templates = db.get_all_templates()
    if not templates:
        await state.clear()
        return await callback.message.answer("⚠️ 当前没有任何配置好的云端启动模板，请先在系统设置中一键开荒后再试。")
        
    # 4. 直接跳过验证码，进入等待选地域阶段！
    await state.set_state(ServerManagement.waiting_for_region)
    await callback.message.answer(
        "🚀 已跳过安全验证。\n请选择您要部署新 ECS 服务器的目标地域：", 
        reply_markup=get_region_main_menu()
    )
    
    await callback.answer()


# ================= ↩️ 取消新增服务器并退回 =================
@router.callback_query(F.data == "cancel_add_server", StateFilter("*"))
async def process_cancel_add_server(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("🔄 正在返回...")
    
    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")
    
    await state.clear()
    
    if account_id:
        await state.update_data(current_account_id=account_id)
        # 🌟 重点在这里：克隆一个带新 data 的对象，而不是直接修改 callback.data
        fake_callback = callback.model_copy(update={"data": f"select_acc:{account_id}"})
        await process_account_selection(fake_callback, state)
    else:
        # 退回主菜单不需要传参数，直接原样传 callback 即可
        await process_back_to_accounts(callback, state)
  
# @router.message(ServerManagement.waiting_for_code)
# async def verify_add_server_code(message: types.Message, state: FSMContext):
#     """处理用户输入的验证码，并展示云端启动模板"""
#     if message.from_user.id != config.ADMIN_ID: return
    
#     user_input_code = message.text.strip()
    
#     # ==============================================================
#     # 🌟 修复漏洞三：新增退出机制，防止 FSM 死锁
#     # ==============================================================
#     if user_input_code.lower() == "/cancel":
#         await state.clear()
#         await message.answer("✅ 已取消新增服务器操作，您可以继续使用其他功能。")
#         return
#     # ==============================================================

#     user_data = await state.get_data()
    
#     if time.time() - user_data.get("timestamp", 0) > 300:
#         await state.clear()
#         return await message.answer("⚠️ 验证码已过期，请重新进入【💻 服务器管理】点击新增。")

#     if user_input_code == user_data.get("code"):
#         # ⚠️ 删除了 await state.clear()，以保护 current_account_id 往下传递
        
#         # 接入数据库模板逻辑
#         import db
#         templates = db.get_all_templates()
        
#         if not templates:
#             # 如果没有模板，说明业务跑不下去，这时候再 clear 并拦截
#             await state.clear()
#             return await message.answer("⚠️ 当前没有任何配置好的云端启动模板，请先在控制台或数据库中添加后再试。")
            
#         # 状态机完美转移：账号 ID 被安全保留，同时进入等待选地域阶段
#         await state.set_state(ServerManagement.waiting_for_region)
#         await message.answer(
#             "✅ 验证码核对无误！\n请选择您要部署新ECS服务器的目标地域：", 
#             reply_markup=get_region_main_menu()
#         )
#     else:
#         await message.answer("❌ 验证码错误，请核对邮箱后重新输入 6 位数字 (或回复 /cancel 取消)：")

@router.callback_query(ServerManagement.waiting_for_region, F.data.startswith("menu_"))
async def navigate_menus(callback: types.CallbackQuery):
    """处理二级菜单的动态折叠与展开"""
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    
    target_menu = callback.data
    
    if target_menu == "menu_main":
        await callback.message.edit_reply_markup(reply_markup=get_region_main_menu())
    elif target_menu == "menu_asia":
        await callback.message.edit_reply_markup(reply_markup=get_region_asia_menu())
    elif target_menu == "menu_eu_us":
        await callback.message.edit_reply_markup(reply_markup=get_region_eu_us_menu())
    elif target_menu == "menu_others":
        await callback.message.edit_reply_markup(reply_markup=get_region_others_menu())
        
    await callback.answer()

@router.callback_query(ServerManagement.waiting_for_region, F.data.startswith("region_"))
async def execute_run_instances(callback: types.CallbackQuery, state: FSMContext):
    """接收最终的地区选择，调用阿里云 API 自动化开机"""
    if callback.from_user.id != config.ADMIN_ID: return await callback.answer()
    
    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")
    
    if not account_id:
        await state.clear()
        return await callback.message.edit_text("⚠️ 账号上下文已丢失，请返回主菜单重新选择云账号。")
    
    region_id = callback.data.replace("region_", "")
    
    # ==========================================
    # 🌟 找回丢失的防呆补丁：完美拦截模板缺失报错
    # ==========================================
    try:
        template_id = get_template_id(account_id, region_id)
    except ValueError as e:
        # 如果抛出了模板找不到的错误，直接在屏幕上弹出一个强提醒！
        # 并且不清理状态机，让你可以继续停留在当前页面选别的地域
        return await callback.answer(str(e), show_alert=True)
    except Exception as e:
        return await callback.message.edit_text(f"❌ 读取模板失败: {e}")
        
    if not template_id:
        return await callback.message.edit_text(f"⚠️ 暂未在系统中配置 `{region_id}` 对应的启动模板，请配置后重试。")
    # ==========================================
        
    # 拿到账号、拿到模板，验证一切通过后，安全清理状态机
    await state.clear()
    progress_msg = await callback.message.edit_text(f"🚀 已跳过安全验证。正在向目标云账号的 `{region_id}` 下发创建任务，请耐心等待 (约需20-40秒)...")
    
    # ... 下方保留你原有的开机逻辑与 _sync_dbs，千万别删 ...
    
    # 🌟 2. 核心改变：把 account_id 作为第一个参数，传给底层的开机函数
    result = await asyncio.to_thread(create_ecs_instance_sync, account_id, region_id, template_id)
    
    if result["success"]:
        inst_id = result['instance_id']
        real_ip = result.get('ip', '0.0.0.0')

        # ==========================================
        # 🌟 修复入库断层：直接精准写入 ecs_business 表
        # ==========================================
        def _sync_dbs(ip, i_id, r_id):
            
            # 🌟 智能计算今天的日期作为默认账单重置日（最大不超过 28 号）
            current_day = min(datetime.now().day, 28)
            
            try:
                conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
                cursor = conn.cursor()
                
                # 🌟 强制将 reset_day 写入数据库，覆盖建表时的默认值 1
                cursor.execute("""
                    INSERT INTO ecs_business (instance_id, account_id, region_id, ip, name, reset_day) 
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET ip=excluded.ip
                """, (i_id, account_id, r_id, ip, f"实例-{i_id[-4:]}", current_day))
                
                # 资产总表的计数
                cursor.execute("""
                    INSERT INTO account_assets (account_id, region_id, running_count, stopped_count)
                    VALUES (?, ?, 1, 0)
                    ON CONFLICT(account_id, region_id) 
                    DO UPDATE SET running_count = running_count + 1
                """, (account_id, r_id))
                
                conn.commit()
                conn.close()
                print(f"✅ [开机同步] 实例 {i_id} (IP: {ip}, 账单日: {current_day}号) 已录入账本！")
            except Exception as e: 
                print(f"❌ [开机同步失败] 数据库写入异常: {e}")

        # 发射到后台执行，绝不卡顿主线程
        await asyncio.to_thread(_sync_dbs, real_ip, inst_id, region_id)
        # ==========================================
        
        node_config_btn = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⚙️ 节点配置 (选脚本)", callback_data=f"srv_sel:{inst_id}")]
        ])

        text = (
            f"🎉 **MG 控制台扩容成功！**\n\n"
            f"🌏 **地域**: `{region_id}`\n"
            f"🆔 **实例 ID**: `{inst_id}`\n"
            f"🌐 **公网 IP**: `{real_ip}`\n"
            f"✅ **状态**: 运行中\n\n"
            f"安全组与计费模式已按模板自动下发。 "
        )
        await progress_msg.edit_text(text, parse_mode="Markdown", reply_markup=node_config_btn)
    else:
        await progress_msg.edit_text(f"❌ **创建失败**\n\n原因: {result.get('error')}", parse_mode="Markdown")
        
    await callback.answer()

def get_single_instance_sync(instance_id: str) -> dict:
    """调用阿里云 API 获取单台机器的最新物理状态 (全自动追踪多账号、多地域)"""   
    # 1. 取出数据库中所有启用的云账号
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM cloud_accounts WHERE is_active = 1")
    accounts = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    # 2. 覆盖全球主流地域
    regions = GLOBAL_REGIONS
    
    # 3. 嵌套轮询：先遍历账号，再遍历地域
    for acc_id in accounts:
        for region_id in regions:
            try:
                # 🌟 核心：使用该账号专属的动态客户端！抛弃旧死密钥！
                client = get_dynamic_ecs_client(acc_id, region_id)
                req = ecs_models.DescribeInstancesRequest(
                    region_id=region_id, 
                    instance_ids=json.dumps([instance_id])
                )
                resp = client.describe_instances(req)
                
                if resp.body.instances and resp.body.instances.instance:
                    inst = resp.body.instances.instance[0]
                    ip = inst.public_ip_address.ip_address[0] if inst.public_ip_address.ip_address else "无公网IP"
                    creation_time = inst.creation_time.split('T')[0] if inst.creation_time else "未知"
                    return {
                        "id": inst.instance_id,
                        "ip": ip,
                        "status": inst.status,
                        "region": region_id,
                        "account_id": acc_id,  # 🌟 关键补丁：把这台机器属于哪个账号也返回去！
                        "creation_time": creation_time
                    }
            except Exception:
                continue # 这个账号或地域不对，找下一个
                
    print(f"查询单台实例失败: 在所有账号和地域中均未找到 {instance_id}")
    return None


def get_real_traffic_gb(instance_id: str, start_time_str: str) -> float:
    """调用阿里云 CMS 接口，拉取指定时间段内的出网总流量，并转换为 GB"""
    import sqlite3
    import config
    import json
    import time
    from datetime import datetime
    
    try:
        # 🌟 核心升级：连表查询，动态获取真实的 AK、SK 和 地域
        conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT c.access_key, c.access_secret, e.region_id 
            FROM ecs_business e 
            JOIN cloud_accounts c ON e.account_id = c.id 
            WHERE e.instance_id = ?
        ''', (instance_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            print(f"流量查询失败：本地账本未找到实例 {instance_id} 的归属账号。")
            return 0.0
            
        ak, sk, region_id = row
        ak = str(ak).strip()
        sk = str(sk).strip()

        start_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        start_ts = int(time.mktime(start_dt.timetuple()) * 1000)
        end_ts = int(time.time() * 1000)
        
        # 动态组装当前地域的 CMS endpoint
        ali_config = open_api_models.Config(
            access_key_id=ak,
            access_key_secret=sk,
            endpoint=f'metrics.{region_id}.aliyuncs.com'
        )
        client = CmsClient(ali_config)
        
        req = cms_models.DescribeMetricListRequest(
            namespace="acs_ecs_dashboard",
            metric_name="InternetOut",
            dimensions=json.dumps([{"instanceId": instance_id}]),
            start_time=str(start_ts),
            end_time=str(end_ts),
            period="3600"
        )
        resp = client.describe_metric_list(req)
        
        total_bytes = 0
        if resp.body.datapoints:
            datapoints = json.loads(resp.body.datapoints)
            for dp in datapoints:
                val = dp.get("Value", 0) or dp.get("Average", 0) 
                total_bytes += val
                
        total_gb = total_bytes / (1024 ** 3)
        return round(total_gb, 2)
    except Exception as e:
        print(f"获取实例 {instance_id} 流量失败: {e}")
        return 0.0

# ================= 核心：点击服务器 IP 展开详情面板 =================
@router.callback_query(F.data.startswith("manage_ecs_"))
async def process_manage_ecs(callback: types.CallbackQuery):
    instance_id = callback.data.replace("manage_ecs_", "")
    await callback.answer("🔄 正在加载服务器深度数据...")
    
    ali_data = await asyncio.to_thread(get_single_instance_sync, instance_id)
    biz_data = db.get_business_data(instance_id)
    
    if not ali_data:
        await callback.message.answer("❌ 无法从阿里云获取该实例的数据，可能已被释放。")
        return

    status_str = "🟢 运行中" if ali_data['status'] == 'Running' else "🔴 已关机"
    if ali_data['status'] in ['Starting', 'Pending']: status_str = "🔵 正在开机中..."
    if ali_data['status'] in ['Stopping']: status_str = "🔵 正在关机中..."

    start_time_str = biz_data.get('traffic_start_time')
    if not start_time_str:
        now = datetime.now()
        start_time_str = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        db.update_business_data(instance_id, "traffic_start_time", start_time_str)

    current_used_traffic = await asyncio.to_thread(get_real_traffic_gb, instance_id, start_time_str)
    
    # 🌟 1. 自动提取这台机器的开机“日” (例如 "2026-07-21" 提取出 21)
    try:
        creation_day = int(ali_data['creation_time'].split('-')[2])
    except:
        creation_day = 1  # 提取失败的容错

    # 🌟 2. 获取设定的重置日，如果没单独设定过，就【默认跟随开机日】！
    reset_day = biz_data.get('reset_day')
    if not reset_day:
        reset_day = creation_day

    # 🌟 3. 智能生成高端的展示文案
    if int(reset_day) == 1:
        reset_display = "自然月 (每月 1 号重置)"
    elif int(reset_day) == creation_day:
        reset_display = f"跟随开机日 (每月 {reset_day} 号重置)"
    else:
        reset_display = f"自定义 (每月 {reset_day} 号重置)"
        
    # 🌟 4. 动态推算客户业务到期时间 (如果数据库为空，则自动推算)
    expire_time_str = biz_data.get('expire_time')
    if not expire_time_str or str(expire_time_str).strip() in ["", "None", "未知日期"]:
        try:
            # 截取开机时间的前10位 (YYYY-MM-DD)
            start_date_str = str(ali_data['creation_time'])[:10]
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            now = datetime.now()
            
            target_month = now.month
            target_year = now.year
            # 以重置日（或开机日）作为自然月锚点
            anchor_day = int(reset_day)
            
            # 如果今天的日期已经超过或等于锚点日，说明推算到下个月
            if now.day >= anchor_day:
                target_month += 1
                if target_month > 12:
                    target_month = 1
                    target_year += 1
                    
            # 防溢出：处理月末没有30/31号的情况
            last_day = calendar.monthrange(target_year, target_month)[1]
            final_day = min(anchor_day, last_day)
            
            expire_time_str = datetime(target_year, target_month, final_day).strftime("%Y-%m-%d")
        except Exception as e:
            expire_time_str = "时间推算失败"

    # 组装最终展示文案
    text = (
        "📊 **ECS 实例详情**\n\n"
        f"🌍 地域: `{ali_data['region']}`\n"
        f"🆔 实例 ID: `{ali_data['id']}`\n"
        f"🌐 公网 IP: `{ali_data['ip']}`\n"
        f"✅ 状态: {status_str}\n"
        f"📶 本期出网流量: `{current_used_traffic} GB` / `{biz_data['traffic_limit_gb']} GB` (阈值95%防刷断网)\n"
        f"📅 服务器开机时间: `{ali_data['creation_time']}`\n"
        f"⏳ 流量重置周期: `{reset_display}`\n"
        f"👤 客户业务到期: `{expire_time_str}`\n"  # 🌟 这里替换成了动态计算出的变量
    )

    builder = InlineKeyboardBuilder()
    if ali_data['status'] == 'Running':
        builder.row(InlineKeyboardButton(text="🛑 关机", callback_data=f"power_stop_{instance_id}"))
    else:
        builder.row(InlineKeyboardButton(text="🟢 开机", callback_data=f"power_start_{instance_id}"))
        
    builder.row(
        InlineKeyboardButton(text="💰 续费选项", callback_data=f"renew_menu_{instance_id}"),
        InlineKeyboardButton(text="⚙️ 流量限制", callback_data=f"set_traffic_{instance_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔄 重装系统", callback_data=f"reinstall_os_{instance_id}"),
        InlineKeyboardButton(text="🚀 带宽设置", callback_data=f"set_bandwidth_{instance_id}")
    )
    builder.row(
        InlineKeyboardButton(text="⏳ 修改重置日", callback_data=f"set_resetday_{instance_id}"),
        InlineKeyboardButton(text="🗑️ 释放服务器", callback_data=f"release_ecs_{instance_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"select_acc:{ali_data['account_id']}"))

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# =====================================================================
# ================= 2. 二级子菜单：全套商业化控制面板 =================
# =====================================================================

@router.callback_query(F.data.startswith("renew_menu_"))
async def process_renew_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("renew_menu_", "")
    await callback.answer()
    
    text = (
        f"⏳ **实例续费管理**\n\n"
        f"🆔 `{instance_id}`\n\n"
        f"💡 **计费规则：按自然月精准叠加**\n"
        f"例如客户 8 号到期，提前在 5 号续费，新到期日将自动累加至下个月 8 号，不吞天数。\n\n"
        f"• 💰 **全部续费**：阿里云物理续费 + 本地客户顺延 1 个月\n"
        f"• ☁️ **仅阿里云续费**：仅物理机续费（囤机器）\n"
        f"• 👤 **仅客户续费**：仅修改本地账本，顺延 1 个月"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 全部续费 (双端同步)", callback_data=f"action_renew_all_{instance_id}"))
    builder.row(
        InlineKeyboardButton(text="☁️ 仅阿里云", callback_data=f"action_renew_ali_{instance_id}"),
        InlineKeyboardButton(text="👤 仅客户", callback_data=f"action_renew_client_{instance_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("set_traffic_"))
async def process_set_traffic(callback: types.CallbackQuery):
    instance_id = callback.data.replace("set_traffic_", "")
    await callback.answer()
    biz_data = db.get_business_data(instance_id)
    limit = biz_data['traffic_limit_gb']
    
    text = (
        f"⚙️ **流量限额与风控熔断**\n\n"
        f"当前客户配额: `{limit} GB` / 月\n"
        f"到达 `{int(limit*0.8)} GB` 触发私聊预警\n"
        f"到达 `{int(limit*0.95)} GB` 触发物理断网（关机）\n\n"
        f"💡 *中途客户加钱买流量包？直接调高配额即可解封。*"
    )
    builder = InlineKeyboardBuilder()
    btn_500 = "🟢 500 GB" if limit == 500 else "⚪ 500 GB"
    btn_1000 = "🟢 1000 GB" if limit == 1000 else "⚪ 1000 GB"
    builder.row(
        InlineKeyboardButton(text=btn_500, callback_data=f"action_tfsize_{instance_id}_500"),
        InlineKeyboardButton(text=btn_1000, callback_data=f"action_tfsize_{instance_id}_1000")
    )
    builder.row(InlineKeyboardButton(text="✏️ 自定义配额 (输入数字)", callback_data=f"input_tflimit_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("set_resetday_"))
async def process_resetday_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("set_resetday_", "")
    await callback.answer("🔄 正在加载账单配置...")  # 增加一个友好的加载提示
    
    biz_data = db.get_business_data(instance_id)
    
    # 🌟 1. 动态获取一下阿里云的物理数据，为了拿到开机时间
    ali_data = await asyncio.to_thread(get_single_instance_sync, instance_id)
    if not ali_data:
        await callback.message.answer("❌ 无法从云端读取实例创建时间。")
        return
        
    start_time_str = biz_data.get('traffic_start_time') or "跟随系统开机"
    
    # 🌟 2. 保持和主面板 100% 一致的智能判断逻辑
    try:
        creation_day = int(ali_data['creation_time'].split('-')[2])
    except:
        creation_day = 1

    reset_day = biz_data.get('reset_day')
    if not reset_day:
        reset_day = creation_day

    if int(reset_day) == 1:
        reset_display = "自然月 (每月 1 号重置)"
    elif int(reset_day) == creation_day:
        reset_display = f"跟随开机日 (每月 {reset_day} 号重置)"
    else:
        reset_display = f"自定义 (每月 {reset_day} 号重置)"
    
    # 🌟 3. 渲染菜单
    text = (
        f"⏳ **账期重置与流量清零**\n\n"
        f"📅 当前账单锚点: `{reset_display}`\n"
        f"⏱️ 本期流量起点: `{start_time_str}`\n\n"
        f"💡 **客户中途流量用尽怎么处理？**\n"
        f"不要动锚点日，直接点击下方【重置当月流量】。系统会将统计起点更新为此刻，之前跑的流量瞬间清零，且下个月依然按锚点日正常循环！"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔄 立即重置当月流量 (清零)", callback_data=f"action_cleartf_{instance_id}"))
    builder.row(InlineKeyboardButton(text="✏️ 修改账单锚点日 (1-28)", callback_data=f"input_resetday_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("reinstall_os_"))
async def process_reinstall_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("reinstall_os_", "")
    await callback.answer()
    
    text = (
        f"🔄 **重装系统 (高危)**\n\n"
        f"⚠️ 警告：重装系统将**彻底抹除**该服务器系统盘上的所有数据，且不可逆！\n"
        f"👉 阿里云要求：执行重装前，**必须先将服务器关机**。"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⚠️ 确认重装为 Debian 12", callback_data=f"action_reinstall_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 怂了，返回详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("set_bandwidth_"))
async def process_bandwidth_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("set_bandwidth_", "")
    await callback.answer()
    text = f"🚀 **公网带宽峰值调整**\n\n请选择需要调整的临时或永久带宽峰值："
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="30 Mbps", callback_data=f"action_bw_{instance_id}_30"),
        InlineKeyboardButton(text="50 Mbps", callback_data=f"action_bw_{instance_id}_50")
    )
    builder.row(
        InlineKeyboardButton(text="100 Mbps", callback_data=f"action_bw_{instance_id}_100"),
        InlineKeyboardButton(text="200 Mbps", callback_data=f"action_bw_{instance_id}_200")
    )
    builder.row(InlineKeyboardButton(text="✏️ 自定义带宽", callback_data=f"input_bw_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("release_ecs_"))
async def process_release_menu(callback: types.CallbackQuery):
    instance_id = callback.data.replace("release_ecs_", "")
    await callback.answer()
    text = (
        f"🗑️ **释放服务器 (极其危险)**\n\n"
        f"🆔 实例 ID: `{instance_id}`\n\n"
        f"⚠️ **您正在执行不可逆的销毁操作！**\n"
        f"点击确认后，阿里云将立刻回收该物理机，IP 释放，所有数据彻底灰飞烟灭，且停止计费。"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔥 确认永久销毁该服务器", callback_data=f"action_release_{instance_id}"))
    builder.row(InlineKeyboardButton(text="🔙 怂了，返回详情", callback_data=f"manage_ecs_{instance_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# =====================================================================
# ================= 新增：开机关机控制模块 ============================
# =====================================================================

@router.callback_query(F.data.startswith("power_stop_"))
async def execute_power_stop(callback: types.CallbackQuery):
    instance_id = callback.data.replace("power_stop_", "")
    await callback.message.edit_text(f"🛑 正在向阿里云下发强制关机指令...\n🆔 `{instance_id}`")
    
    def _do_stop():
        from handlers.common import get_dynamic_ecs_client
        from alibabacloud_ecs20140526 import models as ecs_models
        
        # 自动定位该机器属于哪个账号、哪个地域
        inst_info = get_single_instance_sync(instance_id)
        if not inst_info:
            raise Exception("无法在云端定位到该实例所属账号/地域")
            
        client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
        # 强制关机请求
        req = ecs_models.StopInstanceRequest(
            instance_id=instance_id,
            force_stop=True  # 强制关机，类似拔电源，速度更快
        )
        return client.stop_instance(req)
        
    try:
        await asyncio.to_thread(_do_stop)
        
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回刷新服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        await callback.message.edit_text(
            f"✅ **关机指令已成功下发！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"⏳ 阿里云正在执行物理断电，约需 10~20 秒。\n"
            f"请稍后点击下方按钮刷新状态。",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ **关机失败**\n\n原因：\n`{e}`")


@router.callback_query(F.data.startswith("power_start_"))
async def execute_power_start(callback: types.CallbackQuery):
    instance_id = callback.data.replace("power_start_", "")
    await callback.message.edit_text(f"🟢 正在向阿里云下发开机指令...\n🆔 `{instance_id}`")
    
    def _do_start():
        from handlers.common import get_dynamic_ecs_client
        from alibabacloud_ecs20140526 import models as ecs_models
        
        inst_info = get_single_instance_sync(instance_id)
        if not inst_info:
            raise Exception("无法在云端定位到该实例所属账号/地域")
            
        client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
        req = ecs_models.StartInstanceRequest(instance_id=instance_id)
        return client.start_instance(req)
        
    try:
        await asyncio.to_thread(_do_start)
        
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回刷新服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        await callback.message.edit_text(
            f"✅ **开机指令已成功下发！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"⏳ 服务器正在启动操作系统，约需 20~40 秒后网络恢复联通。\n"
            f"请稍后点击下方按钮刷新状态。",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ **开机失败**\n\n原因：\n`{e}`")


# =====================================================================
# ================= 3. 危险动作执行区：重装与释放 =====================
# =====================================================================

@router.callback_query(F.data.startswith("action_reinstall_"))
async def execute_reinstall(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_reinstall_", "")
    await callback.message.edit_text("🔄 正在执行自动化重装连招（检测状态 ➡️ 关机 ➡️ 重装 ➡️ 开机），请耐心等待...")
    
    # --- 以下是封装的同步 API 调用函数 ---
    def _get_ecs_client_and_info():
        from handlers.common import get_dynamic_ecs_client
        # 移除了错误的导入，直接使用 server.py 全局环境中的 get_single_instance_sync
        inst_info = get_single_instance_sync(instance_id)
        if not inst_info: raise Exception("无法在云端定位到该实例")
        client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
        return client, inst_info['region']

    def _get_instance_status(client, region):
        from alibabacloud_ecs20140526 import models as ecs_models
        import json
        req = ecs_models.DescribeInstancesRequest(region_id=region, instance_ids=json.dumps([instance_id]))
        resp = client.describe_instances(req)
        if not resp.body.instances.instance: raise Exception("未找到该实例")
        inst = resp.body.instances.instance[0]
        return inst.status, inst.image_id
        
    def _stop_instance(client):
        from alibabacloud_ecs20140526 import models as ecs_models
        req = ecs_models.StopInstanceRequest(instance_id=instance_id)
        client.stop_instance(req)

    def _replace_disk(client, image_id):
        from alibabacloud_ecs20140526 import models as ecs_models
        req = ecs_models.ReplaceSystemDiskRequest(
            instance_id=instance_id,
            image_id=image_id,
            password="@QS00008"
        )
        client.replace_system_disk(req)

    def _start_instance(client):
        from alibabacloud_ecs20140526 import models as ecs_models
        req = ecs_models.StartInstanceRequest(instance_id=instance_id)
        client.start_instance(req)

    # --- 以下是异步全自动编排流程 ---
    try:
        # 获取客户端与当前状态
        client, region = await asyncio.to_thread(_get_ecs_client_and_info)
        status, image_id = await asyncio.to_thread(_get_instance_status, client, region)
        
        # 1. 关机逻辑
        if status == "Running":
            await callback.message.edit_text("🔄 检测到机器正在运行，正在下发【自动关机】指令...")
            await asyncio.to_thread(_stop_instance, client)
            
            # 轮询等待关机完成 (每5秒查询一次)
            while True:
                await asyncio.sleep(5)
                status, _ = await asyncio.to_thread(_get_instance_status, client, region)
                if status == "Stopped":
                    break
                # 如果是 Stopping 状态则继续等
        
        # 2. 重装逻辑
        await callback.message.edit_text("🔄 机器已关机，正在下发【系统重装】指令...")
        await asyncio.to_thread(_replace_disk, client, image_id)
        
        # 缓冲等待几秒，确保阿里云服务端彻底受理磁盘替换
        await asyncio.sleep(5)
        
        # 3. 开机逻辑
        await callback.message.edit_text("🔄 系统盘替换成功，正在执行【自动开机】...")
        await asyncio.to_thread(_start_instance, client)
        
        # 构建返回按钮
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        # 输出最终成功信息
        await callback.message.edit_text(
            f"✅ **全自动重装连招已完成！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🔑 默认密码: `@QS00008`\n"
            f"🚀 机器正在开机中，请等待 1-2 分钟后尝试使用 SSH 连接。",
            reply_markup=builder.as_markup()
        )

    except Exception as e:
        error_msg = str(e)
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        await callback.message.edit_text(f"❌ **全自动重装失败**\n\n原因：\n`{error_msg}`", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("action_release_"))
async def execute_release(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_release_", "")
    await callback.message.edit_text("🗑️ 正在执行强制销毁程序...\n1️⃣ 尝试转换计费方式为按量付费\n2️⃣ 尝试执行物理销毁")
    
    # 🌟 统一前置获取：提前拿到 inst_info，不仅省去重复查询，还避免了作用域混乱
    inst_info = await asyncio.to_thread(get_single_instance_sync, instance_id)
    if not inst_info:
        return await callback.message.edit_text(f"❌ **释放失败**\n\n无法在云端定位到该实例 `{instance_id}`，可能已被销毁或账本不同步。")

    def _do_convert():
        # 直接使用外部拿到的 inst_info
        client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
        try:
            req_convert = ecs_models.ModifyInstanceChargeTypeRequest(
                region_id=inst_info['region'],
                instance_ids=json.dumps([instance_id]),
                instance_charge_type="PostPaid"
            )
            client.modify_instance_charge_type(req_convert)
        except Exception:
            pass

    def _do_delete():
        # 直接使用外部拿到的 inst_info
        client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
        req_delete = ecs_models.DeleteInstanceRequest(
            instance_id=instance_id,
            force=True
        )
        return client.delete_instance(req_delete)

    def _clean_local_db():
        import sqlite3, db
        conn = sqlite3.connect(db.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ecs_business WHERE instance_id = ?", (instance_id,))
        conn.commit()
        conn.close()

    try:
        # 1. 尝试转付费类型
        await asyncio.to_thread(_do_convert)
        # 2. 等待阿里云后台数据生效
        await asyncio.sleep(2)
        # 3. 真正执行物理销毁
        await asyncio.to_thread(_do_delete)
        # 4. 清理数据库
        await asyncio.to_thread(_clean_local_db)

        await callback.message.edit_text(f"🔥 **服务器已被永久释放！**\n\n🆔 实例: `{instance_id}`\n本地业务数据已同步清理。")
    except Exception as e:
        await callback.message.edit_text(f"❌ **释放失败**\n\n原因：\n`{e}`")

# =====================================================================
# ================= 4. FSM 状态机：等待与处理用户输入 =================
# =====================================================================

class ServerFSM(StatesGroup):
    wait_for_traffic = State()
    wait_for_reset_day = State()
    wait_for_bandwidth = State()

@router.callback_query(F.data.startswith("input_tflimit_"))
async def ask_traffic_limit(callback: types.CallbackQuery, state: FSMContext):
    instance_id = callback.data.replace("input_tflimit_", "")
    await state.update_data(target_instance=instance_id)
    await state.set_state(ServerFSM.wait_for_traffic)
    
    await callback.message.answer(
        f"✏️ 请直接回复想要为实例 `{instance_id}` 设置的**当月流量限额 (GB)**：\n"
        f"*(请输入纯数字，例如 1500)*"
    )
    await callback.answer()

@router.message(ServerFSM.wait_for_traffic)
async def receive_traffic_limit(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ 格式错误！只能输入纯数字，请重新输入：")
        return
        
    new_limit = int(message.text)
    data = await state.get_data()
    instance_id = data.get("target_instance")
    
    import db
    db.update_business_data(instance_id, "traffic_limit_gb", new_limit)
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 验证一下：返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await message.answer(f"✅ 成功！实例 `{instance_id}` 的流量配额已修改为 **{new_limit} GB**。", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("input_resetday_"))
async def ask_reset_day(callback: types.CallbackQuery, state: FSMContext):
    instance_id = callback.data.replace("input_resetday_", "")
    await state.update_data(target_instance=instance_id)
    await state.set_state(ServerFSM.wait_for_reset_day)
    
    await callback.message.answer(
        f"📅 请回复您想设置的**每月重置日期 (1-28)**：\n"
        f"*(建议不要设置 29-31，因为 2 月没有这几天)*"
    )
    await callback.answer()

@router.message(ServerFSM.wait_for_reset_day)
async def receive_reset_day(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or not (1 <= int(message.text) <= 28):
        await message.answer("❌ 格式错误！请输入 1 到 28 之间的数字：")
        return
        
    new_day = int(message.text)
    data = await state.get_data()
    instance_id = data.get("target_instance")
    
    import db
    db.update_business_data(instance_id, "reset_day", new_day)
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
    await message.answer(f"✅ 成功！实例的账单重置日已修改为 每月 **{new_day}** 号。", reply_markup=builder.as_markup())

# =====================================================================
# ================= 5. 执行动作：公网带宽动态调整 =====================
# =====================================================================

@router.callback_query(F.data.startswith("action_bw_"))
async def execute_set_bandwidth_fixed(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    instance_id = parts[2]
    bw_size = int(parts[3])
    
    await callback.message.edit_text(f"🚀 正在向阿里云提交申请，调整带宽至 **{bw_size} Mbps**...")
    
    def _do_modify_bw():
        inst_info = get_single_instance_sync(instance_id)
        if not inst_info: raise Exception("无法在云端定位到该实例")
        
        # 直接拿动态客户端！超级简洁！
        client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
        req = ecs_models.ModifyInstanceNetworkSpecRequest(
            instance_id=instance_id,
            internet_max_bandwidth_out=bw_size
        )
        return client.modify_instance_network_spec(req)
        
    try:
        await asyncio.to_thread(_do_modify_bw)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        await callback.message.edit_text(
            f"✅ **带宽调整成功！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🚀 当前公网出网带宽峰值: **{bw_size} Mbps**\n\n"
            f"💡 *提示：配置已即时生效，业务未中断，无需重启服务器。*",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ **带宽调整失败**\n\n原因可能是账户欠费或购买额度受限：\n`{e}`")


@router.callback_query(F.data.startswith("input_bw_"))
async def ask_custom_bandwidth(callback: types.CallbackQuery, state: FSMContext):
    instance_id = callback.data.replace("input_bw_", "")
    await state.update_data(target_instance=instance_id)
    await state.set_state(ServerFSM.wait_for_bandwidth)
    
    await callback.message.answer(
        f"🚀 请直接回复您想为实例 `{instance_id}` 设置的**自定义带宽峰值 (Mbps)**：\n"
        f"*(请输入 1 到 200 之间的纯数字)*"
    )
    await callback.answer()


@router.message(ServerFSM.wait_for_bandwidth)
async def receive_custom_bandwidth(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ 格式错误！请输入大于 0 的纯数字：")
        return
        
    bw_size = int(message.text)
    data = await state.get_data()
    instance_id = data.get("target_instance")
    await state.clear()
    
    progress_msg = await message.answer(f"🚀 正在向阿里云提交申请，调整带宽至 **{bw_size} Mbps**...")
    
    def _do_modify_bw():
        import sqlite3
        import config
        
        # 🌟 核心升级：连表查询，动态获取真实的 AK、SK 和 地域
        conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT c.access_key, c.access_secret, e.region_id 
            FROM ecs_business e 
            JOIN cloud_accounts c ON e.account_id = c.id 
            WHERE e.instance_id = ?
        ''', (instance_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            raise Exception("无法在本地账本中找到该实例的云账号归属记录。")
            
        ak, sk, region_id = row
        ak = str(ak).strip()
        sk = str(sk).strip()

        # 动态化 endpoint，解除写死的 cn-hongkong
        ali_config = open_api_models.Config(
            access_key_id=ak,
            access_key_secret=sk,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        client = EcsClient(ali_config)
        req = ecs_models.ModifyInstanceNetworkSpecRequest(
            instance_id=instance_id,
            internet_max_bandwidth_out=bw_size
        )
        return client.modify_instance_network_spec(req)
        
    try:
        await asyncio.to_thread(_do_modify_bw)
        
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        
        await progress_msg.delete()
        await message.answer(
            f"✅ **自定义带宽调整成功！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🚀 当前公网出网带宽峰值: **{bw_size} Mbps**",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        await progress_msg.delete()
        await message.answer(f"❌ **带宽调整失败**\n\n原因：\n`{e}`")

# =====================================================================
# ================= 6. 执行动作：双端续费核心逻辑 =====================
# =====================================================================

def _extend_client_time(instance_id: str) -> str:
    """本地计算核心：为客户精准增加 1 个自然月"""
    import db
    biz_data = db.get_business_data(instance_id)
    current_expire = biz_data.get('expire_time', '')
    now = datetime.now()
    
    if not current_expire:
        base_date = now
    else:
        try:
            base_date = datetime.strptime(current_expire, "%Y-%m-%d")
            if base_date < now:
                base_date = now 
        except ValueError:
            base_date = now
            
    new_expire = base_date + relativedelta(months=1)
    new_expire_str = new_expire.strftime("%Y-%m-%d")
    
    db.update_business_data(instance_id, "expire_time", new_expire_str)
    db.update_business_data(instance_id, "traffic_start_time", now.strftime("%Y-%m-%d %H:%M:%S"))
    
    return new_expire_str

def _renew_aliyun_instance(instance_id: str):
    """向阿里云发起真实的物理机续费请求"""
    inst_info = get_single_instance_sync(instance_id)
    if not inst_info: raise Exception("无法在云端定位到该实例")
    
    client = get_dynamic_ecs_client(inst_info['account_id'], inst_info['region'])
    req = ecs_models.RenewInstanceRequest(
        instance_id=instance_id,
        period=1 
    )
    return client.renew_instance(req)


@router.callback_query(F.data.startswith("action_renew_all_"))
async def execute_renew_all(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_renew_all_", "")
    await callback.message.edit_text("🔄 正在向阿里云提交续费订单，并更新本地账单...")
    
    try:
        await asyncio.to_thread(_renew_aliyun_instance, instance_id)
        # 🛠️ 修正 4：把本地写数据库操作放入后台线程池执行，保障在高并发或数据库大锁时，主循环依然丝滑
        new_expire = await asyncio.to_thread(_extend_client_time, instance_id)
        
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        await callback.message.edit_text(
            f"✅ **全部续费成功！**\n\n"
            f"💰 阿里云物理机已续费 1 个月。\n"
            f"👤 客户业务期已顺延，新到期日：`{new_expire}`\n"
            f"📶 本期流量统计已重新清零起算。",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        if "InvalidInstanceChargeType" in str(e):
            await callback.message.edit_text("❌ **阿里云续费失败：当前机器是按量付费，无法使用包月续费接口。**\n*(正式交付客户时请开通包年包月机器)*")
        else:
            await callback.message.edit_text(f"❌ **阿里云续费失败，本地账单未改变：**\n`{e}`")


@router.callback_query(F.data.startswith("action_renew_ali_"))
async def execute_renew_ali(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_renew_ali_", "")
    await callback.message.edit_text("🔄 正在向阿里云提交续费订单...")
    
    try:
        await asyncio.to_thread(_renew_aliyun_instance, instance_id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器详情", callback_data=f"manage_ecs_{instance_id}"))
        await callback.message.edit_text(
            f"✅ **阿里云物理续费成功！**\n*(本地客户到期时间和流量保持不变)*",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        if "InvalidInstanceChargeType" in str(e):
            await callback.message.edit_text("❌ 失败：按量付费测试机不可包月续费。")
        else:
            await callback.message.edit_text(f"❌ 失败：\n`{e}`")


@router.callback_query(F.data.startswith("action_renew_client_"))
async def execute_renew_client(callback: types.CallbackQuery):
    instance_id = callback.data.replace("action_renew_client_", "")
    
    # 🛠️ 修正 5：同步方法转异步调用，防止阻塞
    new_expire = await asyncio.to_thread(_extend_client_time, instance_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 验证一下：返回详情面板", callback_data=f"manage_ecs_{instance_id}"))
    
    await callback.message.edit_text(
        f"✅ **客户业务续费成功！**\n\n"
        f"👤 客户新到期日已顺延至：`{new_expire}`\n"
        f"📶 本期客户流量已自动清零。\n"
        f"*(阿里云底层未产生扣费动作)*",
        reply_markup=builder.as_markup()
    )
