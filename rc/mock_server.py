#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, uuid, asyncio, signal
from aiohttp import web

# ========= 基本状態 =========
ORDERS = {}  # order_id -> order dict (w1..c3, point:{dest_id})
DRONE_STATE = {"status": "待機中", "eta": None}

WS_DELIVERY = set()  # /Delivery/ws の接続
WS_ORDER    = set()  # /Order/ws の接続

# 単純制御: 同時配達は1件まで
delivery_lock = asyncio.Lock()

# ========= ユーティリティ =========
def project_dir():
    return os.path.abspath(os.path.dirname(__file__))

def web_path(name_candidates):
    """./Web/ 以下から最初に見つかったものを返す"""
    base = os.path.join(project_dir(), "Web")
    for n in name_candidates:
        p = os.path.join(base, n)
        if os.path.exists(p):
            return p
    # 見つからなければ最後の候補を返す（エラー避け）
    return os.path.join(base, name_candidates[-1])

def to_nonneg_int(v):
    try:
        n = int(v)
        return n if n >= 0 else 0
    except Exception:
        return 0

def sanitize_order_payload(data):
    keys = ["w1","w2","w3","j1","j2","j3","c1","c2","c3"]
    point = data.get("point")
    if not isinstance(point, dict) or not point.get("dest_id"):
        raise web.HTTPBadRequest(text=json.dumps({"error":"point.dest_id is required"}), content_type="application/json")
    order = {k: to_nonneg_int(data.get(k, 0)) for k in keys}
    if sum(order.values()) == 0:
        raise web.HTTPBadRequest(text=json.dumps({"error":"at least one item is required"}), content_type="application/json")
    order["point"] = {"dest_id": str(point["dest_id"])}
    return order

async def broadcast_snapshot():
    """100msごとに state+orders を配信"""
    while True:
        snapshot = json.dumps({"state": DRONE_STATE, "order": list(ORDERS.values())}, ensure_ascii=False)
        # 送信（壊れたWSは外す）
        dead = []
        for ws in list(WS_DELIVERY):
            try:
                await ws.send_str(snapshot)
            except Exception:
                dead.append(ws)
        for ws in dead:
            WS_DELIVERY.discard(ws)

        dead = []
        for ws in list(WS_ORDER):
            try:
                await ws.send_str(snapshot)
            except Exception:
                dead.append(ws)
        for ws in dead:
            WS_ORDER.discard(ws)

        await asyncio.sleep(0.1)

async def simulate_delivery(order_id: str):
    """超簡易シミュレーション：ETAを減らし、完了したら注文を消す"""
    # 同時発進を制御
    async with delivery_lock:
        if order_id not in ORDERS:
            return
        # 準備〜開始
        DRONE_STATE["status"] = "配達準備中"; DRONE_STATE["eta"] = None
        await asyncio.sleep(0.8)

        DRONE_STATE["status"] = "配達開始"; DRONE_STATE["eta"] = None
        await asyncio.sleep(0.5)

        # 配達中: 6秒カウントダウン
        DRONE_STATE["status"] = "配達中"; DRONE_STATE["eta"] = 6.0
        for _ in range(12):  # 0.5秒 x 12 = 6秒
            await asyncio.sleep(0.5)
            if isinstance(DRONE_STATE["eta"], (int, float)) and DRONE_STATE["eta"] > 0:
                DRONE_STATE["eta"] = round(DRONE_STATE["eta"] - 0.5, 1)

        # 完了→注文削除
        DRONE_STATE["status"] = "配達完了"; DRONE_STATE["eta"] = None
        ORDERS.pop(order_id, None)
        await asyncio.sleep(1.2)

        # 帰還→待機
        DRONE_STATE["status"] = "帰還中"
        await asyncio.sleep(1.0)
        DRONE_STATE["status"] = "待機中"; DRONE_STATE["eta"] = None

# ========= ハンドラ =========
async def index_delivery(request: web.Request):
    # deliveryUI.html or delivery.html を優先順で探す
    return web.FileResponse(web_path(["deliveryUI.html", "delivery.html"]))

async def index_order(request: web.Request):
    # orderUI.html or order.html を優先順で探す
    return web.FileResponse(web_path(["orderUI.html", "order.html"]))

async def register_order(request: web.Request):
    data = await request.json()
    order = sanitize_order_payload(data)
    order_id = str(uuid.uuid4())
    order["id"] = order_id
    ORDERS[order_id] = order
    return web.json_response({"result":"order registered", "order_id": order_id})

async def start_delivery(request: web.Request):
    data = await request.json()
    order_id = data.get("order_id")
    if not order_id or order_id not in ORDERS:
        raise web.HTTPBadRequest(text=json.dumps({"error":"invalid order_id"}), content_type="application/json")

    # すでに配達中なら 409
    if DRONE_STATE["status"] not in ("待機中", "配達完了"):
        raise web.HTTPConflict(text=json.dumps({"error":"drone busy"}), content_type="application/json")

    # 非同期タスクでシミュレーション開始
    request.app["tasks"].add(asyncio.create_task(simulate_delivery(order_id)))
    return web.json_response({"result":"delivery started"})

async def ws_delivery(request: web.Request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    WS_DELIVERY.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        WS_DELIVERY.discard(ws)
    return ws

async def ws_order(request: web.Request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    WS_ORDER.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        WS_ORDER.discard(ws)
    return ws

async def on_startup(app: web.Application):
    app["broadcaster"] = asyncio.create_task(broadcast_snapshot())
    app["tasks"] = set()

async def on_cleanup(app: web.Application):
    app["broadcaster"].cancel()
    for t in list(app["tasks"]):
        t.cancel()

def create_app():
    app = web.Application()
    app.router.add_get("/Delivery", index_delivery)
    app.router.add_get("/Order",    index_order)
    app.router.add_post("/register_order", register_order)
    app.router.add_post("/start_delivery", start_delivery)
    app.router.add_get("/Delivery/ws", ws_delivery)
    app.router.add_get("/Order/ws",    ws_order)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

def create_app():
    app = web.Application()
    app.router.add_get("/Delivery", index_delivery)
    app.router.add_get("/Order",    index_order)
    app.router.add_post("/register_order", register_order)
    app.router.add_post("/start_delivery", start_delivery)
    app.router.add_get("/Delivery/ws", ws_delivery)
    app.router.add_get("/Order/ws",    ws_order)

    # ▼ ここから追加：/images/ を Web/images にマッピング
    # HTMLを探したディレクトリを基準に、images/ を見つける
    base_html = os.path.dirname(web_path(["orderUI.html", "order.html"]))
    img_dir = os.path.join(base_html, "images")
    if os.path.isdir(img_dir):
      app.router.add_static("/images/", img_dir, name="images", show_index=False)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

# ========= main =========
def main():
    # Ctrl+C で終了
    signal.signal(signal.SIGINT, lambda *a: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))

    app = create_app()
    web.run_app(app, host="0.0.0.0", port=5000)

if __name__ == "__main__":
    main()


#python mock_server.py
# → http://localhost:5000/Order / http://localhost:5000/Delivery