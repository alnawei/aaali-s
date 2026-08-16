import sqlite3
import os
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models

# ================= 配置区 =================
# 🌟 绝对路径，杜绝写进异次元黑洞
DB_PATH = "/srv/Ali/bot_data.db"
# 🌟 全球无死角扫描大名单
TARGET_REGIONS = [
    "cn-hongkong", "ap-northeast-1", "ap-northeast-2", 
    "ap-southeast-1", "ap-southeast-3", "ap-southeast-5", 
    "ap-southeast-6", "ap-southeast-7", "eu-central-1", 
    "eu-west-1", "eu-west-2", "eu-west-3", "us-west-1", 
    "us-east-1", "me-east-1", "me-central-1", "na-south-1"
]
# ==========================================

def create_client(region_id, ak, sk):
    """初始化 API 客户端"""
    config = open_api_models.Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    config.endpoint = f"ecs.{region_id}.aliyuncs.com"
    return EcsClient(config)

def sync_all_accounts():
    """遍历数据库中所有账号，向阿里云核对服务器真实数量并入库，同时清理已释放的幽灵机器"""
    print("====== 开始全局资产盘点与对账 ======")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # 确保表存在，兜底防报错
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ecs_business (
                instance_id TEXT PRIMARY KEY,
                account_id INTEGER DEFAULT 1,
                name TEXT DEFAULT '无名节点',
                region_id TEXT DEFAULT 'cn-hongkong',
                ip TEXT DEFAULT '0.0.0.0',
                reset_day INTEGER DEFAULT 1,
                traffic_limit_gb INTEGER DEFAULT 500,
                expire_time TEXT DEFAULT '',
                traffic_start_time TEXT DEFAULT ''
            )
        """)
        
        # 提取所有激活状态的账号
        cursor.execute("SELECT id, alias, access_key, access_secret FROM cloud_accounts WHERE is_active = 1")
        accounts = cursor.fetchall()
    except Exception as e:
        print(f"❌ 无法读取账号表，请检查数据库: {e}")
        return

    for account in accounts:
        acc_id, acc_alias, acc_ak, acc_sk = account
        acc_ak = str(acc_ak).strip()
        acc_sk = str(acc_sk).strip()
        
        print(f"\n🏢 正在盘点账号: {acc_alias} (ID: {acc_id})")
        
        for region in TARGET_REGIONS:
            print(f"   ⏳ 正在向阿里云请求 {region} 的机器快照...")
            try:
                client = create_client(region, acc_ak, acc_sk)
                # 请求获取实例列表 (一次最多拉 100 台)
                req = ecs_models.DescribeInstancesRequest(region_id=region, max_results=100)
                resp = client.describe_instances(req)
                
                instances_obj = getattr(resp.body, 'instances', None)
                instance_list = getattr(instances_obj, 'instance', []) if instances_obj else []
                
                # 提取云端真实的实例 ID 列表
                cloud_instance_ids = [inst.instance_id for inst in instance_list]
                
                running = 0
                stopped = 0
                
                for inst in instance_list:
                    if inst.status == 'Running':
                        running += 1
                    elif inst.status == 'Stopped':
                        stopped += 1
                    
                    instance_id = inst.instance_id
                    instance_name = inst.instance_name or f"实例-{instance_id[-4:]}"
                    
                    public_ip = "0.0.0.0"
                    if getattr(inst, 'public_ip_address', None) and getattr(inst.public_ip_address, 'ip_address', []):
                        public_ip = inst.public_ip_address.ip_address[0]
                    elif getattr(inst, 'eip_address', None) and getattr(inst.eip_address, 'ip_address', None):
                        public_ip = inst.eip_address.ip_address
                        
                    # 🌟 修复：智能提取云端机器真实的开机日期，最大不超过 28
                    try:
                        # 阿里云返回的格式类似 "2026-07-21T02:00:00Z"
                        creation_day = int(inst.creation_time.split('-')[2][:2])
                        reset_day = min(creation_day, 28)
                    except:
                        reset_day = 1

                    # 将这台机器写进 ecs_business，并带上精准的 reset_day
                    cursor.execute("""
                        INSERT OR IGNORE INTO ecs_business (instance_id, account_id, name, region_id, ip, reset_day)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (instance_id, acc_id, instance_name, region, public_ip, reset_day))
                    
                    # 更新已有机器
                    cursor.execute("""
                        UPDATE ecs_business SET ip = ?, name = ? WHERE instance_id = ?
                    """, (public_ip, instance_name, instance_id))
                
                # 🌟 清理本地的“幽灵机器”：查找该账号在当前地域的本地记录 (增加过滤，放过 ssh_ 开头的自定义机器)
                cursor.execute("SELECT instance_id FROM ecs_business WHERE account_id = ? AND region_id = ? AND instance_id NOT LIKE 'ssh_%'", (acc_id, region))
                local_instances = [row[0] for row in cursor.fetchall()]
                
                # 如果本地有，但云端没有了，执行删除
                for local_id in local_instances:
                    if local_id not in cloud_instance_ids:
                        # 🌟 双重保险：删除操作处再次拦截 ssh_ 机器，防止被误杀
                        cursor.execute("DELETE FROM ecs_business WHERE instance_id = ? AND instance_id NOT LIKE 'ssh_%'", (local_id,))
                        print(f"   🧹 清理已释放的幽灵机器: {local_id}")

                # 将真实的数量覆盖写入 account_assets 表
                cursor.execute("""
                    INSERT INTO account_assets (account_id, region_id, running_count, stopped_count, last_sync_time)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, region_id) 
                    DO UPDATE SET 
                        running_count=excluded.running_count, 
                        stopped_count=excluded.stopped_count,
                        last_sync_time=CURRENT_TIMESTAMP
                """, (acc_id, region, running, stopped))
                conn.commit()
                
                # 如果该区域一台机器都没有，就不用打印“对账成功”，保持日志清爽
                if running > 0 or stopped > 0:
                    print(f"   ✅ [对账成功] {region} -> 运行中: {running} 台 | 已录入节点中心！")
                
            except Exception as e:
                # 忽略权限不足或该地域未开通的 API 报错，保持扫描顺畅
                if "InvalidAccountStatus" in str(e) or "Forbidden" in str(e):
                    pass
                else:
                    print(f"   ❌ [对账失败] {region}: {str(e)}")
                
    conn.close()
    print("\n🎉 全局资产同步完成！本地账本已是最新状态。")
