import asyncio
import db
import config
from config import DB_PATH
from aiogram import Bot
from datetime import datetime
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_tea_openapi import models as open_api_models
# 引入我们刚才在 server.py 写的查流量黑科技
from handlers.server import get_real_traffic_gb

# 设置一个简单的内存缓存，防止 80% 预警信息每半小时就给你发一次“轰炸”你
alerted_80_instances = set()

async def traffic_monitor_job(bot: Bot, admin_id: int):
    """后台静默巡查任务：计算流量并执行风控"""
    import sqlite3
    import asyncio
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_ecs20140526.client import Client as EcsClient
    from alibabacloud_ecs20140526 import models as ecs_models
    
    print("⏳ [流量监控] 任务启动...")
    
    # 1. 连接本地账本
    conn = sqlite3.connect(db.DB_PATH)
    cursor = conn.cursor()
    
    # 获取所有登记在册的机器
    cursor.execute("SELECT instance_id, traffic_limit_gb, traffic_start_time FROM ecs_business WHERE instance_id NOT LIKE 'ssh_%'")
    rows = cursor.fetchall()
    
    # 🌟 【核心修复】：获取所有已激活的云账号，彻底抛弃旧版的 config 静态秘钥
    cursor.execute("SELECT id, alias, access_key, access_secret FROM cloud_accounts WHERE is_active = 1")
    accounts = cursor.fetchall()
    conn.close()

    if not accounts:
        print("⚠️ [流量监控] 未找到任何激活的云账号，无法执行监控。")
        return

    if not rows:
        return  # 如果数据库里没有机器记录，直接结束

    # ⚠️ 提示：你目前硬编码了 cn-hongkong。如果以后有东京机器，此处需要改为遍历地域。
    region_id = "cn-hongkong"

    # 🌟 遍历所有激活的账号，去寻找并监控这些机器
    for account in accounts:
        acc_id, acc_alias, acc_ak, acc_sk = account
        
        # 清除可能存在的隐藏回车/空格，防止引发 latin-1 编码报错
        acc_ak = str(acc_ak).strip()
        acc_sk = str(acc_sk).strip()

        # 初始化当前账号的阿里云 ECS 客户端
        ali_config = open_api_models.Config(
            access_key_id=acc_ak,
            access_key_secret=acc_sk,
            endpoint=f'ecs.{region_id}.aliyuncs.com'
        )
        ecs_client = EcsClient(ali_config)

        for row in rows:
            instance_id = row[0]
            limit_gb = row[1]
            start_time_str = row[2]

            if not start_time_str:
                continue  # 如果这台机器连计费起点都没设置，直接跳过

            # 2. 【核心优化】先看机器是不是开着的，关机状态就不需要查流量了，节省 API 次数
            try:
                req_status = ecs_models.DescribeInstancesRequest(region_id=region_id, instance_ids=f'["{instance_id}"]')
                # 🛠️ 修正 1：放入线程池执行，防止同步网络请求卡死 Telegram 主循环
                resp_status = await asyncio.to_thread(ecs_client.describe_instances, req_status)
                
                # 如果当前账号下查不到这个实例，说明是别的账号的机器，跳过，留给下一次大循环
                if not resp_status.body.instances.instance:
                    continue
                    
                status = resp_status.body.instances.instance[0].status
                if status != "Running":
                    continue # 只要不是 Running，就放过它
            except Exception as e:
                # 权限拦截或网络波动，静默跳过
                continue

            # 3. 去查真实的流量 (加入 0.5 秒睡眠，防止并发请求太多被阿里云 API 封禁)
            await asyncio.sleep(0.5) 
            
            # 🛠️ 修正 2：为整个查询和计算逻辑加上 try...except
            try:
                # ⚠️ 【极其重要】：如果你的 get_real_traffic_gb 函数内部也用了 config.ALIYUN_ACCESS_KEY_ID，
                # 你必须进那个函数里，把它也改成能接收 acc_ak 和 acc_sk 的多账号模式！否则那里还是会报错。
                current_traffic = await asyncio.to_thread(get_real_traffic_gb, instance_id, start_time_str)
                
                # 4. 计算消耗比例，执行风控判断
                usage_percent = current_traffic / limit_gb

                # 🛑 【最高警报】：触发 95% 熔断关机
                if usage_percent >= 0.95:
                    try:
                        # 下发强制关机指令！
                        req_stop = ecs_models.StopInstanceRequest(instance_id=instance_id)
                        await asyncio.to_thread(ecs_client.stop_instance, req_stop)
                        
                        # 给老板发送拔管通知
                        await bot.send_message(
                            chat_id=admin_id,
                            text=f"🚨 **【系统熔断断网警报】**\n\n"
                                 f"🏢 归属账号: `{acc_alias}`\n"
                                 f"🆔 实例: `{instance_id}`\n"
                                 f"📶 当前流量: `{current_traffic} GB` / `{limit_gb} GB`\n"
                                 f"⚠️ 消耗已达 **{usage_percent*100:.1f}%**\n"
                                 f"🛑 系统已自动执行**物理关机**，防止产生天价流量账单！"
                        )
                        # 清理预警记录
                        alerted_80_instances.discard(instance_id)
                    except Exception as e:
                        await bot.send_message(admin_id, f"❌ **警告：对实例 {instance_id} 执行熔断关机失败**，请立刻手动处理！\n报错: {e}")

                # 📢 【中级警报】：触发 80% 预警
                elif usage_percent >= 0.80 and usage_percent < 0.95:
                    # 只有还没被警告过的，才发消息，防骚扰
                    if instance_id not in alerted_80_instances:
                        await bot.send_message(
                            chat_id=admin_id,
                            text=f"⚠️ **【流量消耗极速预警】**\n\n"
                                 f"🏢 归属账号: `{acc_alias}`\n"
                                 f"🆔 实例: `{instance_id}`\n"
                                 f"📶 当前流量: `{current_traffic} GB` / `{limit_gb} GB`\n"
                                 f"📢 消耗已达 **{usage_percent*100:.1f}%**，即将面临熔断，请注意客户续费动态。"
                        )
                        alerted_80_instances.add(instance_id) # 标记为“已警告”
                
                # 🟢 【绿灯恢复】：如果有人调高了流量包或重置了流量，比例降下来了
                elif usage_percent < 0.80:
                    alerted_80_instances.discard(instance_id) # 解除它的警告标记
                    
            except Exception as e:
                # 单台查询失败仅打印日志或提示，用 continue 保证后续其它机器正常被巡查
                print(f"⚠️ [流量监控] 检查实例 {instance_id} 时发生异常: {e}")
                continue
                
async def daily_billing_check_job(bot: Bot, admin_id: int):
    """每日后台巡查任务：主动推送即将到期的机器，提醒老板催费"""
    import sqlite3
    import db
    
    conn = sqlite3.connect(db.DB_PATH)
    cursor = conn.cursor()
    # 只需要拿实例 ID 和 到期时间
    cursor.execute("SELECT instance_id, expire_time FROM ecs_business")
    rows = cursor.fetchall()
    conn.close()

    now = datetime.now()
    alerts = []

    for row in rows:
        instance_id = row[0]
        expire_time_str = row[1]
        
        if not expire_time_str:
            continue

        try:
            expire_date = datetime.strptime(expire_time_str, "%Y-%m-%d")
            days_left = (expire_date - now).days

            short_id = instance_id[-6:]

            # 💡 精准推送逻辑：只有在剩余3天、1天 和 今天到期时，才发通知。
            # 这样既能起到提醒作用，又不会每天无脑轰炸你。
            if days_left in [3, 1]:
                alerts.append(f"• `...{short_id}` 剩余 **{days_left}** 天 (到期: `{expire_time_str}`)")
            elif days_left == 0:
                alerts.append(f"• `...{short_id}` 🚨 **今日到期！请立即处理！**")
            elif days_left == -1:
                alerts.append(f"• `...{short_id}` ❌ **已过期 1 天，请确认是否拔管释放**")
        except ValueError:
            pass

    # 如果今天有需要催费的机器，就给老板发微信（TG）
    if alerts:
        msg = "🔔 **【每日催费与到期预警】**\n\n老板，以下机器即将到期，请及时联系客户续费：\n\n" + "\n".join(alerts)
        # 🛠️ 修正 3：包裹异常并增加降级策略，防范由于字符原因导致的 Markdown 语法崩溃发不出消息
        try:
            await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            print(f"⚠️ Markdown 解析异常，降级为普通文本发送: {e}")
            try:
                await bot.send_message(chat_id=admin_id, text=msg, parse_mode=None)
            except Exception as e2:
                print(f"❌ 催费通知发送彻底失败: {e2}")
