"""
eel → Flask-SocketIO 書き換え版 (セキュリティ強化統合版)
---------------------------------
変更点まとめ:
  - `eel.init` / `eel.start`        → Flask + SocketIO のサーバー起動に置換
  - `@eel.expose`                    → `@socketio.on(...)` イベントハンドラに置換
  - `eel.add_log(...)`               → `socketio.emit("add_log", ...)` に置換
  - `eel.add_reveal_card(...)`       → `socketio.emit("add_reveal_card", ...)` に置換
  - `eel.update_status(...)`         → `socketio.emit("update_status", ...)` に置換

セキュリティ追加対策:
  - 【対策1】IPアドレスベースの利用回数制限 (1日 DAILY_LIMIT 回まで)
  - 【対策2】同時実行数の制限 (MAX_CONCURRENT) + キュー待ち + 待ち人数リアルタイム通知
  - 【対策3】APIキー未設定時のフォールバック処理

フロントエンド側 (web/index.html) で必要な変更:
  1. `<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>` を追加
  2. eel.js の読み込みを削除
  3. JS 側の呼び出しを以下のように変更:
       - eel.receive_user_instruction(text)
           → socket.emit("receive_user_instruction", { text })
       - eel.update_locked_parts(categories)
           → socket.emit("update_locked_parts", { categories })
       - eel.start_build_sequence(budget, purpose)
           → socket.emit("start_build_sequence", { budget, purpose })
  4. eel のコールバック受信を以下のように変更:
       - eel.add_log(...)          → socket.on("add_log", ...)
       - eel.add_reveal_card(...)  → socket.on("add_reveal_card", ...)
       - eel.update_status(...)    → socket.on("update_status", ...)
  5. キュー関連イベントを追加:
       - socket.on("queue_waiting", ({ position, total }) => { ... })
       - socket.on("queue_position_update", ({ position, total, message }) => { ... })
       - socket.on("queue_started", ({ message }) => { ... })
  6. ブラウザを自動起動したい場合は webbrowser.open("http://localhost:5000") を使用
"""

import asyncio
import threading
import os
import re
from collections import defaultdict
from datetime import date

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit

try:
    from browser_driver import BrowserDriver
    from ai_engine import AIEngine
except ImportError:
    print("必要なファイルが足りません。")
    exit()


# ------------------------------------------------------------------ #
#  Flask / SocketIO セットアップ
# ------------------------------------------------------------------ #
base_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(base_dir, "web"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ------------------------------------------------------------------ #
#  【対策3】APIキー保護 ─ 起動時に検証
# ------------------------------------------------------------------ #
def _validate_api_key() -> bool:
    """
    AIEngine が必要とする API キーが環境変数に存在するか確認する。
    キーが未設定の場合はフォールバック処理を行い False を返す。
    """
    key = os.environ.get("BEDROCK_API_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    if not key:
        print("=" * 60)
        print("[ERROR] 必要な API キーが環境変数に設定されていません。")
        print("  BEDROCK_API_KEY または AWS_ACCESS_KEY_ID を設定してください。")
        print("  例: export AWS_ACCESS_KEY_ID=your_key_here")
        print("=" * 60)
        return False
    return True

API_KEY_AVAILABLE = _validate_api_key()


# ------------------------------------------------------------------ #
#  【対策1】IP ベースの利用回数制限
# ------------------------------------------------------------------ #
DAILY_LIMIT = 3          # 1 IP あたり 1 日の最大ビルド開始回数
RATE_LIMIT_LOCK = threading.Lock()

# { "192.168.0.1": {"date": date(2025,1,1), "count": 2} }
_ip_usage: dict[str, dict] = defaultdict(lambda: {"date": None, "count": 0})


def _get_client_ip() -> str:
    """
    リバースプロキシ (Nginx 等) 経由でも正しい IP を取得する。
    X-Forwarded-For が信頼できる環境では先頭アドレスを使用。
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _check_and_increment_rate_limit(ip: str) -> tuple[bool, int]:
    """
    利用回数を確認し、上限以下なら +1 して (True, remaining) を返す。
    上限超過の場合は (False, 0) を返す。
    """
    today = date.today()
    with RATE_LIMIT_LOCK:
        record = _ip_usage[ip]
        if record["date"] != today:
            record["date"] = today
            record["count"] = 0
        if record["count"] >= DAILY_LIMIT:
            return False, 0
        record["count"] += 1
        remaining = DAILY_LIMIT - record["count"]
        return True, remaining


# ------------------------------------------------------------------ #
#  【対策2】同時実行数の制限 + キュー管理
# ------------------------------------------------------------------ #
MAX_CONCURRENT = 1       # 同時に AI を動かせるセッション数

_active_sessions: set[str] = set()      # 現在実行中の sid
_waiting_queue: list[tuple] = []        # [(sid, budget, purpose, ip), ...]
_session_lock = threading.Lock()


def _try_start_or_enqueue(sid: str, budget, purpose: str, ip: str) -> bool:
    """
    空きスロットがあれば即時実行、なければキューに追加する。
    Returns True if started immediately, False if enqueued.
    """
    with _session_lock:
        if len(_active_sessions) < MAX_CONCURRENT:
            _active_sessions.add(sid)
            return True
        else:
            _waiting_queue.append((sid, budget, purpose, ip))
            return False


def _release_session(sid: str):
    """セッション終了時に呼び出す。次のキュー待ちを起動し、残り全員に位置を通知。"""
    with _session_lock:
        _active_sessions.discard(sid)
        next_item = _waiting_queue.pop(0) if _waiting_queue else None
        remaining_queue = list(_waiting_queue)

    if next_item:
        next_sid, next_budget, next_purpose, next_ip = next_item
        socketio.emit(
            "queue_started",
            {"message": "順番が来ました。構成を開始します。"},
            to=next_sid,
        )
        with _session_lock:
            _active_sessions.add(next_sid)
        threading.Thread(
            target=run_async_logic,
            args=(next_budget, next_purpose, next_sid),
            daemon=True,
        ).start()

    # 残りの待機者全員に最新位置を broadcast
    _broadcast_queue_positions(remaining_queue)


def _broadcast_queue_positions(queue_snapshot: list):
    """
    waiting_queue の現在スナップショットを受け取り、
    各 sid に「あなたは現在 N 番目です」を個別送信する。
    """
    for pos, (sid, *_) in enumerate(queue_snapshot, start=1):
        socketio.emit(
            "queue_position_update",
            {
                "position": pos,
                "total": len(queue_snapshot),
                "message": f"あと {pos} 人で順番です",
            },
            to=sid,
        )


# ------------------------------------------------------------------ #
#  グローバル状態
# ------------------------------------------------------------------ #
driver = BrowserDriver()

# AIEngine の初期化 ─ キーがなければ None で置換
if API_KEY_AVAILABLE:
    ai = AIEngine()
else:
    ai = None

current_user_instructions: list[str] = []
locked_parts: list[str] = []
_lock = threading.Lock()


# ------------------------------------------------------------------ #
#  管理者モード / 緊急停止
# ------------------------------------------------------------------ #
ADMIN_SECRET = "jinbee"       # ← 必要に応じて変更してください
is_admin: dict[str, bool] = {}
force_stop_event = asyncio.Event()

COMPLETION_KEYWORDS = [
    "完了", "以上です", "終了", "以上が最終", "最終的な構成",
    "予算内に収まりました", "おめでとうございます", "構成完了"
]

MAX_EMPTY_RESPONSES = 3


# ------------------------------------------------------------------ #
#  静的ファイル配信（eel.init / eel.start の代替）
# ------------------------------------------------------------------ #
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


# ------------------------------------------------------------------ #
#  eel.expose → socketio.on イベントハンドラ
# ------------------------------------------------------------------ #

@socketio.on("receive_user_instruction")
def receive_user_instruction(data):
    text = data.get("text", "").strip()
    sid = request.sid

    # ── 管理者認証 ──────────────────────────────────────────────────
    if text == ADMIN_SECRET:
        is_admin[sid] = True
        emit("admin_auth_success", {"message": "管理者権限が付与されました"})
        print(f"[ADMIN] 認証成功: sid={sid}")
        return

    # ── 緊急停止 ────────────────────────────────────────────────────
    if text.upper() == "STOP":
        if is_admin.get(sid):
            force_stop_event.set()
            emit("admin_auth_success", {"message": "⛔ 緊急停止を実行しました"})
            print(f"[ADMIN] 緊急停止: sid={sid}")
        else:
            emit("add_log", ["STOP コマンドは管理者のみ使用できます", "error"])
        return

    # ── 通常の指示 ──────────────────────────────────────────────────
    with _lock:
        current_user_instructions.append(text)
    print(f"User Request Received: {text}")


@socketio.on("update_locked_parts")
def update_locked_parts(data):
    global locked_parts
    categories = data.get("categories", [])
    with _lock:
        locked_parts = categories
    print(f"Locked Categories: {categories}")


@socketio.on("update_status")
def update_status(data):
    # フロントから受け取るケースがあれば処理（元コードと同様に空実装）
    pass


@socketio.on("start_build_sequence")
def start_build_sequence(data):
    sid = request.sid
    ip = _get_client_ip()

    # ── 【対策3】APIキー確認 ─────────────────────────────────────
    if not API_KEY_AVAILABLE or ai is None:
        emit("add_log", [
            "サーバーの設定エラーにより AI が利用できません。管理者にお問い合わせください。",
            "error",
        ])
        return

    # ── 【対策1】IP レート制限チェック ───────────────────────────
    allowed, remaining = _check_and_increment_rate_limit(ip)
    if not allowed:
        emit("add_log", [
            f"本日の利用上限（{DAILY_LIMIT}回）に達しました。明日またお試しください。",
            "error",
        ])
        return

    budget = data.get("budget")
    purpose = data.get("purpose")
    force_stop_event.clear()

    emit("add_log", [
        f"ビルドを開始します（本日の残り利用回数: {remaining}回）",
        "system",
    ])

    # ── 【対策2】同時実行制限 / キュー ───────────────────────────
    started = _try_start_or_enqueue(sid, budget, purpose, ip)
    if started:
        threading.Thread(
            target=run_async_logic, args=(budget, purpose, sid), daemon=True
        ).start()
    else:
        with _session_lock:
            queue_snapshot = list(_waiting_queue)
        my_pos = len(queue_snapshot)
        emit("add_log", [
            f"現在他のユーザーが構成中です。キューで待機しています（あと {my_pos} 人）",
            "system",
        ])
        emit("queue_waiting", {"position": my_pos, "total": my_pos})
        # 自分の追加で総数が変わるため、前の待機者にも再通知
        _broadcast_queue_positions(queue_snapshot)


@socketio.on("disconnect")
def on_disconnect():
    """切断時にキューから除外して残り全員に再通知する。"""
    sid = request.sid
    removed = False
    with _session_lock:
        before = len(_waiting_queue)
        _waiting_queue[:] = [item for item in _waiting_queue if item[0] != sid]
        removed = len(_waiting_queue) < before
        queue_snapshot = list(_waiting_queue)

    if removed:
        print(f"[QUEUE] 切断によりキューから除外: sid={sid}")
        _broadcast_queue_positions(queue_snapshot)

    # 実行中セッションが切断した場合も解放
    if sid in _active_sessions:
        _release_session(sid)


@socketio.on("start_screencast")
def start_screencast():
    """フロントから呼ばれたらCDPスクリーンキャストを開始"""
    threading.Thread(target=_run_screencast, daemon=True).start()


@socketio.on("stop_screencast")
def stop_screencast():
    driver.stop_screencast()


def _run_screencast():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(driver.start_screencast(socketio))


# ------------------------------------------------------------------ #
#  eel.xxx(...) 呼び出し → socketio.emit(...) ラッパー
#  ※ バックグラウンドスレッドから呼ぶため socketio.emit を直接使用
# ------------------------------------------------------------------ #

def _emit(event, *args):
    """スレッドセーフな emit ラッパー（eel.xxx の代替）"""
    socketio.emit(event, list(args))


def add_log(message, level="info"):
    """eel.add_log(message, level) の代替"""
    _emit("add_log", message, level)


def add_reveal_card(category, name, price):
    """eel.add_reveal_card(category, name, price) の代替"""
    _emit("add_reveal_card", category, name, price)


def update_status_emit(status_text):
    """eel.update_status(status_text) の代替"""
    _emit("update_status", status_text)


# ------------------------------------------------------------------ #
#  非同期ループ（sid を引数に追加してセッション解放に使用）
# ------------------------------------------------------------------ #

def run_async_logic(budget, purpose, sid: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_ai_loop(budget, purpose))
    finally:
        # セッション終了時に必ず解放し、次のキュー待ちを起動
        _release_session(sid)


async def main_ai_loop(budget, purpose):
    try:
        add_log("ARCHITECT ENGINE: リアルタイム構成修正モード起動", "system")
        await driver.init_browser(headless=True)

        messages = [{"role": "user", "content": [{"text": f"予算 {budget}円 でPC構成を作成してください。"}]}]

        base_system_prompt = (
            f"あなたは【価格.com】と【Amazon】を使い分ける超一流のPCパーツバイヤーです。\n"
            f"現在の予算制限: {budget}円 / 用途: {purpose}\n\n"
            "【検索ルール（最重要）】\n"
            "1. パーツを探す際は必ず search ツールを使ってください。\n"
            "2. search ツールは自動的に価格.comを優先して検索します。\n"
            "3. 価格.comに結果がない場合のみ、Amazonの検索URLが返されます。その場合はそのURLをvisitしてください。\n"
            "4. visitするURLは【価格.com (kakaku.com)】か【Amazon (amazon.co.jp)】のみ許可します。それ以外は絶対にvisitしないでください。\n"
            "5. 価格.comで見つかった場合は価格.comの商品ページURLをvisitして最安値を確認してください。\n"
            "6. 価格.comで見つからない場合は返却されたAmazonのURLをvisitして価格を確認してください。\n\n"
            "【動的な構成変更ルール】\n"
            "1. 常に合計金額を計算しながらパーツを選定してください。\n"
            "2. 予算を超えると判断した場合は以前選んだパーツを安価なモデルへランクダウンさせ、再度 mark_part で上書きしてください。\n"
            "3. 検索時は型番のみで検索し、visitで最新価格を把握すること。\n"
            f"4. 最終的に予算 {budget}円 以内に収まった完璧なリストを完成させてください。\n"
            "5. 自分のデータベース上の価格は絶対に使わないでください。必ずvisitして最新価格を確認してください。\n"
            "6. 必要なパーツ: CPU / GPU / マザーボード / メモリ(DDR5 シングルチャネルは禁止　例16GBx2のようにしてください。) / ストレージ(NVMe SSD PCIe 4.0以上) / 電源 / ケース / CPUクーラー(簡易水冷推奨) / GPUホルダー\n"
            "7. 色指定やブランド指定がある場合は必ず守ってください。\n"
            "8. mark_part の前に必ずPythonコードで合計金額を計算してください。頭の中だけで計算することは禁止です。\n\n"
            "【仮定禁止の鉄則】\n"
            "1. 価格を仮定することは厳禁です。必ずvisitしたページの実際の販売価格をmark_partに入力してください。\n"
            "2. 予算を1円でも超えたら即座に下位モデルへランクダウンし、mark_partで上書きしてください。\n\n"
            "【2026年3月のリアルな相場観】\n"
            "GPU: RTX 5080 (約20万) / RTX 5070 Ti (約16万)\n"
            "CPU: Core Ultra 7 265K (約7.5万) / Ryzen 7 9800X3D (約8.5万)\n"
            "メモリ: DDR5 32GB (約1.8万〜)\n\n"
            "【予算計算の方法】\n"
            "Pythonコードを書いて各パーツの価格を合計し、予算内かを必ず確認してください。\n"
            "何よりも予算が守られることが最優先です。予算を超える選択は絶対にしないでください。決して近い数字でも予算を下回るまで考え抜いてください。\n"
        )

        empty_response_count = 0
        is_completed = False
        processed_instruction_count = 0

        for i in range(100000):

            # ── 緊急停止チェック ─────────────────────────────────────
            if force_stop_event.is_set():
                add_log("⛔ 緊急停止が実行されました。ループを終了します。", "error")
                break

            # ---- 完了後は新しい指示が来るまで待機 ----
            if is_completed:
                with _lock:
                    new_instructions = current_user_instructions[processed_instruction_count:]

                if not new_instructions:
                    await asyncio.sleep(1.0)
                    continue

                instruction_text = " / ".join(new_instructions)
                add_log(f"指示をAIに送信: {instruction_text}", "system")
                messages.append({
                    "role": "user",
                    "content": [{"text": (
                        f"ユーザーから追加指示が来ました: 「{instruction_text}」\n\n"
                        "この指示に従って、該当するパーツを再検索・再選定し、mark_partで上書きしてください。"
                        "必ずsearchとvisitを使って実際の価格を確認してから変更してください。"
                    )}]
                })

                with _lock:
                    processed_instruction_count = len(current_user_instructions)

                is_completed = False
                update_status_emit(f"STEP {i+1}: 指示に基づき更新中...")
                await asyncio.sleep(2.0)

            else:
                await asyncio.sleep(2.0)
                update_status_emit(f"STEP {i+1}: 構成調整中...")

            with _lock:
                locked_snapshot = list(locked_parts)

            dynamic_info = ""
            if locked_snapshot:
                dynamic_info += f"\n【現在ロック中（変更不可）】: {', '.join(locked_snapshot)}"

            current_prompt = base_system_prompt + dynamic_info

            res = None
            for attempt in range(5):
                try:
                    res = await ai.ask_ai(messages, current_prompt)
                    break
                except Exception as e:
                    err_str = str(e)
                    if "ThrottlingException" in err_str or "Too many tokens" in err_str:
                        wait_sec = 30 * (attempt + 1)
                        add_log(f"API制限中... {wait_sec}秒待機 (試行 {attempt+1}/5)", "error")
                        await asyncio.sleep(wait_sec)
                    else:
                        raise

            if res is None:
                add_log("APIリトライ上限に達しました。終了します。", "error")
                break

            # ── 緊急停止チェック（重い処理の直後）──────────────────
            if force_stop_event.is_set():
                add_log("⛔ 緊急停止 (ask_ai 完了直後)。ループを終了します。", "error")
                break

            res_content = res.get("content", [])

            if not res_content:
                empty_response_count += 1
                add_log(f"AIから空のレスポンス ({empty_response_count}/{MAX_EMPTY_RESPONSES})", "error")
                if empty_response_count >= MAX_EMPTY_RESPONSES:
                    add_log("構成が完了しました。追加の指示をお待ちしています。", "success")
                    update_status_emit("待機中 - 追加指示をどうぞ")
                    is_completed = True
                    empty_response_count = 0
                continue

            empty_response_count = 0
            messages.append(res)

            tool_results = []
            full_text = ""

            for content in res_content:
                if "text" in content:
                    add_log(content["text"], "ai")
                    full_text += content["text"]

                if "toolUse" in content:
                    tool = content["toolUse"]
                    t_name = tool["name"]
                    t_input = tool["input"]
                    t_id = tool["toolUseId"]
                    res_text = ""

                    if t_name == "search":
                        clean_query = re.sub(r'(価格|値段|の|安い|おすすめ|調査|検索)', '', t_input["query"]).strip()
                        encoded_query = clean_query.replace(' ', '+')
                        kakaku_url = f"https://kakaku.com/search_results/?query={encoded_query}"
                        amazon_url = f"https://www.amazon.co.jp/s?k={encoded_query}"

                        add_log(f"価格.com で検索中: {clean_query}", "tool")
                        kakaku_result = await driver.fetch_page_text(kakaku_url)

                        # ── 緊急停止チェック（fetch直後）──────────────
                        if force_stop_event.is_set():
                            add_log("⛔ 緊急停止 (fetch直後)。ループを終了します。", "error")
                            return

                        has_result = (
                            kakaku_result
                            and len(kakaku_result.strip()) >= 100
                            and "該当する商品が見つかりません" not in kakaku_result
                            and "0件" not in kakaku_result
                        )

                        if has_result:
                            add_log(f"価格.com で結果取得: {clean_query}", "tool")
                            res_text = (
                                f"【価格.com 検索結果】{clean_query}\n\n"
                                f"{kakaku_result}\n\n"
                                "上記リストから最安値の商品ページURLをvisitして正確な価格を確認してください。"
                                "visitするURLは価格.comのURLのみにしてください。"
                            )
                        else:
                            add_log(f"価格.com に結果なし → Amazon に切替: {clean_query}", "tool")
                            res_text = (
                                f"価格.com に '{clean_query}' の結果がありませんでした。\n"
                                f"次のAmazon URLをvisitして価格を確認してください:\n{amazon_url}\n\n"
                                "visitするURLはこのAmazon URLのみにしてください。"
                            )

                    elif t_name == "visit":
                        url = t_input["url"]
                        if "kakaku.com" not in url and "amazon.co.jp" not in url:
                            add_log(f"アクセス拒否 (許可外サイト): {url}", "error")
                            res_text = (
                                "Error: このURLへのアクセスは許可されていません。"
                                "visitできるのは kakaku.com または amazon.co.jp のURLのみです。"
                                "searchツールを使って正しいURLを取得してください。"
                            )
                        else:
                            site_name = "価格.com" if "kakaku.com" in url else "Amazon"
                            add_log(f"{site_name} を解析中...", "tool")
                            res_text = await driver.fetch_page_text(url)
                            # ── 緊急停止チェック（visit fetch直後）──────
                            if force_stop_event.is_set():
                                add_log("⛔ 緊急停止 (visit fetch直後)。ループを終了します。", "error")
                                return

                    elif t_name == "mark_part":
                        cat = t_input.get("category", "パーツ")
                        with _lock:
                            is_locked = cat in locked_parts

                        if is_locked:
                            add_log(f"{cat} は固定されているためAIの変更を拒否", "error")
                            res_text = (
                                f"Error: {cat} is currently LOCKED. "
                                "You cannot change this part. Adjust other parts instead."
                            )
                        else:
                            name = t_input.get("name", "製品名不明")
                            price = t_input.get("price", 0)
                            add_reveal_card(cat, name, price)
                            add_log(f"{cat} 確定: {name} (¥{price:,})", "success")
                            res_text = "Confirmed."

                    tool_results.append({
                        "toolResult": {
                            "toolUseId": t_id,
                            "content": [{"text": res_text}]
                        }
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                if any(k in full_text for k in COMPLETION_KEYWORDS):
                    add_log("構成が完了しました。追加の指示をお待ちしています。", "success")
                    update_status_emit("待機中 - 追加指示をどうぞ")
                    is_completed = True
                    with _lock:
                        processed_instruction_count = len(current_user_instructions)

    except Exception as e:
        add_log(f"エラー: {str(e)}", "error")
    finally:
        await driver.close()


# ------------------------------------------------------------------ #
#  サーバー起動（eel.start の代替）
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    if not API_KEY_AVAILABLE:
        print("[WARN] APIキー未設定のままサーバーを起動します。AIは利用不可の状態です。")
    # ブラウザを自動で開きたい場合は以下をアンコメント
    # import webbrowser
    # webbrowser.open("http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)


# ==================================================================== #
#  【フロントエンド追記案内】index.html の JS に以下を追加してください
# ==================================================================== #
#
#  // ── キュー待機バナー用 HTML 要素を追加 ──────────────────────────
#  // <div id="queue-banner" style="display:none">
#  //   <span id="queue-message"></span>
#  //   <div id="queue-progress-track">
#  //     <div id="queue-progress-bar"></div>
#  //   </div>
#  // </div>
#
#  // ── キューイベント受信 ────────────────────────────────────────────
#  socket.on("queue_waiting", ({ position, total }) => {
#    showQueueBanner(position, total);
#  });
#
#  socket.on("queue_position_update", ({ position, total, message }) => {
#    showQueueBanner(position, total);
#  });
#
#  socket.on("queue_started", ({ message }) => {
#    hideQueueBanner();
#    addLog(message, "system");
#  });
#
#  function showQueueBanner(position, total) {
#    const banner = document.getElementById("queue-banner");
#    const msgEl  = document.getElementById("queue-message");
#    const barEl  = document.getElementById("queue-progress-bar");
#    if (!banner) return;
#    msgEl.textContent = `⏳ あと ${position} 人で順番です（現在 ${total} 人待ち）`;
#    const pct = total > 1 ? Math.round(((total - position) / (total - 1)) * 90) : 0;
#    barEl.style.width = pct + "%";
#    banner.style.display = "block";
#  }
#
#  function hideQueueBanner() {
#    const banner = document.getElementById("queue-banner");
#    if (banner) banner.style.display = "none";
#  }
#
#  // ── 管理者認証成功 → 隠しデバッグパネルを表示 ──────────────────
#  socket.on("admin_auth_success", ([data]) => {
#    console.log("[ADMIN]", data.message);
#    const panel = document.getElementById("debug-panel");
#    if (panel) {
#      panel.style.display = "block";
#      panel.querySelector("#admin-status").textContent = data.message;
#    }
#    const stopBtn = document.getElementById("stop-btn");
#    if (stopBtn) stopBtn.disabled = false;
#  });
#
#  // STOPボタンの送信例
#  document.getElementById("stop-btn")?.addEventListener("click", () => {
#    socket.emit("receive_user_instruction", { text: "STOP" });
#  });
#
#  // HTML側に追加する要素の例:
#  // <div id="debug-panel" style="display:none; border:2px solid red; padding:8px;">
#  //   <span id="admin-status"></span>
#  //   <button id="stop-btn" disabled>⛔ 緊急停止</button>
#  // </div>
# ==================================================================== #