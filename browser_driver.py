"""
browser_driver.py — CDP スクリーンキャスト対応版
-------------------------------------------------
変更点:
  - start_screencast(socketio) : CDPでリアルタイム映像をSocketIOに流す
  - stop_screencast()          : キャストを止める
  - 既存の init_browser / fetch_page_text / close はそのまま
"""

import asyncio
from playwright.async_api import async_playwright


class BrowserDriver:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp_session = None
        self._screencast_active = False

    # ------------------------------------------------------------------ #
    #  ブラウザ起動
    # ------------------------------------------------------------------ #
    async def init_browser(self, headless: bool = False):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        self._page = await self._context.new_page()

    # ------------------------------------------------------------------ #
    #  ページ取得（既存処理）
    # ------------------------------------------------------------------ #
    async def fetch_page_text(self, url: str) -> str:
        if self._page is None:
            return "Error: ブラウザが初期化されていません"
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._page.wait_for_timeout(2000)
            text = await self._page.evaluate("() => document.body.innerText")
            return text[:8000]  # 長すぎる場合は切り捨て
        except Exception as e:
            return f"Error fetching {url}: {e}"

    # ------------------------------------------------------------------ #
    #  CDP スクリーンキャスト開始
    # ------------------------------------------------------------------ #
    async def start_screencast(self, socketio, quality: int = 40, max_width: int = 1280):
        """
        CDPのPage.screencastFrameイベントを使って
        JPEGフレームをbase64でSocketIOに流す。

        quality   : JPEG品質 (1-100)。低いほど帯域を節約できる
        max_width : リサイズ上限px
        """
        if self._page is None:
            return

        self._screencast_active = True

        # CDPセッションを開く
        self._cdp_session = await self._context.new_cdp_session(self._page)

        # フレームを受け取るコールバック
        async def on_frame(params):
            if not self._screencast_active:
                return

            # フレームをブラウザに受け取ったことを通知（必須）
            try:
                await self._cdp_session.send(
                    "Page.screencastFrameAck",
                    {"sessionId": params["sessionId"]}
                )
            except Exception:
                pass

            # base64 JPEGをそのままSocketIOで送信
            socketio.emit("browser_frame", {"img": params["data"]})

        self._cdp_session.on("Page.screencastFrame", on_frame)

        # スクリーンキャスト開始
        await self._cdp_session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": quality,
                "maxWidth": max_width,
                "maxHeight": 800,
                "everyNthFrame": 1,  # 毎フレーム取得
            }
        )

        # stop_screencast() が呼ばれるまで待機
        while self._screencast_active:
            await asyncio.sleep(0.1)

        # 停止
        try:
            await self._cdp_session.send("Page.stopScreencast")
            await self._cdp_session.detach()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  CDP スクリーンキャスト停止
    # ------------------------------------------------------------------ #
    def stop_screencast(self):
        """スレッドセーフに停止フラグを立てる"""
        self._screencast_active = False

    # ------------------------------------------------------------------ #
    #  ブラウザ終了
    # ------------------------------------------------------------------ #
    async def close(self):
        self.stop_screencast()
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        finally:
            self._browser = None
            self._playwright = None
            self._page = None
            self._cdp_session = None