import os
import time
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from decimal import Decimal, ROUND_DOWN
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def base58_to_hex(addr: str) -> str:
    n = 0
    for c in addr:
        n = n * 58 + BASE58_ALPHABET.index(c)
    return n.to_bytes(25, 'big')[:21].hex()

# ================= 配置区 =================
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
MONITORED_ADDRESS = os.environ["MONITORED_ADDRESS"]

# 读取各大群组的 ID
GROUP_A_ID = int(os.environ["GROUP_A_ID"])
GROUP_B_ID = int(os.environ["GROUP_B_ID"])
GROUP_C_ID = int(os.environ["GROUP_C_ID"])
GROUP_D_ID = int(os.environ["GROUP_D_ID"])
GROUP_E_ID = int(os.environ["GROUP_E_ID"])
GROUP_F_ID = int(os.environ["GROUP_F_ID"])

# 读取各大群组对应的付款地址列表
GROUP_A_SENDERS = os.environ["GROUP_A_SENDERS"].split(",")
GROUP_B_SENDERS = os.environ["GROUP_B_SENDERS"].split(",")
GROUP_C_SENDERS = os.environ["GROUP_C_SENDERS"].split(",")
GROUP_D_SENDERS = os.environ["GROUP_D_SENDERS"].split(",")
GROUP_E_SENDERS = os.environ["GROUP_E_SENDERS"].split(",")
GROUP_F_SENDERS = os.environ["GROUP_F_SENDERS"].split(",")

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "4"))
TRONGRID_API_KEY = "e0513fec-d546-4a16-bd68-9bcdbdc1322d"
# ==========================================

DB_PATH = "processed_txs.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS processed_txs (tx_id TEXT PRIMARY KEY, created_at TEXT DEFAULT (datetime('now')))")
    conn.commit()
    conn.close()

init_db()

processed_tx_set = set()
IS_FIRST_RUN = True  # 🌟 新增：首次运行标记，用来防刷屏

try:
    conn = sqlite3.connect(DB_PATH)
    for row in conn.execute("SELECT tx_id FROM processed_txs"):
        processed_tx_set.add(row[0])
    conn.close()
except Exception:
    pass

USDT_CONTRACT_HEX = base58_to_hex(USDT_CONTRACT)

async def fetch_balances(address: str):
    try:
        async with httpx.AsyncClient(headers={"TRON-PRO-API-KEY": TRONGRID_API_KEY}) as client:
            resp = await client.get(f"https://api.trongrid.io/v1/accounts/{address}", timeout=3)
            if resp.status_code != 200:
                return 0, 0
            result = resp.json()
            account_list = result.get("data", [])
            if not account_list:
                return 0, 0
            account = account_list[0]
            trx_raw = account.get("balance", 0)
            trx = Decimal(str(trx_raw)) / Decimal("1000000")
            trx = float(trx.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
            usdt = 0
            for token in account.get("trc20", []):
                if USDT_CONTRACT in token:
                    usdt = Decimal(token[USDT_CONTRACT]) / Decimal("1000000")
                    usdt = float(usdt.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
                    break
                if USDT_CONTRACT_HEX in token:
                    usdt = Decimal(token[USDT_CONTRACT_HEX]) / Decimal("1000000")
                    usdt = float(usdt.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
                    break
            return usdt, trx
    except Exception:
        return 0, 0

async def check_usdt_transactions(context: ContextTypes.DEFAULT_TYPE):
    global processed_tx_set, IS_FIRST_RUN
    
    url = f"https://api.trongrid.io/v1/accounts/{MONITORED_ADDRESS}/transactions/trc20"
    params = {
        "contract_address": USDT_CONTRACT,
        "limit": 10,
        "only_confirmed": "false"
    }
    
    try:
        async with httpx.AsyncClient(headers={"TRON-PRO-API-KEY": TRONGRID_API_KEY}) as client:
            response = await client.get(url, params=params, timeout=8)
            if response.status_code != 200:
                return
            
            res_data = response.json()
            if not res_data.get("success"):
                return
            
            transactions = res_data.get("data", [])

            # 🌟 核心防御：如果是刚部署启动且数据库是空的，开启静默模式，只记账不发通知
            skip_alerts = False
            if IS_FIRST_RUN and len(processed_tx_set) == 0:
                skip_alerts = True
                print("🤖 监测到机器人重新部署且本地缓存清空，正在静默同步最新历史账单，不触发群通知...")

            # 从旧到新处理新到账
            for tx in reversed(transactions):
                tx_id = tx.get("transaction_id")
                
                if tx_id not in processed_tx_set and tx.get("to") == MONITORED_ADDRESS:
                    
                    # 🌟 如果处于静默模式，直接塞进数据库和内存，跳过发群消息
                    if skip_alerts:
                        conn = sqlite3.connect(DB_PATH)
                        conn.execute("INSERT OR IGNORE INTO processed_txs (tx_id) VALUES (?)", (tx_id,))
                        conn.commit()
                        conn.close()
                        processed_tx_set.add(tx_id)
                        continue

                    raw_value = int(tx.get("value", 0))
                    decimals = int(tx.get("token_info", {}).get("decimals", 6))
                    amount = raw_value / (10 ** decimals)
                    from_address = tx.get("from")
                    
                    # 精准匹配 A、B、C、D、E、F 对应的群
                    if from_address in GROUP_A_SENDERS:
                        target_groups = [GROUP_A_ID]
                    elif from_address in GROUP_B_SENDERS:
                        target_groups = [GROUP_B_ID]
                    elif from_address in GROUP_C_SENDERS:
                        target_groups = [GROUP_C_ID]
                    elif from_address in GROUP_D_SENDERS:
                        target_groups = [GROUP_D_ID]
                    elif from_address in GROUP_E_SENDERS:
                        target_groups = [GROUP_E_ID]
                    elif from_address in GROUP_F_SENDERS:
                        target_groups = [GROUP_F_ID]
                    else:
                        continue
                    
                    await asyncio.sleep(3)
                    usdt_balance, trx_balance = await fetch_balances(from_address)

                    message = (
                        f"🔔 <b>收到一笔 USDT 到账提醒！呜呼 发财啦🎉🎉🎉</b>\n\n"
                        f"💰 <b>到账金额:</b> <code>{amount:.4f}</code> USDT\n"
                        f"👤 <b>付款地址:</b> <code>{from_address}</code>\n"
                        f"📊 <b>对方USDT余额:</b> <code>{usdt_balance:.4f}</code>\n"
                        f"📊 <b>对方TRX余额:</b> <code>{trx_balance:.4f}</code>\n"
                        f"🔗 <b>区块哈希:</b> <code>{tx_id}</code>"
                    )
                    
                    # 精准投递到对应的群
                    for chat_id in target_groups:
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=message,
                                parse_mode="HTML",
                                disable_web_page_preview=True
                            )
                        except Exception as send_err:
                            print(f"群组 {chat_id} 发送失败: {send_err}")
                    
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("INSERT OR IGNORE INTO processed_txs (tx_id) VALUES (?)", (tx_id,))
                    conn.commit()
                    conn.close()
                    processed_tx_set.add(tx_id)
            
            # 🌟 无论这轮有没有新交易，第一轮轮询顺利结束，就关闭首次运行标记
            if IS_FIRST_RUN:
                IS_FIRST_RUN = False

            if len(processed_tx_set) > 200:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("DELETE FROM processed_txs WHERE tx_id NOT IN (SELECT tx_id FROM processed_txs ORDER BY created_at DESC LIMIT 200)")
                conn.commit()
                conn.close()
                processed_tx_set.clear()
                conn = sqlite3.connect(DB_PATH)
                for row in conn.execute("SELECT tx_id FROM processed_txs"):
                    processed_tx_set.add(row[0])
                conn.close()
                
    except Exception as e:
        print(f"网络轮询异常: {e}")

async def cx_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://www.okx.com",
            "Referer": "https://www.okx.com/c2c/trading",
        }

        async with httpx.AsyncClient(follow_redirects=True) as client:
            async def fetch_v3(side):
                resp = await client.get(
                    "https://www.okx.com/v3/c2c/tradingOrders/books",
                    params={
                        "baseCurrency": "USDT",
                        "quoteCurrency": "CNY",
                        "side": side,
                        "paymentMethod": "all",
                        "userType": "all",
                        "t": int(time.time() * 1000),
                    },
                    headers=headers,
                    timeout=5
                )
                text = resp.text
                if not text.strip():
                    raise Exception("empty response")
                data = resp.json()
                if str(data.get("code")) != "0":
                    raise Exception(f"OKX error: code={data.get('code')}, msg={data.get('msg')}")
                return data

            sell_resp, buy_resp = await asyncio.gather(
                fetch_v3("sell"),
                fetch_v3("buy")
            )

        sell_data = sell_resp.get("data", {})
        buy_data = buy_resp.get("data", {})
        sell_list = sell_data.get("sell", []) if isinstance(sell_data, dict) else sell_data
        buy_list = buy_data.get("buy", []) if isinstance(buy_data, dict) else buy_data

        if not sell_list or not buy_list:
            await update.message.reply_text("❌ 暂无商家报价")
            return

        lines = ["<b>💱 OKX 商家 C2C实时交易汇率</b>\n"]
        lines.append("━━━ 商家卖USDT Top 10 ━━━")
        for i, ad in enumerate(sell_list[:10], 1):
            fname = ad.get("nickName", ad.get("userName", "未知"))
            lines.append(f"{i}. <b>{ad['price']}</b> {fname}")

        now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        lines.append("")
        lines.append(f"⏰ <i>{now}</i>")
        lines.append("")
        lines.append("<b>💰 使用十七机器人，你会成为人上人</b>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"❌ 获取失败: {e}")

async def universal_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = update.effective_chat
        message = update.effective_message
        
        if not chat or not message or not message.text:
            return
            
        user_text = message.text.strip().lower()
        
        if user_text == "cx":
            await cx_command(update, context)
            return
            
        if "查群id" in user_text or "群id" in user_text:
            if chat.type in ["group", "supergroup"]:
                group_id = chat.id
                group_name = chat.title
                
                reply_text = (
                    f"📋 <b>群组信息查询成功</b>:\n\n"
                    f"👤 <b>群名称:</b> {group_name}\n"
                    f"🆔 <b>群组 ID:</b> <code>{group_id}</code>\n\n"
                    f"<i>(提示：点击上方ID数字可自动复制)</i>"
                )
                await message.reply_text(reply_text, parse_mode="HTML")
            else:
                await message.reply_text("❌ 请在需要查询的群组中发送该指令。")
                
    except Exception as e:
        print(f"统一消息处理异常: {repr(e)}")

def main():
    application = Application.builder().token(TG_BOT_TOKEN).build()
    application.add_handler(CommandHandler("cx", cx_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, universal_message_handler))
    application.job_queue.run_repeating(check_usdt_transactions, interval=CHECK_INTERVAL, first=1)
    application.run_polling()

if __name__ == "__main__":
    main()
