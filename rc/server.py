#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, threading, signal, asyncio, json, uuid
from aiohttp import web
import libs.hakosim as hakosim

# ==================== グローバル ====================
connected_ws_delivery = set()
connected_ws_order = set()
should_run = True

current_state = {
    "x":0,"y":0,"z":0,
    "roll":0,"pitch":0,"yaw":0,
    "status":"待機中",
    "eta": None
}

current_order = {}  # 注文ID: 注文情報

DEFAULT_SPEED = 1
DEFAULT_HEIGHT = 0.5

# ==================== Drone操作 ====================
def transport(client, baggage_pos, transfer_pos, order_id):
    current_state["status"] = "配達準備中"
    client.takeoff(DEFAULT_HEIGHT)
    time.sleep(1)
    client.moveToPosition(baggage_pos['x'], baggage_pos['y'], DEFAULT_HEIGHT, DEFAULT_SPEED)
    client.grab_baggage(True)
    time.sleep(2)

    current_state["status"] = "配達開始"
    stop_eta = False
    def eta_loop():
        while not stop_eta:
            update_eta(transfer_pos)
            time.sleep(0.1)
    t = threading.Thread(target=eta_loop, daemon=True)
    t.start()

    client.moveToPosition(transfer_pos['x'], transfer_pos['y'], DEFAULT_HEIGHT, DEFAULT_SPEED)
    stop_eta = True
    t.join()
    client.grab_baggage(False)

    current_state["status"] = "配達完了"
    current_state["eta"] = None

    # 注文削除
    if order_id in current_order:
        current_order.pop(order_id)

    time.sleep(2)
    current_state["status"] = "帰還中"
    client.moveToPosition(0,0,DEFAULT_HEIGHT,5)
    time.sleep(1)
    current_state["status"] = "待機中"
    client.land()

def debug_pos(client):
    pose = client.simGetVehiclePose()
    roll, pitch, yaw = hakosim.hakosim_types.Quaternionr.quaternion_to_euler(pose.orientation)
    current_state.update({
        "x": pose.position.x_val,
        "y": pose.position.y_val,
        "z": pose.position.z_val,
        "roll": roll, "pitch": pitch, "yaw": yaw
    })

def update_eta(target_pos):
    dx = target_pos['x'] - current_state['x']
    dy = target_pos['y'] - current_state['y']
    dz = target_pos.get('z', DEFAULT_HEIGHT) - current_state['z']
    distance = (dx**2 + dy**2 + dz**2)**0.5
    eta_sec = distance / DEFAULT_SPEED
    if current_state.get('eta') is None or eta_sec < current_state['eta']:
        current_state['eta'] = eta_sec

# ==================== aiohttp API ====================
async def index_delivery(request):
    filepath = os.path.join(os.path.dirname(__file__), './Web/delivery.html')
    return web.FileResponse(filepath)

async def index_order(request):
    filepath = os.path.join(os.path.dirname(__file__), './Web/order.html')
    return web.FileResponse(filepath)

async def register_order(request):
    data = await request.json()
    coords = data.get("coords")
    if not coords:
        return web.json_response({"error":"Invalid coordinates"}, status=400)

    order_id = str(uuid.uuid4())
    current_order[order_id] = {
        "id": order_id,
        "omurice": int(data.get("omurice",0)),
        "hamburg": int(data.get("hamburg",0)),
        "onigiri": int(data.get("onigiri",0)),
        "coords": coords
    }
    return web.json_response({"result":"order registered","order_id":order_id})

async def start_delivery(request):
    data = await request.json()
    coords = data.get("coords")
    order_id = data.get("order_id")
    if not coords or not order_id:
        return web.json_response({"error":"Invalid request"}, status=400)

    threading.Thread(
        target=transport,
        args=(request.app["client"], {"x":2,"y":0,"z":DEFAULT_HEIGHT}, coords, order_id),
        daemon=True
    ).start()
    return web.json_response({"result":"delivery started"})

# ==================== WebSocket ====================
async def websocket_handler_delivery(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connected_ws_delivery.add(ws)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.ERROR:
                print("WS error:", ws.exception())
    finally:
        connected_ws_delivery.remove(ws)
    return ws

async def websocket_handler_order(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connected_ws_order.add(ws)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.ERROR:
                print("WS error:", ws.exception())
    finally:
        connected_ws_order.remove(ws)
    return ws

async def broadcast_state():
    while should_run:
        msg = json.dumps({
            "state": current_state,
            "order": list(current_order.values())
        })
        if connected_ws_delivery:
            await asyncio.gather(*[ws.send_str(msg) for ws in connected_ws_delivery])
        if connected_ws_order:
            await asyncio.gather(*[ws.send_str(msg) for ws in connected_ws_order])
        await asyncio.sleep(0.1)

# ==================== main ====================
def handle_shutdown(signum, frame):
    global should_run
    print("\nShutdown signal received...")
    should_run = False
    time.sleep(1)
    os._exit(0)

async def start_background_tasks(app):
    app['broadcast_task'] = asyncio.create_task(broadcast_state())

async def cleanup_background_tasks(app):
    app['broadcast_task'].cancel()
    await app['broadcast_task']

def main():
    if len(sys.argv)!=2:
        print(f"Usage: {sys.argv[0]} <config_path>")
        return 1
    config_path = sys.argv[1]
    client = hakosim.MultirotorClient(config_path,"Drone")
    client.confirmConnection()
    client.enableApiControl(True)
    client.armDisarm(True)

    threading.Thread(target=lambda: [debug_pos(client) or time.sleep(0.1) for _ in iter(int,1)], daemon=True).start()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    app = web.Application()
    app["client"] = client

    app.router.add_get('/Delivery', index_delivery)
    app.router.add_get('/Order', index_order)
    app.router.add_get('/Delivery/ws', websocket_handler_delivery)
    app.router.add_get('/Order/ws', websocket_handler_order)
    app.router.add_post('/register_order', register_order)
    app.router.add_post('/start_delivery', start_delivery)

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    web.run_app(app, host='0.0.0.0', port=5000)

if __name__=='__main__':
    sys.exit(main())
