from database import redis_client
import json
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import BINANCE_API_KEY, BINANCE_API_SECRET
from account_positions import get_account_status
import time

client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
REDIS_KEY = "trading_records"

TP_SL_TYPES = {
    "sl": ["STOP", "STOP_MARKET"],
    "tp": ["TAKE_PROFIT", "TAKE_PROFIT_MARKET"]
}

def save_trade_record(record: dict):
    """保存交易记录"""
    redis_client.lpush(REDIS_KEY, json.dumps(record))

def _normalize_qty(symbol: str, qty: float):
    """按精度修正数量"""
    info = client.futures_exchange_info()
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize", 1))
                    min_qty = float(f.get("minQty", 0))
                    if qty < min_qty:
                        qty = min_qty
                    qty = qty - (qty % step)
                    return qty
    return qty

def get_min_notional(symbol: str, default=0):
    """最小下单金额"""
    info = client.futures_exchange_info()
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "MIN_NOTIONAL":
                    try:
                        return float(f.get("notional", default))
                    except Exception:
                        return default
    return default

def cancel_algo_order(symbol, algoId=None, clientAlgoId=None):
    if not algoId and not clientAlgoId:
        print("⚠ 必须提供 algoId 或 clientAlgoId")
        return
    try:
        client.futures_cancel_algo_order(
            symbol=symbol,
            algoId=algoId,
            clientAlgoId=clientAlgoId
        )
        print(f"♻ 撤销条件单成功: algoId={algoId}, clientAlgoId={clientAlgoId}")
    except Exception as e:
        print(f"⚠ 撤销条件单失败: algoId={algoId}, clientAlgoId={clientAlgoId}, 错误: {e}")


# ===============================
# 下单 TP/SL（独立函数）
# ===============================

def _cancel_tp_sl(symbol, position_side, cancel_sl=True, cancel_tp=True):
    """
    取消指定方向、指定类型的 TP/SL
    支持基础挂单 + 条件单
    """
    types_to_cancel = []
    if cancel_sl:
        types_to_cancel += TP_SL_TYPES["sl"]
    if cancel_tp:
        types_to_cancel += TP_SL_TYPES["tp"]
    if not types_to_cancel:
        return

    # -------------------------------
    # 1️⃣ 取消基础挂单
    # -------------------------------
    try:
        open_orders = client.futures_get_open_orders(symbol=symbol)
    except Exception as e:
        print(f"⚠ 获取基础挂单失败: {e}")
        open_orders = []

    seen_ids = set()
    for o in open_orders:
        if (
            o.get("positionSide") == position_side
            and o.get("type") in types_to_cancel
            and o.get("status") in ["NEW", "PARTIALLY_FILLED"]
        ):
            oid = o["orderId"]
            if oid not in seen_ids:
                seen_ids.add(oid)
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=oid)
                    print(
                        f"♻ 取消基础单 {position_side} {o['type']} | id={oid} stop={o.get('stopPrice')}"
                    )
                except Exception as e:
                    print(f"⚠ 取消基础单失败 id={oid}: {e}")

    # -------------------------------
    # 2️⃣ 取消条件单（Algo Order）
    # -------------------------------
    try:
        algo_orders = client.futures_get_open_orders(symbol=symbol, conditional=True)
    except Exception as e:
        print(f"⚠ 获取条件单失败: {e}")
        algo_orders = []

    for o in algo_orders:
        if o.get("positionSide") == position_side and o.get("orderType") in types_to_cancel:
            cancel_algo_order(symbol=symbol, algoId=o.get("algoId"), clientAlgoId=o.get("clientAlgoId"))


def _place_tp_sl(symbol, position_side, sl=None, tp=None):
    """
    下止损/止盈单（支持条件单）
    返回下单结果列表
    """
    results = []
    if sl:
        try:
            order = client.futures_create_order(
                symbol=symbol,
                side="SELL" if position_side == "LONG" else "BUY",
                positionSide=position_side,
                type="STOP_MARKET",  # 条件止损
                stopPrice=float(sl),
                closePosition=True,
                timeInForce="GTC"
            )
            print(f"🛑 设置止损条件单成功 {symbol}: {sl}")
            results.append(order)
        except Exception as e:
            print(f"⚠ 止损条件单下单失败 {symbol}: {e}")

    if tp:
        try:
            order = client.futures_create_order(
                symbol=symbol,
                side="SELL" if position_side == "LONG" else "BUY",
                positionSide=position_side,
                type="TAKE_PROFIT_MARKET",  # 条件止盈
                stopPrice=float(tp),
                closePosition=True,
                timeInForce="GTC"
            )
            print(f"🎯 设置止盈条件单成功 {symbol}: {tp}")
            results.append(order)
        except Exception as e:
            print(f"⚠ 止盈条件单下单失败 {symbol}: {e}")

    return results

def _update_tp_sl(symbol, position_side, sl=None, tp=None):
    """
    更新止盈止损：
    - 先取消已有 TP/SL
    - 下新单
    返回订单对象列表
    """
    _cancel_tp_sl(symbol, position_side, cancel_sl=bool(sl), cancel_tp=bool(tp))
    time.sleep(1)  # 等待 Binance 处理旧订单
    return _place_tp_sl(symbol, position_side, sl, tp)

# ===============================
# 主交易执行
# ===============================
def execute_trade(symbol: str, action: str, stop_loss=None, take_profit=None,
                  quantity=None, position_size=None):
    """
    执行交易函数（不使用杠杆）
    - symbol: 交易对
    - action: open_long, open_short, close_long, close_short, reverse, increase_position, decrease_position,
              update_stop_loss, update_take_profit
    - stop_loss / take_profit: 止损/止盈价格
    - quantity: 指定合约数量
    - position_size: 指定 USDT 金额（会自动换算成合约数量）
    """
    try:
        # 获取当前持仓和标记价格
        acc = get_account_status()
        pos = next((p for p in acc["positions"] if p["symbol"] == symbol), None)
        mark = float(pos["mark_price"]) if pos else float(
            client.futures_mark_price(symbol=symbol)["markPrice"]
        )

        # 计算下单数量
        qty = None
        if position_size:  # 用 USDT 金额计算 qty
            qty = float(position_size) / mark
        elif quantity:
            qty = float(quantity)
        else:
            if action in ["open_long", "open_short", "increase_position"]:
                print(f"⚠ {symbol} 缺少 position_size 或 quantity，无法执行开仓/加仓")
                return None

        if qty:
            # 精度修正
            qty = _normalize_qty(symbol, qty)

            # 检查最小下单金额
            min_notional = get_min_notional(symbol)
            if qty * mark < min_notional:
                qty = _normalize_qty(symbol, min_notional / mark)
                print(f"⚠ {symbol} 金额过小 → 自动提升至最小金额，下单数量调整为 {qty}")

        current = abs(pos["size"]) if pos else 0

        # 下单函数
        def place_order(**kwargs):
            order = client.futures_create_order(**kwargs)
            save_trade_record({
                "symbol": symbol,
                "action": action,
                "order": kwargs,
                "price": mark,
                "quantity": kwargs.get("quantity"),
                "status": order.get("status")
            })
            return order

        # 执行动作
        if action == "open_long":
            order = place_order(symbol=symbol, side="BUY", positionSide="LONG",
                                type="MARKET", quantity=qty)
            _update_tp_sl(symbol, "LONG", sl=stop_loss, tp=take_profit)
            return order

        elif action == "open_short":
            order = place_order(symbol=symbol, side="SELL", positionSide="SHORT",
                                type="MARKET", quantity=qty)
            _update_tp_sl(symbol, "SHORT", sl=stop_loss, tp=take_profit)
            return order

        elif action == "close_long":
            if not pos or pos["size"] <= 0:
                return None
            return place_order(symbol=symbol, side="SELL", positionSide="LONG",
                               type="MARKET", quantity=current)

        elif action == "close_short":
            if not pos or pos["size"] >= 0:
                return None
            return place_order(symbol=symbol, side="BUY", positionSide="SHORT",
                               type="MARKET", quantity=current)

        elif action == "reverse":
            if not pos or current <= 0:
                return None
            if pos["size"] > 0:  # 平多 → 开空
                place_order(symbol=symbol, side="SELL", positionSide="LONG",
                            type="MARKET", quantity=current)
                order = place_order(symbol=symbol, side="SELL", positionSide="SHORT",
                                    type="MARKET", quantity=qty)
                _update_tp_sl(symbol, "SHORT", sl=stop_loss, tp=take_profit)
                return order
            else:  # 平空 → 开多
                place_order(symbol=symbol, side="BUY", positionSide="SHORT",
                            type="MARKET", quantity=current)
                order = place_order(symbol=symbol, side="BUY", positionSide="LONG",
                                    type="MARKET", quantity=qty)
                _update_tp_sl(symbol, "LONG", sl=stop_loss, tp=take_profit)
                return order

        elif action == "increase_position":
            if not qty:
                print(f"⚠ {symbol} increase_position 缺少下单数量")
                return None
            if pos["size"] > 0:  # 加多
                return place_order(symbol=symbol, side="BUY", positionSide="LONG",
                                   type="MARKET", quantity=qty)
            elif pos["size"] < 0:  # 加空
                return place_order(symbol=symbol, side="SELL", positionSide="SHORT",
                                   type="MARKET", quantity=qty)

        elif action == "decrease_position":
            if not pos:
                return None
            reduce_qty = qty if qty else current / 2
            reduce_qty = min(reduce_qty, current)
            if pos["size"] > 0:  # 减多
                return place_order(symbol=symbol, side="SELL", positionSide="LONG",
                                   type="MARKET", quantity=reduce_qty)
            elif pos["size"] < 0:  # 减空
                return place_order(symbol=symbol, side="BUY", positionSide="SHORT",
                                   type="MARKET", quantity=reduce_qty)

        elif action == "update_stop_loss":
            if pos:
                side = "LONG" if pos["size"] > 0 else "SHORT"
                orders = _update_tp_sl(symbol, side, sl=stop_loss, tp=None)
                return orders if orders else None
            return None

        elif action == "update_take_profit":
            if pos:
                side = "LONG" if pos["size"] > 0 else "SHORT"
                orders = _update_tp_sl(symbol, side, sl=None, tp=take_profit)
                return orders if orders else None
            return None

        else:
            print(f"⚠ 未识别动作: {action}")
            return None

    except BinanceAPIException as e:
        print(f"❌ Binance 下单异常 → {symbol}: {e}")
        return None
    except Exception as e:
        print(f"❌ 其他异常 → {symbol}: {e}")
        return None
