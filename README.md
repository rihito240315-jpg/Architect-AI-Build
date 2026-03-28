💻 PC Architect Engine: Autonomous AI Buyer
Claude 3.5 Sonnet と Playwright を駆使し、予算と用途に合わせて実勢価格に基づいたPC構成を自律的に作成するAIエージェントです。

🚀 プロジェクトの概要
このアプリケーションは、単なるパーツ提案ツールではありません。AIが実際にブラウザを操作して「価格.com」や「Amazon」を巡回し、**「今、その瞬間に買える最安値」**を積み上げて合計予算内に収める「自律型バイヤー」をコンセプトにしています。

✨ 注目の機能
リアルタイム・ライブ・ブラウジング: データベース上の古い価格ではなく、実行時の最新価格を取得。

インテリジェント・バジェット管理: 予算を超過した場合、AIがパーツのグレードを自ら判断して落とし、再計算を行う思考ループを搭載。

高機能待機列（Queue）システム: Flask-SocketIOにより、サーバー負荷を抑えつつ、待機中のユーザーへリアルタイムに「あと何人」という進捗を通知。

サイバーパンク風UI/UX: 進行状況が視覚的に伝わるプログレスバーと、パーツが確定するたびに表示されるリベールカード。

🛠 技術スタック
Backend: Python 3.10+, Flask, Flask-SocketIO

AI Engine: AWS Bedrock (Anthropic Claude 3.5 Sonnet)

Browser Automation: Playwright (Chromium)

Frontend: Tailwind CSS, JavaScript, Socket.io

📦 セットアップ方法
1. クローンとライブラリのインストール
Bash
git clone https://github.com/YOUR_USERNAME/pc-architect-engine.git
cd pc-architect-engine
pip install -r requirements.txt
playwright install chromium
2. 環境変数の設定
プロジェクト直下に .env ファイルを作成し、AWSの認証情報を記載してください。

コード スニペット
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
3. 実行
Bash
python main.py
起動後、ブラウザで http://localhost:5000 を開きます。

🚦 キュー管理について
本システムはブラウザを実働させるため、サーバーリソース保護の観点から同時実行数を制限しています。2人目以降のユーザーにはリアルタイムに待ち順位が表示され、前のセッションが終了次第、自動的に構成が開始されます。