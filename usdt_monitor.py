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

GROUP_A_ID = int(os.environ["GROUP_A_ID"])
GROUP_B_ID = int(os.environ["GROUP_B_ID"])
GROUP_C_ID = int(os.environ["GROUP_C_ID"])

GROUP_A_SENDERS = os.environ["GROUP_A_SENDERS"].split(",")
GROUP_B_SENDERS = os.environ["GROUP_B_SENDERS"].split(",")
GROUP_C_SENDERS = os.environ["GROUP_C_SENDERS"].split(",")

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
try:
    conn = sqlite3.connect(DB_PATH)
    for row in conn.execute("SELECT tx_id FROM processed_txs"):
        processed_tx_set.add(row[0])
    conn.close()
except Exception:
    pass

USDT_CONTRACT_HEX = base58_to_hex(USDT_CONTRACT)

CX_CACHE_SECONDS = 30
_cx_cache_text = None
_cx_cache_time = 0.0

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
    global processed_tx_set
    
    url = f"https://api.trongrid.io/v1/accounts/{MONITORED_ADDRESS}/transactions/trc20"
    params = {
        "contract_address": USDT_CONTRACT,
        "limit": 10,
        "only_confirmed": "false"
    }
    
    try:
        async with httpx.AsyncClient(headers={"TRON-PRO-API-KEY": TRONGRID_API_KEY}) as client:
            response = await client.get(url, params=params, timeout=3)
            if response.status_code != 200:
                return
            
            res_data = response.json()
            if not res_data.get("success"):
                return
            
            transactions = res_data.get("data", [])

            # 从旧到新处理新到账
            for tx in reversed(transactions):
                tx_id = tx.get("transaction_id")
                
                if tx_id not in processed_tx_set and tx.get("to") == MONITORED_ADDRESS:
                    raw_value = int(tx.get("value", 0))
                    decimals = int(tx.get("token_info", {}).get("decimals", 6))
                    amount = raw_value / (10 ** decimals)
                    from_address = tx.get("from")
                    
                    # 后台自动精确判定发送到哪个群，但群消息里不再显示多余标签
                    if from_address in GROUP_A_SENDERS:
                        target_groups = [GROUP_A_ID]
                    elif from_address in GROUP_B_SENDERS:
                        target_groups = [GROUP_B_ID]
                    elif from_address in GROUP_C_SENDERS:
                        target_groups = [GROUP_C_ID]
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
    global _cx_cache_text, _cx_cache_time

    now_ts = time.time()
    if _cx_cache_text and (now_ts - _cx_cache_time) < CX_CACHE_SECONDS:
        await update.message.reply_text(_cx_cache_text, parse_mode="HTML")
        return

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

        _cx_cache_text = "\n".join(lines)
        _cx_cache_time = time.time()

        await update.message.reply_text(_cx_cache_text, parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"❌ 获取失败: {e}")

def main():
    application = Application.builder().token(TG_BOT_TOKEN).build()
    application.add_handler(CommandHandler("cx", cx_command))
    application.add_handler(MessageHandler(filters.Regex(r'^[cC][xX]$'), cx_command))
    application.job_queue.run_repeating(check_usdt_transactions, interval=CHECK_INTERVAL, first=1)
    application.run_polling()

if __name__ == "__main__":
    main()