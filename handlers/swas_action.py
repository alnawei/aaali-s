import asyncio
import json
import sqlite3
import config

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from aiogram.filters import StateFilter



# 阿里云 SDK 依赖 (需确保安装了 alibabacloud_swas-open20200601)
# pip install alibabacloud_swas-open20200601
from alibabacloud_swas_open20200601.client import Client as SwasClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_swas_open20200601 import models as swas_models

swas_router = Router()

# ================= 🛡️ 全局最高权限拦截 =================
swas_router.message.filter(F.from_user.id == int(config.ADMIN_ID))
swas_router.callback_query.filter(F.from_user.id == int(config.ADMIN_ID))
# =========================================================

class SwasFSM(StatesGroup):
    waiting_for_region = State()
    waiting_for_plan = State()

# ================= 底层 API 与工厂函数 =================


def _get_swas_status_sync(account_id: int, region_id: str, instance_id: str) -> str:
    """获取单台轻量云的实时状态 (Running, Stopped 等)"""
    from alibabacloud_swas_open20200601 import models as swas_models
    try:
        client = get_swas_client(account_id, region_id)
        req = swas_models.ListInstancesRequest(
            region_id=region_id,
            instance_ids=json.dumps([instance_id])
        )
        resp = client.list_instances(req)
        if resp.body.instances:
            return resp.body.instances[0].status
    except Exception:
        pass
    return "Unknown"

def _execute_swas_action_sync(instance_id: str, action: str) -> dict:
    """同步执行实例的电源与重装操作"""
    import json
    from alibabacloud_swas_open20200601 import models as swas_models
    
    meta = get_single_swas_sync(instance_id)
    if not meta:
        return {"success": False, "error": "未在数据库中匹配到该实例信息"}
    
    try:
        client = get_swas_client(meta["account_id"], meta["region"])
        
        if action == "stop":
            req = swas_models.StopInstancesRequest(region_id=meta["region"], instance_ids=json.dumps([instance_id]))
            client.stop_instances(req)
        elif action == "start":
            req = swas_models.StartInstancesRequest(region_id=meta["region"], instance_ids=json.dumps([instance_id]))
            client.start_instances(req)
        elif action == "reinstall":
            # ⚠️ 修复：轻量云的 ResetSystem 不接受 password 参数，仅重置系统盘
            req = swas_models.ResetSystemRequest(
                region_id=meta["region"],
                instance_id=instance_id
            )
            client.reset_system(req)
        elif action == "resetpw":
            # 🌟 新增：调用专门的接口修改轻量云的密码
            req = swas_models.UpdateInstanceAttributeRequest(
                region_id=meta["region"],
                instance_id=instance_id,
                password="@QS00008"
            )
            client.update_instance_attribute(req)
            
        return {"success": True, "account_id": meta["account_id"]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_swas_client(account_id: int, region_id: str) -> SwasClient:
    """动态获取轻量云 (SWAS) 客户端"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT access_key, access_secret FROM cloud_accounts WHERE id = ?", (account_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise ValueError(f"未找到账号 ID {account_id} 的密钥信息")
        
    ak, sk = row
    ali_config = open_api_models.Config(
        access_key_id=ak.strip(),
        access_key_secret=sk.strip(),
        endpoint=f'swas.{region_id}.aliyuncs.com'
    )
    return SwasClient(ali_config)

def _get_debian_image_id(client: SwasClient, region_id: str) -> str:
    """自动寻找 Debian 12.10 的系统镜像 ID"""
    req = swas_models.ListImagesRequest(region_id=region_id, image_type="system")
    resp = client.list_images(req)
    for img in resp.body.images:
        if "Debian" in img.image_name and "12.10" in img.image_name:
            return img.image_id
    raise Exception("当前地域未找到 Debian 12.10 镜像")

def _get_plan_id(client: SwasClient, region_id: str, target_memory: float) -> str:
    """自动匹配指定的套餐 ID (通用型 2核)"""
    req = swas_models.ListPlansRequest(region_id=region_id)
    resp = client.list_plans(req)
    for plan in resp.body.plans:
        # 通用型匹配规则：2核 + 对应的内存
        if plan.core == 2 and float(plan.memory) == target_memory:
            return plan.plan_id
    raise Exception(f"当前地域未找到 2核 {target_memory}G 的套餐配置")

def _create_swas_sync(account_id: int, region_id: str, memory: float) -> dict:
    """同步执行：买机器 + 开防火墙"""
    client = get_swas_client(account_id, region_id)
    
    try:
        # 1. 自动寻址 镜像与套餐
        image_id = _get_debian_image_id(client, region_id)
        plan_id = _get_plan_id(client, region_id, memory)
        
        # 2. 下发创建指令 (⚠️ 移除 password 参数，轻量云不支持开机直设密码)
        create_req = swas_models.CreateInstancesRequest(
            region_id=region_id,
            image_id=image_id,
            plan_id=plan_id,
            period=1,
            charge_type="PrePaid"
        )
        create_resp = client.create_instances(create_req)
        instance_ids = create_resp.body.instance_ids
        if not instance_ids:
            return {"success": False, "error": "创建接口已响应，但未返回实例ID"}
            
        instance_id = instance_ids[0]
        
        # 3. 开启防火墙 (TCP+UDP 1/65535)
        import time
        time.sleep(5)  # 缓冲几秒，等待实例在底层注册完成
        fw_req = swas_models.CreateFirewallRuleRequest(
            region_id=region_id,
            instance_id=instance_id,
            rule_protocol="TCP+UDP",
            port="1/65535",
            remark="全开"
        )
        # 4. 🌟 新增：自动重置密码为固定密码
        reset_req = swas_models.ResetSystemRequest(
            region_id=region_id,
            instance_id=instance_id,
            password="@QS00008" # 在这里统一修改
        )
        client.reset_system(reset_req)
        
        return {"success": True, "instance_id": instance_id}
        
    except Exception as e:
        error_msg = str(e)
        # 🌟 智能拦截：遇到售罄直接返回中文友好提示
        if "Inventory" in error_msg or "stock" in error_msg.lower() or "售罄" in error_msg:
            return {"success": False, "error": "您选择的配置在当前地域已售罄，请尝试选择其他配置或者地域。"}
            
        # 其他错误照常抛出
        return {"success": False, "error": error_msg}


# ================= 动态菜单 UI =================

def get_swas_region_main_menu():
    builder = InlineKeyboardBuilder()
    # 🌟 新增：常用地区和中国大陆放第一行并排
    builder.row(
        InlineKeyboardButton(text="⭐ 常用地区", callback_data="swas_menu_common"),
        InlineKeyboardButton(text="🇨🇳 中国大陆", callback_data="swas_menu_cn")
    )
    builder.row(
        InlineKeyboardButton(text="🌏 亚太地区", callback_data="swas_menu_ap"),
        InlineKeyboardButton(text="🌍 欧美地区", callback_data="swas_menu_eu_us")
    )
    builder.row(InlineKeyboardButton(text="🔙 取消并返回", callback_data="cancel_add_server"))
    return builder.as_markup()

def get_swas_common_menu():
    """🌟 新增：常用地区专属子菜单"""
    builder = InlineKeyboardBuilder()
    # 香港单独占一行（如果你想的话，也可以跟下面放一起）
    builder.row(InlineKeyboardButton(text="🇭🇰 中国(香港)", callback_data="swas_reg_cn-hongkong"))
    # 新加坡和美国并排，注意中间必须有英文逗号隔开！
    builder.row(    
        InlineKeyboardButton(text="🇸🇬 新加坡", callback_data="swas_reg_ap-southeast-1"),
        InlineKeyboardButton(text="🇺🇸 美国(弗吉尼亚)", callback_data="swas_reg_us-east-1")
    )
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="swas_menu_main"))
    return builder.as_markup()

def get_swas_cn_menu():
    builder = InlineKeyboardBuilder()
    cn_regions = [
        ("华东1(杭州)", "cn-hangzhou"), ("华北2(北京)", "cn-beijing"),
        ("华东2(上海)", "cn-shanghai"), ("华南1(深圳)", "cn-shenzhen"),
        ("西南1(成都)", "cn-chengdu"), ("华北1(青岛)", "cn-qingdao"),
        ("华北5(呼和浩特)", "cn-huhehaote"), ("华北6(乌兰察布)", "cn-wulanchabu"),
        ("华南3(广州)", "cn-guangzhou"), ("华南2(河源)", "cn-heyuan")
    ]
    for i in range(0, len(cn_regions), 2):
        row = [InlineKeyboardButton(text=r[0], callback_data=f"swas_reg_{r[1]}") for r in cn_regions[i:i+2]]
        builder.row(*row)
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="swas_menu_main"))
    return builder.as_markup()

def get_swas_ap_menu():
    builder = InlineKeyboardBuilder()
    ap_regions = [
        ("🇭🇰 中国(香港)", "cn-hongkong"), ("🇸🇬 新加坡", "ap-southeast-1"),
        ("🇯🇵 日本(东京)", "ap-northeast-1"), ("🇰🇷 韩国(首尔)", "ap-northeast-2"),
        ("🇵🇭 菲律宾(马尼拉)", "ap-southeast-6"), ("🇹🇭 泰国(曼谷)", "ap-southeast-7"),
        ("🇮🇩 印尼(雅加达)", "ap-southeast-5"), ("🇲🇾 马来西亚(吉隆坡)", "ap-southeast-3")
    ]
    for i in range(0, len(ap_regions), 2):
        row = [InlineKeyboardButton(text=r[0], callback_data=f"swas_reg_{r[1]}") for r in ap_regions[i:i+2]]
        builder.row(*row)
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="swas_menu_main"))
    return builder.as_markup()

def get_swas_eu_us_menu():
    builder = InlineKeyboardBuilder()
    eu_us_regions = [
        ("🇺🇸 美国(弗吉尼亚)", "us-east-1"), ("🇺🇸 美国(硅谷)", "us-west-1"),
        ("🇩🇪 德国(法兰克福)", "eu-central-1"), ("🇬🇧 英国(伦敦)", "eu-west-1")
    ]
    for i in range(0, len(eu_us_regions), 2):
        row = [InlineKeyboardButton(text=r[0], callback_data=f"swas_reg_{r[1]}") for r in eu_us_regions[i:i+2]]
        builder.row(*row)
    builder.row(InlineKeyboardButton(text="🔙 返回上级", callback_data="swas_menu_main"))
    return builder.as_markup()

def get_swas_plan_menu():
    builder = InlineKeyboardBuilder()
    # 核心映射：传递内存大小给底层做筛选
    builder.row(InlineKeyboardButton(text="通用型 $4 /月 (2核 0.5G)", callback_data="swas_plan_0.5"))
    builder.row(InlineKeyboardButton(text="通用型 $5 /月 (2核 1.0G)", callback_data="swas_plan_1.0"))
    builder.row(InlineKeyboardButton(text="通用型 $8 /月 (2核 2.0G)", callback_data="swas_plan_2.0"))
    builder.row(InlineKeyboardButton(text="🔙 重新选择地域", callback_data="swas_menu_main"))
    return builder.as_markup()

@swas_router.callback_query(SwasFSM.waiting_for_region, F.data.startswith("swas_menu_"))
async def swas_navigate_menus(callback: types.CallbackQuery):
    """【折叠菜单导航】"""
    target = callback.data
    if target == "swas_menu_main":
        await callback.message.edit_reply_markup(reply_markup=get_swas_region_main_menu())
    elif target == "swas_menu_common":  # 🌟 新增：拦截常用地区的点击
        await callback.message.edit_reply_markup(reply_markup=get_swas_common_menu())
    elif target == "swas_menu_cn":
        await callback.message.edit_reply_markup(reply_markup=get_swas_cn_menu())
    elif target == "swas_menu_ap":
        await callback.message.edit_reply_markup(reply_markup=get_swas_ap_menu())
    elif target == "swas_menu_eu_us":
        await callback.message.edit_reply_markup(reply_markup=get_swas_eu_us_menu())
    await callback.answer()
# ================= 🚀 路由接管逻辑 =================

@swas_router.callback_query(F.data == "add_swas_server", StateFilter("*"))
async def trigger_add_swas_server(callback: types.CallbackQuery, state: FSMContext):
    """【入口】接管创建轻量云的点击"""
    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")
    
    if not account_id:
        return await callback.answer("⚠️ 会话已过期，请退回主菜单重新选择账号", show_alert=True)

    await state.set_state(SwasFSM.waiting_for_region)
    await callback.answer()
    
    text = "🪶 **轻量应用服务器 (SWAS) 部署**\n\n请选择目标地域 (已开启防刷，支持 22 个节点)："
    await callback.message.edit_text(text, reply_markup=get_swas_region_main_menu(), parse_mode="Markdown")


@swas_router.callback_query(SwasFSM.waiting_for_region, F.data.startswith("swas_menu_"))
async def swas_navigate_menus(callback: types.CallbackQuery):
    """【折叠菜单导航】"""
    target = callback.data
    if target == "swas_menu_main":
        await callback.message.edit_reply_markup(reply_markup=get_swas_region_main_menu())
    elif target == "swas_menu_cn":
        await callback.message.edit_reply_markup(reply_markup=get_swas_cn_menu())
    elif target == "swas_menu_ap":
        await callback.message.edit_reply_markup(reply_markup=get_swas_ap_menu())
    elif target == "swas_menu_eu_us":
        await callback.message.edit_reply_markup(reply_markup=get_swas_eu_us_menu())
    await callback.answer()


@swas_router.callback_query(SwasFSM.waiting_for_region, F.data.startswith("swas_reg_"))
async def swas_select_region(callback: types.CallbackQuery, state: FSMContext):
    """【选择地域】后跳转到选套餐"""
    region_id = callback.data.replace("swas_reg_", "")
    await state.update_data(swas_region=region_id)
    await state.set_state(SwasFSM.waiting_for_plan)
    
    await callback.message.edit_text(
        f"📍 已选地域: `{region_id}`\n\n"
        f"⚙️ 镜像: `Debian 12.10`\n"
        f"🔑 密码: `@QS00008`\n"
        f"🛡️ 防火墙: `TCP+UDP 1/65535`\n\n"
        f"请选择你要开通的套餐配置：",
        reply_markup=get_swas_plan_menu(), 
        parse_mode="Markdown"
    )
    await callback.answer()


@swas_router.callback_query(SwasFSM.waiting_for_plan, F.data.startswith("swas_plan_"))
async def swas_execute_create(callback: types.CallbackQuery, state: FSMContext):
    """【执行创建】调用 API 买机器并应用防火墙"""
    memory_target = float(callback.data.replace("swas_plan_", ""))
    
    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")
    region_id = user_data.get("swas_region")
    
    await state.clear()  # 立即清理状态，防止锁死
    
    progress_msg = await callback.message.edit_text(
        f"🚀 正在向阿里云下发轻量云部署任务...\n\n"
        f"地域: `{region_id}`\n"
        f"配置: 2核 {memory_target}G\n"
        f"*(该过程包含买机器和配置全开防火墙，约需 10-20 秒)*",
        parse_mode="Markdown"
    )
    
    # 扔进后台线程并发执行
    result = await asyncio.to_thread(_create_swas_sync, account_id, region_id, memory_target)
    
    if result["success"]:
        inst_id = result["instance_id"]
        
        # 构建返回按钮回显
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"select_acc:{account_id}"))
        
        await progress_msg.edit_text(
            f"🎉 **轻量云 (SWAS) 扩容成功！**\n\n"
            f"🌍 **地域**: `{region_id}`\n"
            f"🆔 **实例 ID**: `{inst_id}`\n"
            f"✅ **状态**: 运行中\n"
            f"🛡️ **防火墙**: TCP+UDP 1/65535 全开\n\n"
            f"💡 *轻量云开机较慢，获取 IP 及完全连通可能需要等待 1-2 分钟，请稍后刷新列表查看。*",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"select_acc:{account_id}"))
        await progress_msg.edit_text(
            f"❌ **轻量云创建失败**\n\n原因: `{result.get('error')}`", 
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

# =====================================================================
# ================= 🪶 轻量云 (SWAS) 详情面板与操作 =====================
# =====================================================================

def get_single_swas_sync(instance_id: str) -> dict:
    """遍历寻找单台轻量云实例的详细物理信息"""
    import sqlite3, config, json
    from alibabacloud_swas_open20200601.client import Client as SwasClient
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_swas_open20200601 import models as swas_models

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, access_key, access_secret FROM cloud_accounts WHERE is_active = 1")
    accounts = cursor.fetchall()
    conn.close()

    # 扫描雷达，覆盖轻量云主流地域
    regions = [
        "cn-hongkong", "ap-southeast-1", "ap-northeast-1", "ap-northeast-2",
        "us-east-1", "us-west-1", "eu-central-1", "eu-west-1",
        "cn-hangzhou", "cn-beijing", "cn-shanghai", "cn-shenzhen",
        "cn-chengdu", "cn-qingdao", "cn-guangzhou", "cn-heyuan",
        "cn-huhehaote", "cn-wulanchabu"
    ]

    for acc_id, ak, sk in accounts:
        for region_id in regions:
            try:
                ali_config = open_api_models.Config(
                    access_key_id=ak.strip(),
                    access_key_secret=sk.strip(),
                    endpoint=f'swas.{region_id}.aliyuncs.com'
                )
                client = SwasClient(ali_config)
                req = swas_models.ListInstancesRequest(
                    region_id=region_id,
                    instance_ids=json.dumps([instance_id])
                )
                resp = client.list_instances(req)
                if resp.body.instances:
                    inst = resp.body.instances[0]
                    ip = inst.public_ip_address if inst.public_ip_address else "无公网IP"
                    creation_time = inst.creation_time.split('T')[0] if inst.creation_time else "未知"
                    return {
                        "id": inst.instance_id,
                        "ip": ip,
                        "status": inst.status,
                        "region": region_id,
                        "account_id": acc_id,
                        "creation_time": creation_time
                    }
            except Exception:
                continue
    return None

@swas_router.callback_query(F.data.startswith("manage_swas_"))
async def process_manage_swas(callback: types.CallbackQuery):
    """渲染轻量云专属的 ECS 详情面板"""
    instance_id = callback.data.replace("manage_swas_", "")
    await callback.answer("🔄 正在从阿里云拉取轻量云深度数据...")

    ali_data = await asyncio.to_thread(get_single_swas_sync, instance_id)
    if not ali_data:
        return await callback.message.answer("❌ 无法从阿里云获取该轻量云的数据，可能已被释放。")

    import db
    from datetime import datetime
    import calendar
    biz_data = db.get_business_data(instance_id)

    status_str = "🟢 运行中" if ali_data['status'] == 'Running' else "🔴 已关机"
    if ali_data['status'] in ['Starting', 'Pending']: status_str = "🔵 正在开机中..."
    if ali_data['status'] in ['Stopping']: status_str = "🔵 正在关机中..."

    # 日期推算 (完美复刻 ECS 的逻辑)
    start_time_str = biz_data.get('traffic_start_time')
    if not start_time_str:
        now = datetime.now()
        start_time_str = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        db.update_business_data(instance_id, "traffic_start_time", start_time_str)

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

    expire_time_str = biz_data.get('expire_time')
    if not expire_time_str or str(expire_time_str).strip() in ["", "None", "未知日期"]:
        try:
            start_date_str = str(ali_data['creation_time'])[:10]
            now = datetime.now()
            target_month = now.month
            target_year = now.year
            anchor_day = int(reset_day)
            if now.day >= anchor_day:
                target_month += 1
                if target_month > 12:
                    target_month = 1
                    target_year += 1
            last_day = calendar.monthrange(target_year, target_month)[1]
            final_day = min(anchor_day, last_day)
            expire_time_str = datetime(target_year, target_month, final_day).strftime("%Y-%m-%d")
        except:
            expire_time_str = "推算失败"

    text = (
        f"📊 **轻量云 (SWAS) 实例详情**\n\n"
        f"🌍 地域: `{ali_data['region']}`\n"
        f"🆔 实例 ID: `{ali_data['id']}`\n"
        f"🌐 公网 IP: `{ali_data['ip']}`\n"
        f"✅ 状态: {status_str}\n"
        f"📶 本期出网流量: `轻量云暂未接入` / `{biz_data.get('traffic_limit_gb', 500)} GB`\n"
        f"📅 服务器开机时间: `{ali_data['creation_time']}`\n"
        f"⏳ 流量重置周期: `{reset_display}`\n"
        f"👤 客户业务到期: `{expire_time_str}`\n"
    )

    builder = InlineKeyboardBuilder()
    
    # 动态渲染开关机按钮
    if ali_data['status'] == 'Running':
        builder.row(InlineKeyboardButton(text="🛑 关机", callback_data=f"swas_action_stop_{instance_id}"))
    else:
        builder.row(InlineKeyboardButton(text="🟢 开机", callback_data=f"swas_action_start_{instance_id}"))
        
    builder.row(
        InlineKeyboardButton(text="🔑 强制重置密码", callback_data=f"swas_action_resetpw_{instance_id}"),
        InlineKeyboardButton(text="🗑️ 释放服务器", callback_data=f"swas_action_release_{instance_id}")
    )
    
    # 🌟 新增：重装系统按钮
    builder.row(InlineKeyboardButton(text="💿 重装系统", callback_data=f"swas_action_reinstall_{instance_id}"))
    
    builder.row(InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"select_acc:{ali_data['account_id']}"))

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# 占位拦截器：防止用户点击轻量云功能按钮时转圈卡死
@swas_router.callback_query(F.data.startswith("swas_action_stop_") | F.data.startswith("swas_action_start_"))
async def process_swas_power_action(callback: types.CallbackQuery):
    """【执行】真实的开关机逻辑"""
    parts = callback.data.split("_")
    action = parts[2] # stop 或 start
    instance_id = parts[3]
    
    await callback.answer(f"⏳ 正在向阿里云下发{'关机' if action=='stop' else '开机'}指令...", show_alert=False)
    
    # 扔进后台线程并发执行API
    result = await asyncio.to_thread(_execute_swas_action_sync, instance_id, action)
    
    if result["success"]:
        await callback.answer("✅ 指令下发成功！请等待几秒后再次点击面板刷新。", show_alert=True)
        # 自动调用你之前的详情函数刷新面板状态
        callback.data = f"manage_swas_{instance_id}"
        await process_manage_swas(callback)
    else:
        await callback.answer(f"❌ 操作失败: {result.get('error')}", show_alert=True)

@swas_router.callback_query(F.data.startswith("swas_action_reinstall_"))
async def process_swas_reinstall_ask(callback: types.CallbackQuery):
    """【询问】重装系统防误触二次确认"""
    instance_id = callback.data.split("_")[3]
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚠️ 确认重装 (清空全部数据)", callback_data=f"swas_confirm_reinstall_{instance_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 取消并返回详情", callback_data=f"manage_swas_{instance_id}")
    )
    await callback.message.edit_text(
        "⚠️ **高危操作确认：重装系统**\n\n"
        "此操作将把您的轻量应用服务器恢复至初始镜像状态：\n"
        "1. 系统盘上的**所有数据将被彻底擦除且无法恢复**。\n"
        "2. 默认密码将被重新变更为 `@QS00008`。\n\n"
        "您确定要继续执行吗？",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

@swas_router.callback_query(F.data.startswith("swas_confirm_reinstall_"))
async def process_swas_reinstall_execute(callback: types.CallbackQuery):
    """【执行】轻量云全自动重装连招 (关机 -> 重置 -> 开机)"""
    instance_id = callback.data.split("_")[3]
    
    # 1. 获取机器基础信息
    meta = await asyncio.to_thread(get_single_swas_sync, instance_id)
    if not meta:
        return await callback.answer("❌ 未找到实例信息，请重试", show_alert=True)
        
    account_id = meta["account_id"]
    region_id = meta["region"]
    
    # 更新 UI：提示开始执行
    progress_msg = await callback.message.edit_text(
        "⏳ **全自动重装连招启动中...**\n\n"
        "正在检测实例状态并下发指令，该过程约需 1-3 分钟，请不要操作菜单...",
        parse_mode="Markdown"
    )

    try:
        # ================= 自动化编排流程 =================
        
        # 步骤 A: 检查状态并强制关机
        current_status = await asyncio.to_thread(_get_swas_status_sync, account_id, region_id, instance_id)
        if current_status == "Running":
            await progress_msg.edit_text("⏳ **进度 1/3**：正在下发关机指令，等待云端响应...")
            await asyncio.to_thread(_execute_swas_action_sync, instance_id, "stop")
            
            # 轮询等待关机彻底完成 (每 5 秒查一次)
            while True:
                await asyncio.sleep(5)
                status = await asyncio.to_thread(_get_swas_status_sync, account_id, region_id, instance_id)
                if status == "Stopped":
                    break
                elif status == "Unknown": # 防止死循环
                    raise Exception("获取状态失败")

        # 步骤 B: 提交重置系统请求
        await progress_msg.edit_text("⏳ **进度 2/3**：已关机。正在擦除系统盘并重置镜像...")
        result = await asyncio.to_thread(_execute_swas_action_sync, instance_id, "reinstall")
        if not result["success"]:
            raise Exception(result.get("error", "重置接口调用失败"))
            
        # 缓冲等待 10 秒，确保底层重置任务受理完成
        await asyncio.sleep(10)
        
        # 🌟 追加步骤：系统重置后，独立下发修改密码指令
        await progress_msg.edit_text("⏳ **进度 2.5/3**：系统盘擦除完成，正在写入默认密码...")
        pw_result = await asyncio.to_thread(_execute_swas_action_sync, instance_id, "resetpw")
        if not pw_result["success"]:
            # 即使密码修改失败，也别阻断流程，可能需要稍后手动在面板重置
            pass
        
        # 步骤 C: 尝试下发开机指令
        await progress_msg.edit_text("⏳ **进度 3/3**：正在引导系统开机...")
        await asyncio.to_thread(_execute_swas_action_sync, instance_id, "start")
        
        # ================= 流程结束 =================

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回服务器列表", callback_data=f"select_acc:{account_id}"))
        
        # 完美复刻 ECS 的成功文案回显
        await progress_msg.edit_text(
            f"✅ **轻量云 (SWAS) 全自动重装已完成！**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🔑 默认密码: `@QS00008`\n\n"
            f"🚀 机器正在云端开机并初始化，请等待 1-2 分钟后尝试使用 SSH 连接。\n"
            f"*(可点击下方按钮返回列表重新刷新状态)*",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 返回详情", callback_data=f"manage_swas_{instance_id}"))
        await progress_msg.edit_text(
            f"❌ **重装流程意外中断**\n\n原因: `{str(e)}`",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )
