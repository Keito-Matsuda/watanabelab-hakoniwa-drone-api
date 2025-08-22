#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, time, threading, signal, asyncio, json, uuid, heapq, math
from aiohttp import web
import libs.hakosim as hakosim

DEFAULT_SPEED = 1.5
DEFAULT_HEIGHT = 0.5

# ==================== Drone操作 ====================
class DroneController:
    def __init__(self, client):
        self.client = client
        self.state = {
            "x": 0, "y": 0, "z": 0,
            "roll": 0, "pitch": 0, "yaw": 0,
            "status": "待機中",
            "eta": None
        }
        threading.Thread(target=self._state_loop, daemon=True).start()

    def _state_loop(self):
        while True:
            self.debug_pos()
            time.sleep(0.1)

    def debug_pos(self):
        pose = self.client.simGetVehiclePose()
        roll, pitch, yaw = hakosim.hakosim_types.Quaternionr.quaternion_to_euler(pose.orientation)
        self.state.update({
            "x": pose.position.x_val,
            "y": pose.position.y_val,
            "z": pose.position.z_val,
            "roll": roll, "pitch": pitch, "yaw": yaw
        })

    def update_eta(self, target_pos):
        dx = target_pos['x'] - self.state['x']
        dy = target_pos['y'] - self.state['y']
        dz = target_pos.get('z', DEFAULT_HEIGHT) - self.state['z']
        distance = (dx**2 + dy**2 + dz**2) ** 0.5
        eta_sec = distance / DEFAULT_SPEED
        if self.state.get('eta') is None or eta_sec < self.state['eta']:
            self.state['eta'] = eta_sec

    # ==================== JSON読み込み（ノード辞書） ====================
    def load_checkpoints(self, json_path):
        base_dir = os.path.dirname(__file__)
        full_path = os.path.join(base_dir, json_path)
        with open(full_path, "r", encoding="utf-8") as f:
            raw_points = json.load(f)

        # ノード辞書：id -> {pos, connect}
        nodes = {p["id"]: {"pos": p["pos"], "connect": p["connect"]} for p in raw_points}
        return nodes

    # ==================== 距離計算 ====================
    @staticmethod
    def distance(a, b):
        dx = a['x'] - b['x']
        dy = a['y'] - b['y']
        dz = a.get('z', DEFAULT_HEIGHT) - b.get('z', DEFAULT_HEIGHT)
        return (dx**2 + dy**2 + dz**2) ** 0.5

    # ==================== A*探索 ====================
    def astar(self, nodes, start_id, goal_id):
        open_set = []
        heapq.heappush(open_set, (0, start_id))
        came_from = {}
        g_score = {nid: float('inf') for nid in nodes}
        g_score[start_id] = 0
        f_score = {nid: float('inf') for nid in nodes}
        f_score[start_id] = self.distance(nodes[start_id]['pos'], nodes[goal_id]['pos'])

        while open_set:
            _, current = heapq.heappop(open_set)
            if current == goal_id:
                # 経路復元
                path = [current]
                while current in came_from:
                    current = came_from[current]
                    path.append(current)
                return path[::-1]  # スタート→ゴール
            for neighbor in nodes[current]['connect']:
                if neighbor not in nodes:
                    continue
                tentative_g = g_score[current] + self.distance(nodes[current]['pos'], nodes[neighbor]['pos'])
                if tentative_g < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    f_score[neighbor] = tentative_g + self.distance(nodes[neighbor]['pos'], nodes[goal_id]['pos'])
                    heapq.heappush(open_set, (f_score[neighbor], neighbor))
        return None

    # ==================== 複数チェックポイント配達 ====================
    def transport_multi(self, baggage_pos, checkpoints, order_id, order_manager):
        self.state["status"] = "配達準備中"
        self.client.takeoff(DEFAULT_HEIGHT)
        time.sleep(1)

        # 荷物取得
        self.client.moveToPosition(baggage_pos['x'], baggage_pos['y'], DEFAULT_HEIGHT, DEFAULT_SPEED)
        self.client.grab_baggage(True)
        time.sleep(2)

        self.state["status"] = "配達開始"

        # JSONロードしてノード辞書取得
        nodes = self.load_checkpoints("checkPoints.json")

        # ゴールID
        goal_id = order_manager.get_destination_id(order_id)

        # スタート固定 P0
        start_id = "P0"

        # A*で経路決定
        path_ids = self.astar(nodes, start_id, goal_id)
        


        if not path_ids:
            print("経路が見つかりません", flush=True)
            self.state["status"] = "待機中"
            return

        print("----航行経路決定----", flush=True)
        print(path_ids, flush=True)

        # ETA更新用スレッド
        stop_eta = False
        def eta_loop():
            while not stop_eta:
                target = nodes[goal_id]['pos']
                self.update_eta(target)
                time.sleep(0.2)

        t = threading.Thread(target=eta_loop, daemon=True)
        t.start()

        visited = [baggage_pos]  # 帰還用

        # 経路順に移動
        for nid in path_ids:
            target = nodes[nid]['pos']
            target_z = target.get('z', DEFAULT_HEIGHT)
            self.client.moveToPosition(target['x'], target['y'], target_z, DEFAULT_SPEED)
            time.sleep(1)
            visited.append(target)

        # 配達完了
        self.client.grab_baggage(False)
        self.state["status"] = "配達完了"
        order_manager.complete_order(order_id)
        time.sleep(2)

        stop_eta = True
        t.join()
        self.state["eta"] = None

        # 帰路
        self.state["status"] = "帰還中"
        for pos in reversed(visited[:-1]):
            pos_z = pos.get('z', DEFAULT_HEIGHT)
            self.client.moveToPosition(pos['x'], pos['y'], pos_z, DEFAULT_SPEED)
            time.sleep(1)

        self.client.moveToPosition(0, 0, DEFAULT_HEIGHT, DEFAULT_SPEED)
        self.state["status"] = "待機中"
        self.client.land()

# ==================== 注文管理 ====================
class OrderManager:
    def __init__(self):
        self.orders = {}

    def register_order(self, data):
        order_id = str(uuid.uuid4())
        self.orders[order_id] = {
            "id": order_id,
            "omurice": int(data.get("omurice", 0)),
            "hamburg": int(data.get("hamburg", 0)),
            "onigiri": int(data.get("onigiri", 0)),
            "point": data["point"]  # 配達先番号のみ
        }
        return order_id

    def complete_order(self, order_id):
        if order_id in self.orders:
            self.orders.pop(order_id)

    def list_orders(self):
        return list(self.orders.values())

    def get_destination_id(self, order_id):
        order = self.orders.get(order_id)
        if not order:
            return None
        return order.get("point", {}).get("dest_id")

# ==================== Webサーバ ====================
class DeliveryServer:
    def __init__(self, drone: DroneController, order_manager: OrderManager):
        self.drone = drone
        self.orders = order_manager
        self.should_run = True
        self.connected_ws_delivery = set()
        self.connected_ws_order = set()

    async def index_delivery(self, request):
        filepath = os.path.join(os.path.dirname(__file__), './Web/delivery.html')
        return web.FileResponse(filepath)

    async def index_order(self, request):
        filepath = os.path.join(os.path.dirname(__file__), './Web/order.html')
        return web.FileResponse(filepath)

    async def register_order(self, request):
        data = await request.json()
        point = data.get("point")
        if not point:
            return web.json_response({"error": "Invalid point"}, status=400)

        order_id = self.orders.register_order(data)
        return web.json_response({"result": "order registered", "order_id": order_id})

    async def start_delivery(self, request):
        data = await request.json()
        order_id = data.get("order_id")
        if not order_id:
            return web.json_response({"error": "Invalid request"}, status=400)

        checkpoints = self.drone.load_checkpoints("checkPoints.json")

        threading.Thread(
            target=self.drone.transport_multi,
            args=({"x": 2, "y": 0, "z": DEFAULT_HEIGHT}, checkpoints, order_id, self.orders),
            daemon=True
        ).start()

        return web.json_response({"result": "delivery started"})

    async def websocket_handler_delivery(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connected_ws_delivery.add(ws)
        print("Delivery WebSocket接続確立", flush=True)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.ERROR:
                    print("WS error:", ws.exception())
        finally:
            self.connected_ws_delivery.remove(ws)
            print("Delivery WebSocket切断", flush=True)
        return ws

    async def websocket_handler_order(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connected_ws_order.add(ws)
        print("Order WebSocket接続確立", flush=True)
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.ERROR:
                    print("WS error:", ws.exception())
        finally:
            self.connected_ws_order.remove(ws)
            print("Order WebSocket切断", flush=True)
        return ws

    async def broadcast_state(self):
        while self.should_run:
            msg = json.dumps({
                "state": self.drone.state,
                "order": self.orders.list_orders()
            })
            if self.connected_ws_delivery:
                await asyncio.gather(*[ws.send_str(msg) for ws in self.connected_ws_delivery])
            if self.connected_ws_order:
                await asyncio.gather(*[ws.send_str(msg) for ws in self.connected_ws_order])
            await asyncio.sleep(0.1)

    async def start_background_tasks(self, app):
        app['broadcast_task'] = asyncio.create_task(self.broadcast_state())

    async def cleanup_background_tasks(self, app):
        app['broadcast_task'].cancel()
        await app['broadcast_task']

    def create_app(self):
        app = web.Application()
        app.router.add_get('/Delivery', self.index_delivery)
        app.router.add_get('/Order', self.index_order)
        app.router.add_get('/Delivery/ws', self.websocket_handler_delivery)
        app.router.add_get('/Order/ws', self.websocket_handler_order)
        app.router.add_post('/register_order', self.register_order)
        app.router.add_post('/start_delivery', self.start_delivery)
        app.on_startup.append(self.start_background_tasks)
        app.on_cleanup.append(self.cleanup_background_tasks)
        return app

# ==================== main ====================
def handle_shutdown(signum, frame):
    print("\nShutdown signal received...")
    time.sleep(1)
    os._exit(0)

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <config_path>")
        return 1

    config_path = sys.argv[1]
    client = hakosim.MultirotorClient(config_path, "Drone")
    client.confirmConnection()
    client.enableApiControl(True)
    client.armDisarm(True)

    drone = DroneController(client)
    orders = OrderManager()
    server = DeliveryServer(drone, orders)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    app = server.create_app()
    web.run_app(app, host='0.0.0.0', port=5000)

if __name__ == '__main__':
    sys.exit(main())
