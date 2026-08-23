import asyncio
import calendar
import json
import sqlite3
import time
import uuid

import config

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from aiogram.filters import StateFilter

from alibabacloud_swas_open20200601.client import Client as SwasClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_swas_open20200601 import models as swas_models


swas_router = Router()

# 仅 ADMIN_ID 可以操作轻量云
swas_router.message.filter(F.from_user.id == int(config.ADMIN_ID))
swas_router.callback_query.filter(F.from_user.id == int(config.ADMIN_ID))


# =========================================================
# 全局配置
# =========================================================

DEFAULT_PASSWORD = "@QS00008"

# 电源状态轮询
POWER_POLL_INTERVAL = 5
POWER_TIMEOUT = 180

# 重装后等待 API 可以继续接受操作的最长时间
REINSTALL_TIMEOUT = 900
REINSTALL_RETRY_INTERVAL = 8

# 轻量云常用地域
SWAS_REGIONS = [
    "cn-hongkong",
    "ap-southeast-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "us-east-1",
    "us-west-1",
    "eu-central-1",
    "eu-west-1",
    "cn-hangzhou",
    "cn-beijing",
    "cn-shanghai",
    "cn-shenzhen",
    "cn-chengdu",
    "cn-qingdao",
    "cn-guangzhou",
    "cn-heyuan",
    "cn-huhehaote",
    "cn-wulanchabu",
    "ap-southeast-6",
    "ap-southeast-7",
    "ap-southeast-5",
    "ap-southeast-3",
]


class SwasFSM(StatesGroup):
    waiting_for_region = State()
    waiting_for_plan = State()


# =========================================================
# 通用工具
# =========================================================

def _safe_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text[-1500:]


def _is_retryable_reinstall_error(error_text: str) -> bool:
    text = (error_text or "").lower()
    keywords = [
        "incorrectinstancestatus",
        "incorrect instance status",
        "invalidinstance.status",
        "operationconflict",
        "operation conflict",
        "instance is busy",
        "being reset",
        "resetting",
        "task is running",
        "requestid",
        "servicebusy",
        "system operation",
        "正在重置",
        "操作进行中",
    ]
    return any(k in text for k in keywords)


# =========================================================
# 阿里云 Client
# =========================================================

def get_swas_client(account_id: int, region_id: str) -> SwasClient:
    """根据数据库中的云账号创建 SWAS client。"""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT access_key, access_secret FROM cloud_accounts WHERE id = ?",
            (account_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise ValueError(f"未找到账号 ID {account_id} 的密钥信息")

    access_key, access_secret = row

    ali_config = open_api_models.Config(
        access_key_id=str(access_key).strip(),
        access_key_secret=str(access_secret).strip(),
        endpoint=f"swas.{region_id}.aliyuncs.com",
    )
    return SwasClient(ali_config)


# =========================================================
# 查询实例状态
# =========================================================

def _get_swas_status_sync(account_id: int, region_id: str, instance_id: str) -> str:
    """获取实例实时状态。失败时抛异常，避免 Unknown 被误判成真实状态。"""
    client = get_swas_client(account_id, region_id)
    req = swas_models.ListInstancesRequest(
        region_id=region_id,
        instance_ids=json.dumps([instance_id]),
    )
    resp = client.list_instances(req)

    instances = getattr(resp.body, "instances", None) or []
    if not instances:
        raise RuntimeError(f"阿里云未找到实例 {instance_id}")

    return instances[0].status


async def wait_swas_status(
    account_id: int,
    region_id: str,
    instance_id: str,
    target_statuses,
    timeout: int = POWER_TIMEOUT,
):
    """等待实例进入目标状态。"""
    if isinstance(target_statuses, str):
        target_statuses = {target_statuses}
    else:
        target_statuses = set(target_statuses)

    started = time.monotonic()
    last_status = None

    while True:
        status = await asyncio.to_thread(
            _get_swas_status_sync,
            account_id,
            region_id,
            instance_id,
        )
        last_status = status

        if status in target_statuses:
            return status

        if time.monotonic() - started >= timeout:
            raise TimeoutError(
                f"等待实例状态超时，当前状态: {last_status}"
            )

        await asyncio.sleep(POWER_POLL_INTERVAL)


# =========================================================
# 获取实例完整信息
# =========================================================

def get_single_swas_sync(instance_id: str) -> dict | None:
    """扫描启用账号和已知地域，找到单台 SWAS 实例。"""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, access_key, access_secret "
            "FROM cloud_accounts "
            "WHERE is_active = 1"
        )
        accounts = cursor.fetchall()
    finally:
        conn.close()

    for account_id, access_key, access_secret in accounts:
        for region_id in SWAS_REGIONS:
            try:
                ali_config = open_api_models.Config(
                    access_key_id=str(access_key).strip(),
                    access_key_secret=str(access_secret).strip(),
                    endpoint=f"swas.{region_id}.aliyuncs.com",
                )
                client = SwasClient(ali_config)

                req = swas_models.ListInstancesRequest(
                    region_id=region_id,
                    instance_ids=json.dumps([instance_id]),
                )
                resp = client.list_instances(req)
                instances = getattr(resp.body, "instances", None) or []
                if not instances:
                    continue

                inst = instances[0]
                ip = getattr(inst, "public_ip_address", None) or "无公网IP"
                creation_time = getattr(inst, "creation_time", None)
                creation_date = (
                    creation_time.split("T")[0]
                    if creation_time
                    else "未知"
                )

                return {
                    "id": inst.instance_id,
                    "ip": ip,
                    "status": inst.status,
                    "region": region_id,
                    "account_id": account_id,
                    "creation_time": creation_date,
                }

            except Exception:
                continue

    return None


# =========================================================
# 底层 SWAS 动作
# =========================================================

def _execute_swas_action_sync(
    instance_id: str,
    action: str,
    password: str | None = None,
    image_id: str | None = None,
) -> dict:
    """
    真实调用阿里云 API。

    注意：
    - stop/start 只负责下发指令；上层必须等待最终状态。
    - ResetSystem 只负责重装系统。
    - 修改密码使用 UpdateInstanceAttribute。
    - 密码修改后必须 RebootInstance 才能生效。
    """
    meta = get_single_swas_sync(instance_id)
    if not meta:
        return {
            "success": False,
            "error": "未在阿里云匹配到该实例信息",
        }

    region_id = meta["region"]

    try:
        client = get_swas_client(meta["account_id"], region_id)
        client_token = str(uuid.uuid4())

        if action == "stop":
            req = swas_models.StopInstancesRequest(
                region_id=region_id,
                instance_ids=json.dumps([instance_id]),
                client_token=client_token,
            )
            resp = client.stop_instances(req)

        elif action == "start":
            req = swas_models.StartInstancesRequest(
                region_id=region_id,
                instance_ids=json.dumps([instance_id]),
                client_token=client_token,
            )
            resp = client.start_instances(req)

        elif action == "reboot":
            req = swas_models.RebootInstanceRequest(
                region_id=region_id,
                instance_id=instance_id,
                client_token=client_token,
            )
            resp = client.reboot_instance(req)

        elif action == "resetpw":
            new_password = password or DEFAULT_PASSWORD
            req = swas_models.UpdateInstanceAttributeRequest(
                region_id=region_id,
                instance_id=instance_id,
                password=new_password,
                client_token=client_token,
            )
            resp = client.update_instance_attribute(req)

        elif action == "reinstall":
            kwargs = {
                "region_id": region_id,
                "instance_id": instance_id,
                "client_token": client_token,
            }
            if image_id:
                kwargs["image_id"] = image_id

            # 不把 password 放到 ResetSystemRequest 顶层。
            # 为了兼容不同 SDK 版本，这里统一采用：
            # ResetSystem -> 等待可操作 -> UpdateInstanceAttribute。
            req = swas_models.ResetSystemRequest(**kwargs)
            resp = client.reset_system(req)

        else:
            return {
                "success": False,
                "error": f"未知操作: {action}",
            }

        return {
            "success": True,
            "account_id": meta["account_id"],
            "region": region_id,
            "response": resp,
        }

    except Exception as exc:
        return {
            "success": False,
            "error": _format_error(exc),
            "account_id": meta["account_id"],
            "region": region_id,
        }


# =========================================================
# 镜像 / 套餐
# =========================================================

def _get_debian_image_id(client: SwasClient, region_id: str) -> str:
    """自动寻找 Debian 12.10 系统镜像。"""
    req = swas_models.ListImagesRequest(
        region_id=region_id,
        image_type="system",
    )
    resp = client.list_images(req)
    images = getattr(resp.body, "images", None) or []

    # 优先精确匹配 Debian 12.10
    for img in images:
        name = str(getattr(img, "image_name", ""))
        if "Debian" in name and "12.10" in name:
            return img.image_id

    # 如果当前地域镜像命名略有变化，则退化到 Debian 12
    for img in images:
        name = str(getattr(img, "image_name", ""))
        if "Debian" in name and "12" in name:
            return img.image_id

    raise RuntimeError("当前地域未找到 Debian 12 系统镜像")


def _get_plan_id(client: SwasClient, region_id: str, target_memory: float) -> str:
    """匹配 2 核 + 指定内存的套餐。"""
    req = swas_models.ListPlansRequest(region_id=region_id)
    resp = client.list_plans(req)
    plans = getattr(resp.body, "plans", None) or []

    for plan in plans:
        core = getattr(plan, "core", None)
        memory = getattr(plan, "memory", None)
        if core == 2 and _safe_float(memory) == float(target_memory):
            return plan.plan_id

    raise RuntimeError(
        f"当前地域未找到 2核 {target_memory}G 的套餐配置"
    )


# =========================================================
# 创建服务器
# =========================================================

def _wait_instance_visible_sync(
    account_id: int,
    region_id: str,
    instance_id: str,
    timeout: int = 180,
):
    """创建实例后等待 ListInstances 能查到。"""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            client = get_swas_client(account_id, region_id)
            req = swas_models.ListInstancesRequest(
                region_id=region_id,
                instance_ids=json.dumps([instance_id]),
            )
            resp = client.list_instances(req)
            instances = getattr(resp.body, "instances", None) or []
            if instances:
                return instances[0]
        except Exception:
            pass
        time.sleep(5)

    raise TimeoutError("创建成功后等待实例上线超时")


def _create_firewall_sync(
    client: SwasClient,
    region_id: str,
    instance_id: str,
):
    """创建 TCP+UDP 1/65535 全开规则。"""
    req = swas_models.CreateFirewallRuleRequest(
        region_id=region_id,
        instance_id=instance_id,
        rule_protocol="TCP+UDP",
        port="1/65535",
        remark="全开",
    )
    return client.create_firewall_rule(req)


def _set_password_and_reboot_sync(
    instance_id: str,
    password: str,
):
    """同步：修改密码，然后重启。"""
    result = _execute_swas_action_sync(
        instance_id,
        "resetpw",
        password=password,
    )
    if not result["success"]:
        return result

    reboot_result = _execute_swas_action_sync(
        instance_id,
        "reboot",
    )
    if not reboot_result["success"]:
        return reboot_result

    return result


def _create_swas_sync(
    account_id: int,
    region_id: str,
    memory: float,
) -> dict:
    """同步执行：创建实例 -> 等待可见 -> 开防火墙 -> 设置密码 -> 开机确认。"""
    try:
        client = get_swas_client(account_id, region_id)

        # 1. 镜像 / 套餐
        image_id = _get_debian_image_id(client, region_id)
        plan_id = _get_plan_id(client, region_id, memory)

        # 2. 创建实例
        create_req = swas_models.CreateInstancesRequest(
            region_id=region_id,
            image_id=image_id,
            plan_id=plan_id,
            period=1,
            charge_type="PrePaid",
            client_token=str(uuid.uuid4()),
        )
        create_resp = client.create_instances(create_req)
        instance_ids = getattr(create_resp.body, "instance_ids", None) or []

        if not instance_ids:
            return {
                "success": False,
                "error": "创建接口已响应，但未返回实例 ID",
            }

        instance_id = instance_ids[0]

        # 3. 等待实例注册到 ListInstances
        _wait_instance_visible_sync(
            account_id,
            region_id,
            instance_id,
            timeout=180,
        )

        # 4. 防火墙
        try:
            _create_firewall_sync(
                client,
                region_id,
                instance_id,
            )
        except Exception as firewall_exc:
            # 防火墙失败不回滚服务器创建，但把错误返回给上层。
            return {
                "success": False,
                "instance_id": instance_id,
                "error": f"服务器已创建，但防火墙配置失败：{_format_error(firewall_exc)}",
            }

        # 5. 修改密码
        password_result = _execute_swas_action_sync(
            instance_id,
            "resetpw",
            password=DEFAULT_PASSWORD,
        )
        if not password_result["success"]:
            return {
                "success": False,
                "instance_id": instance_id,
                "error": f"服务器已创建，但密码设置失败：{password_result['error']}",
            }

        # 6. 修改密码后重启
        reboot_result = _execute_swas_action_sync(
            instance_id,
            "reboot",
        )
        if not reboot_result["success"]:
            return {
                "success": False,
                "instance_id": instance_id,
                "error": f"密码已设置，但重启失败：{reboot_result['error']}",
            }

        # 7. 等待 Running
        # 注意：这里是同步线程，所以直接轮询。
        started = time.monotonic()
        while time.monotonic() - started < POWER_TIMEOUT:
            try:
                status = _get_swas_status_sync(
                    account_id,
                    region_id,
                    instance_id,
                )
                if status == "Running":
                    break
            except Exception:
                pass
            time.sleep(POWER_POLL_INTERVAL)

        return {
            "success": True,
            "instance_id": instance_id,
            "region": region_id,
            "account_id": account_id,
            "password": DEFAULT_PASSWORD,
            "image_id": image_id,
            "plan_id": plan_id,
        }

    except Exception as exc:
        error_msg = _format_error(exc)
        if (
            "Inventory" in error_msg
            or "stock" in error_msg.lower()
            or "售罄" in error_msg
        ):
            error_msg = "您选择的配置在当前地域已售罄，请尝试其他地域或配置。"

        return {
            "success": False,
            "error": error_msg,
        }


# =========================================================
# 操作重试
# =========================================================

async def execute_with_retry(
    instance_id: str,
    action: str,
    password: str | None = None,
    timeout: int = REINSTALL_TIMEOUT,
):
    """
    用于 ResetSystem 后的过渡阶段。
    阿里云可能暂时拒绝下一步操作，所以这里采用真正的重试而不是固定 sleep。
    """
    started = time.monotonic()
    last_error = "未知错误"

    while time.monotonic() - started < timeout:
        result = await asyncio.to_thread(
            _execute_swas_action_sync,
            instance_id,
            action,
            password,
        )

        if result["success"]:
            return result

        last_error = result.get("error", "未知错误")

        if not _is_retryable_reinstall_error(last_error):
            return result

        await asyncio.sleep(REINSTALL_RETRY_INTERVAL)

    return {
        "success": False,
        "error": f"操作超时，最后错误：{last_error}",
    }


# =========================================================
# 地区菜单
# =========================================================

def get_swas_region_main_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⭐ 常用地区", callback_data="swas_menu_common"),
        InlineKeyboardButton(text="🇨🇳 中国大陆", callback_data="swas_menu_cn"),
    )
    builder.row(
        InlineKeyboardButton(text="🌏 亚太地区", callback_data="swas_menu_ap"),
        InlineKeyboardButton(text="🌍 欧美地区", callback_data="swas_menu_eu_us"),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 取消并返回",
            callback_data="cancel_add_server",
        )
    )
    return builder.as_markup()


def get_swas_common_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🇭🇰 中国(香港)",
            callback_data="swas_reg_cn-hongkong",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🇸🇬 新加坡",
            callback_data="swas_reg_ap-southeast-1",
        ),
        InlineKeyboardButton(
            text="🇺🇸 美国(弗吉尼亚)",
            callback_data="swas_reg_us-east-1",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 返回上级",
            callback_data="swas_menu_main",
        )
    )
    return builder.as_markup()


def get_swas_cn_menu():
    builder = InlineKeyboardBuilder()
    regions = [
        ("华东1(杭州)", "cn-hangzhou"),
        ("华北2(北京)", "cn-beijing"),
        ("华东2(上海)", "cn-shanghai"),
        ("华南1(深圳)", "cn-shenzhen"),
        ("西南1(成都)", "cn-chengdu"),
        ("华北1(青岛)", "cn-qingdao"),
        ("华北5(呼和浩特)", "cn-huhehaote"),
        ("华北6(乌兰察布)", "cn-wulanchabu"),
        ("华南3(广州)", "cn-guangzhou"),
        ("华南2(河源)", "cn-heyuan"),
    ]
    for i in range(0, len(regions), 2):
        builder.row(
            *[
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"swas_reg_{region}",
                )
                for name, region in regions[i:i + 2]
            ]
        )
    builder.row(
        InlineKeyboardButton(
            text="🔙 返回上级",
            callback_data="swas_menu_main",
        )
    )
    return builder.as_markup()


def get_swas_ap_menu():
    builder = InlineKeyboardBuilder()
    regions = [
        ("🇭🇰 中国(香港)", "cn-hongkong"),
        ("🇸🇬 新加坡", "ap-southeast-1"),
        ("🇯🇵 日本(东京)", "ap-northeast-1"),
        ("🇰🇷 韩国(首尔)", "ap-northeast-2"),
        ("🇵🇭 菲律宾(马尼拉)", "ap-southeast-6"),
        ("🇹🇭 泰国(曼谷)", "ap-southeast-7"),
        ("🇮🇩 印尼(雅加达)", "ap-southeast-5"),
        ("🇲🇾 马来西亚(吉隆坡)", "ap-southeast-3"),
    ]
    for i in range(0, len(regions), 2):
        builder.row(
            *[
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"swas_reg_{region}",
                )
                for name, region in regions[i:i + 2]
            ]
        )
    builder.row(
        InlineKeyboardButton(
            text="🔙 返回上级",
            callback_data="swas_menu_main",
        )
    )
    return builder.as_markup()


def get_swas_eu_us_menu():
    builder = InlineKeyboardBuilder()
    regions = [
        ("🇺🇸 美国(弗吉尼亚)", "us-east-1"),
        ("🇺🇸 美国(硅谷)", "us-west-1"),
        ("🇩🇪 德国(法兰克福)", "eu-central-1"),
        ("🇬🇧 英国(伦敦)", "eu-west-1"),
    ]
    for i in range(0, len(regions), 2):
        builder.row(
            *[
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"swas_reg_{region}",
                )
                for name, region in regions[i:i + 2]
            ]
        )
    builder.row(
        InlineKeyboardButton(
            text="🔙 返回上级",
            callback_data="swas_menu_main",
        )
    )
    return builder.as_markup()


def get_swas_plan_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="通用型 $4 /月 (2核 0.5G)",
            callback_data="swas_plan_0.5",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="通用型 $5 /月 (2核 1.0G)",
            callback_data="swas_plan_1.0",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="通用型 $8 /月 (2核 2.0G)",
            callback_data="swas_plan_2.0",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 重新选择地域",
            callback_data="swas_menu_main",
        )
    )
    return builder.as_markup()


# =========================================================
# 创建流程
# =========================================================

@swas_router.callback_query(
    F.data == "add_swas_server",
    StateFilter("*"),
)
async def trigger_add_swas_server(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")

    if not account_id:
        return await callback.answer(
            "⚠️ 会话已过期，请退回主菜单重新选择账号",
            show_alert=True,
        )

    await state.set_state(SwasFSM.waiting_for_region)
    await callback.answer()

    await callback.message.edit_text(
        "🪶 **轻量应用服务器 (SWAS) 部署**\n\n"
        "请选择目标地域：",
        reply_markup=get_swas_region_main_menu(),
        parse_mode="Markdown",
    )


@swas_router.callback_query(
    SwasFSM.waiting_for_region,
    F.data.startswith("swas_menu_"),
)
async def swas_navigate_menus(callback: types.CallbackQuery):
    target = callback.data

    if target == "swas_menu_main":
        markup = get_swas_region_main_menu()
    elif target == "swas_menu_common":
        markup = get_swas_common_menu()
    elif target == "swas_menu_cn":
        markup = get_swas_cn_menu()
    elif target == "swas_menu_ap":
        markup = get_swas_ap_menu()
    elif target == "swas_menu_eu_us":
        markup = get_swas_eu_us_menu()
    else:
        markup = get_swas_region_main_menu()

    await callback.message.edit_reply_markup(reply_markup=markup)
    await callback.answer()


@swas_router.callback_query(
    SwasFSM.waiting_for_region,
    F.data.startswith("swas_reg_"),
)
async def swas_select_region(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    region_id = callback.data.replace("swas_reg_", "", 1)
    await state.update_data(swas_region=region_id)
    await state.set_state(SwasFSM.waiting_for_plan)

    await callback.message.edit_text(
        f"📍 已选地域: `{region_id}`\n\n"
        "⚙️ 镜像: `Debian 12`\n"
        f"🔑 密码: `{DEFAULT_PASSWORD}`\n"
        "🛡️ 防火墙: `TCP+UDP 1/65535`\n\n"
        "请选择你要开通的套餐配置：",
        reply_markup=get_swas_plan_menu(),
        parse_mode="Markdown",
    )
    await callback.answer()


@swas_router.callback_query(
    SwasFSM.waiting_for_plan,
    F.data.startswith("swas_plan_"),
)
async def swas_execute_create(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    memory_target = float(callback.data.replace("swas_plan_", "", 1))

    user_data = await state.get_data()
    account_id = user_data.get("current_account_id")
    region_id = user_data.get("swas_region")

    if not account_id or not region_id:
        await state.clear()
        return await callback.answer(
            "❌ 创建会话已失效，请重新进入",
            show_alert=True,
        )

    await state.clear()

    progress_msg = await callback.message.edit_text(
        "🚀 **正在向阿里云创建轻量云实例...**\n\n"
        f"🌍 地域: `{region_id}`\n"
        f"⚙️ 配置: `2核 {memory_target}G`\n\n"
        "正在创建实例、配置防火墙、设置密码并等待 Running。",
        parse_mode="Markdown",
    )

    result = await asyncio.to_thread(
        _create_swas_sync,
        account_id,
        region_id,
        memory_target,
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔙 返回服务器列表",
            callback_data=f"select_acc:{account_id}",
        )
    )

    if result["success"]:
        await progress_msg.edit_text(
            "🎉 **轻量云创建成功！**\n\n"
            f"🌍 地域: `{region_id}`\n"
            f"🆔 实例 ID: `{result['instance_id']}`\n"
            "✅ 状态: `Running`\n"
            "🛡️ 防火墙: `TCP+UDP 1/65535`\n"
            f"🔑 密码: `{DEFAULT_PASSWORD}`\n\n"
            "现在可以正常 SSH 登录。",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )
    else:
        await progress_msg.edit_text(
            "❌ **轻量云创建失败**\n\n"
            f"原因: `{result.get('error', '未知错误')}`",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )


# =========================================================
# 详情页面
# =========================================================

@swas_router.callback_query(F.data.startswith("manage_swas_"))
async def process_manage_swas(callback: types.CallbackQuery):
    instance_id = callback.data.replace("manage_swas_", "", 1)

    await callback.answer("🔄 正在从阿里云读取实时状态...")

    ali_data = await asyncio.to_thread(
        get_single_swas_sync,
        instance_id,
    )

    if not ali_data:
        return await callback.message.answer(
            "❌ 无法从阿里云获取该轻量云的数据，可能已被释放。"
        )

    import db
    from datetime import datetime

    biz_data = db.get_business_data(instance_id) or {}

    status = ali_data["status"]
    if status == "Running":
        status_str = "🟢 运行中"
    elif status == "Stopped":
        status_str = "🔴 已关机"
    elif status in {"Starting", "Pending"}:
        status_str = "🔵 正在开机中..."
    elif status == "Stopping":
        status_str = "🔵 正在关机中..."
    else:
        status_str = f"⚪ {status}"

    # 流量起始时间
    start_time_str = biz_data.get("traffic_start_time")
    if not start_time_str:
        start_time = datetime.now().replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        start_time_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            db.update_business_data(
                instance_id,
                "traffic_start_time",
                start_time_str,
            )
        except Exception:
            pass

    try:
        creation_day = int(str(ali_data["creation_time"]).split("-")[2])
    except Exception:
        creation_day = 1

    reset_day = biz_data.get("reset_day")
    if not reset_day:
        reset_day = creation_day

    try:
        reset_day_int = int(reset_day)
    except Exception:
        reset_day_int = creation_day

    if reset_day_int == 1:
        reset_display = "自然月 (每月 1 号重置)"
    elif reset_day_int == creation_day:
        reset_display = f"跟随开机日 (每月 {reset_day_int} 号重置)"
    else:
        reset_display = f"自定义 (每月 {reset_day_int} 号重置)"

    expire_time_str = biz_data.get("expire_time")
    if not expire_time_str or str(expire_time_str).strip() in {
        "",
        "None",
        "未知日期",
    }:
        try:
            now = datetime.now()
            target_month = now.month
            target_year = now.year
            anchor_day = reset_day_int

            if now.day >= anchor_day:
                target_month += 1
                if target_month > 12:
                    target_month = 1
                    target_year += 1

            last_day = calendar.monthrange(target_year, target_month)[1]
            final_day = min(anchor_day, last_day)
            expire_time_str = datetime(
                target_year,
                target_month,
                final_day,
            ).strftime("%Y-%m-%d")
        except Exception:
            expire_time_str = "推算失败"

    text = (
        "📊 **轻量云 (SWAS) 实例详情**\n\n"
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

    # 动态开关机按钮
    if status == "Running":
        builder.row(
            InlineKeyboardButton(
                text="🛑 关机",
                callback_data=f"swas_action_stop_{instance_id}",
            )
        )
    elif status == "Stopped":
        builder.row(
            InlineKeyboardButton(
                text="🟢 开机",
                callback_data=f"swas_action_start_{instance_id}",
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text="🔄 刷新状态",
                callback_data=f"manage_swas_{instance_id}",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔑 强制重置密码",
            callback_data=f"swas_action_resetpw_{instance_id}",
        ),
        InlineKeyboardButton(
            text="🗑️ 释放服务器",
            callback_data=f"swas_action_release_{instance_id}",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="💿 重装系统",
            callback_data=f"swas_action_reinstall_{instance_id}",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔄 刷新",
            callback_data=f"manage_swas_{instance_id}",
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 返回服务器列表",
            callback_data=f"select_acc:{ali_data['account_id']}",
        )
    )

    await callback.message.edit_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown",
    )


# =========================================================
# 关机 / 开机
# =========================================================

@swas_router.callback_query(
    F.data.startswith("swas_action_stop_")
    | F.data.startswith("swas_action_start_")
)
async def process_swas_power_action(callback: types.CallbackQuery):
    parts = callback.data.split("_", 3)
    if len(parts) < 4:
        return await callback.answer("❌ 无效操作参数", show_alert=True)

    action = parts[2]
    instance_id = parts[3]
    action_name = "关机" if action == "stop" else "开机"

    await callback.answer(
        f"⏳ 正在执行{action_name}...",
        show_alert=False,
    )

    meta = await asyncio.to_thread(
        get_single_swas_sync,
        instance_id,
    )
    if not meta:
        return await callback.answer(
            "❌ 找不到服务器",
            show_alert=True,
        )

    account_id = meta["account_id"]
    region_id = meta["region"]

    try:
        current_status = await asyncio.to_thread(
            _get_swas_status_sync,
            account_id,
            region_id,
            instance_id,
        )

        if action == "stop":
            if current_status == "Stopped":
                await callback.answer("服务器已经是关机状态", show_alert=True)
            else:
                result = await asyncio.to_thread(
                    _execute_swas_action_sync,
                    instance_id,
                    "stop",
                )
                if not result["success"]:
                    raise RuntimeError(result["error"])

                await wait_swas_status(
                    account_id,
                    region_id,
                    instance_id,
                    "Stopped",
                    POWER_TIMEOUT,
                )

                await callback.answer(
                    "✅ 服务器已确认关机",
                    show_alert=True,
                )

        elif action == "start":
            if current_status == "Running":
                await callback.answer("服务器已经是运行状态", show_alert=True)
            else:
                result = await asyncio.to_thread(
                    _execute_swas_action_sync,
                    instance_id,
                    "start",
                )
                if not result["success"]:
                    raise RuntimeError(result["error"])

                await wait_swas_status(
                    account_id,
                    region_id,
                    instance_id,
                    "Running",
                    POWER_TIMEOUT,
                )

                await callback.answer(
                    "✅ 服务器已确认开机",
                    show_alert=True,
                )

        else:
            return await callback.answer("❌ 不支持的操作", show_alert=True)

        callback.data = f"manage_swas_{instance_id}"
        await process_manage_swas(callback)

    except Exception as exc:
        await callback.answer(
            f"❌ {action_name}失败\n\n{_format_error(exc)}",
            show_alert=True,
        )


# =========================================================
# 重置密码
# =========================================================

@swas_router.callback_query(
    F.data.startswith("swas_action_resetpw_")
)
async def process_swas_reset_password(callback: types.CallbackQuery):
    instance_id = callback.data.replace("swas_action_resetpw_", "", 1)

    await callback.answer("⏳ 正在修改密码并重启服务器...", show_alert=False)

    meta = await asyncio.to_thread(
        get_single_swas_sync,
        instance_id,
    )
    if not meta:
        return await callback.answer("❌ 找不到服务器", show_alert=True)

    account_id = meta["account_id"]
    region_id = meta["region"]

    try:
        # 1. 修改密码
        result = await asyncio.to_thread(
            _execute_swas_action_sync,
            instance_id,
            "resetpw",
            DEFAULT_PASSWORD,
        )
        if not result["success"]:
            raise RuntimeError(result["error"])

        # 2. 官方要求：密码修改后重启
        reboot_result = await asyncio.to_thread(
            _execute_swas_action_sync,
            instance_id,
            "reboot",
        )
        if not reboot_result["success"]:
            raise RuntimeError(reboot_result["error"])

        # 3. 确认最终 Running
        await wait_swas_status(
            account_id,
            region_id,
            instance_id,
            "Running",
            POWER_TIMEOUT,
        )

        await callback.answer(
            f"✅ 密码修改成功\n\n新密码：{DEFAULT_PASSWORD}",
            show_alert=True,
        )

        callback.data = f"manage_swas_{instance_id}"
        await process_manage_swas(callback)

    except Exception as exc:
        await callback.answer(
            f"❌ 密码修改失败\n\n{_format_error(exc)}",
            show_alert=True,
        )


# =========================================================
# 重装系统：确认
# =========================================================

@swas_router.callback_query(
    F.data.startswith("swas_action_reinstall_")
)
async def process_swas_reinstall_ask(callback: types.CallbackQuery):
    instance_id = callback.data.replace("swas_action_reinstall_", "", 1)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚠️ 确认重装 (清空系统盘)",
            callback_data=f"swas_confirm_reinstall_{instance_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 取消并返回详情",
            callback_data=f"manage_swas_{instance_id}",
        )
    )

    await callback.message.edit_text(
        "⚠️ **高危操作确认：重装系统**\n\n"
        "此操作将恢复系统盘至镜像状态：\n"
        "1. 系统盘上的数据会被清除。\n"
        "2. 重装完成后会重新设置登录密码。\n"
        f"3. 新密码：`{DEFAULT_PASSWORD}`\n\n"
        "确定继续执行吗？",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown",
    )
    await callback.answer()


# =========================================================
# 重装系统：执行
# =========================================================

@swas_router.callback_query(
    F.data.startswith("swas_confirm_reinstall_")
)
async def process_swas_reinstall_execute(callback: types.CallbackQuery):
    instance_id = callback.data.replace("swas_confirm_reinstall_", "", 1)

    meta = await asyncio.to_thread(
        get_single_swas_sync,
        instance_id,
    )
    if not meta:
        return await callback.answer(
            "❌ 未找到实例信息，请重试",
            show_alert=True,
        )

    account_id = meta["account_id"]
    region_id = meta["region"]

    progress_msg = await callback.message.edit_text(
        "⏳ **轻量云重装流程启动**\n\n"
        "1️⃣ 检查实例状态\n"
        "2️⃣ 确保服务器关机\n"
        "3️⃣ ResetSystem 重装系统\n"
        "4️⃣ 设置新的登录密码\n"
        "5️⃣ 开机并确认 Running\n\n"
        "整个过程由程序轮询阿里云状态，不再固定等待 10 秒。",
        parse_mode="Markdown",
    )

    try:
        # -------------------------------------------------
        # 1. 检查并确保关机
        # -------------------------------------------------
        current_status = await asyncio.to_thread(
            _get_swas_status_sync,
            account_id,
            region_id,
            instance_id,
        )

        if current_status != "Stopped":
            await progress_msg.edit_text(
                "⏳ **进度 1/5**\n\n"
                "正在关机并等待阿里云确认状态...",
                parse_mode="Markdown",
            )

            result = await asyncio.to_thread(
                _execute_swas_action_sync,
                instance_id,
                "stop",
            )
            if not result["success"]:
                raise RuntimeError(result["error"])

            await wait_swas_status(
                account_id,
                region_id,
                instance_id,
                "Stopped",
                POWER_TIMEOUT,
            )

        # -------------------------------------------------
        # 2. ResetSystem
        # -------------------------------------------------
        await progress_msg.edit_text(
            "⏳ **进度 2/5**\n\n"
            "服务器已确认关机。\n"
            "正在调用阿里云 ResetSystem 重装系统盘...",
            parse_mode="Markdown",
        )

        reset_result = await asyncio.to_thread(
            _execute_swas_action_sync,
            instance_id,
            "reinstall",
        )
        if not reset_result["success"]:
            raise RuntimeError(reset_result["error"])

        # -------------------------------------------------
        # 3. ResetSystem 后，不固定 sleep 10 秒
        #    而是不断尝试 UpdateInstanceAttribute。
        #    只有 API 真正接受密码修改，才进入下一步。
        # -------------------------------------------------
        await progress_msg.edit_text(
            "⏳ **进度 3/5**\n\n"
            "系统重装任务已经提交。\n"
            "正在等待阿里云允许继续配置密码...",
            parse_mode="Markdown",
        )

        password_result = await execute_with_retry(
            instance_id=instance_id,
            action="resetpw",
            password=DEFAULT_PASSWORD,
            timeout=REINSTALL_TIMEOUT,
        )

        if not password_result["success"]:
            raise RuntimeError(
                f"重装已提交，但密码设置失败：{password_result['error']}"
            )

        # -------------------------------------------------
        # 4. 密码修改后重启
        # -------------------------------------------------
        await progress_msg.edit_text(
            "⏳ **进度 4/5**\n\n"
            "新密码已写入。\n"
            "正在重启服务器使密码正式生效...",
            parse_mode="Markdown",
        )

        reboot_result = await execute_with_retry(
            instance_id=instance_id,
            action="reboot",
            timeout=POWER_TIMEOUT,
        )

        if not reboot_result["success"]:
            # 如果服务器当前已经是 Stopped，Reboot 可能不适用；此时直接 Start。
            start_result = await asyncio.to_thread(
                _execute_swas_action_sync,
                instance_id,
                "start",
            )
            if not start_result["success"]:
                raise RuntimeError(
                    f"密码已设置，但重启/开机失败：{reboot_result.get('error')}"
                )

        # -------------------------------------------------
        # 5. 最终确认 Running
        # -------------------------------------------------
        await progress_msg.edit_text(
            "⏳ **进度 5/5**\n\n"
            "正在等待服务器重新进入 Running...",
            parse_mode="Markdown",
        )

        await wait_swas_status(
            account_id,
            region_id,
            instance_id,
            "Running",
            POWER_TIMEOUT,
        )

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="🔄 刷新服务器状态",
                callback_data=f"manage_swas_{instance_id}",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="🔙 返回服务器列表",
                callback_data=f"select_acc:{account_id}",
            )
        )

        await progress_msg.edit_text(
            "✅ **轻量云重装完成**\n\n"
            f"🆔 实例: `{instance_id}`\n"
            f"🌍 地域: `{region_id}`\n"
            "✅ 状态: `Running`\n"
            f"🔑 新密码: `{DEFAULT_PASSWORD}`\n\n"
            "已确认服务器进入 Running 状态。",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )

    except Exception as exc:
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="🔄 查看服务器状态",
                callback_data=f"manage_swas_{instance_id}",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="🔙 返回服务器列表",
                callback_data=f"select_acc:{account_id}",
            )
        )

        await progress_msg.edit_text(
            "❌ **重装流程失败**\n\n"
            f"错误：`{_format_error(exc)}`\n\n"
            "程序没有把失败伪装成成功，请打开服务器详情查看当前状态。",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )
