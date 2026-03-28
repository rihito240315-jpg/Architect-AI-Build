import boto3
import os
from dotenv import load_dotenv

# プロジェクト起動時に .env を読み込む
load_dotenv()

class AIEngine:
    def __init__(self):
        self.model_id = "anthropic.claude-3-5-sonnet-20240620-v1:0"
        self.client = None
        
        # 起動時に環境変数をチェック（.env または サーバーの設定）
        ak = os.getenv("AWS_ACCESS_KEY_ID")
        sk = os.getenv("AWS_SECRET_ACCESS_KEY")
        
        if ak and sk:
            print("✅ AWS Keys found in environment. Initializing...")
            self.setup_client(ak, sk)

    def setup_client(self, access_key, secret_key):
        """AWSクライアントを初期化。成功ならTrueを返す。"""
        try:
            self.client = boto3.client(
                "bedrock-runtime",
                region_name="us-east-1",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key
            )
            return True
        except Exception as e:
            print(f"Setup Error: {e}")
            self.client = None
            return False

    def get_tools(self):
        # ... (ツール定義は変更なし) ...
        return [
            {
                "toolSpec": {
                    "name": "search",
                    "description": "Google検索URLを生成します。検索ワードに'価格'や'在庫'を含めてください。",
                    "inputSchema": {"json": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
                }
            },
            {
                "toolSpec": {
                    "name": "visit",
                    "description": "URLから最新の価格情報を読み取ります。Amazonや価格.comを推奨。",
                    "inputSchema": {"json": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}
                }
            },
            {
                "toolSpec": {
                    "name": "mark_part",
                    "description": "パーツを確定します。実際の販売価格以外は入力禁止。",
                    "inputSchema": {"json": {"type": "object", "properties": {
                        "category": {"type": "string"}, "name": {"type": "string"}, "price": {"type": "number"}
                    }, "required": ["category", "name", "price"]}}
                }
            }
        ]

    async def ask_ai(self, messages, system_prompt):
        """AIとの対話実行。"""
        if not self.client:
            return {
                "error": "API_KEY_NOT_SET", 
                "content": [{"text": "エラー：AWS APIキーが設定されていません。環境変数または設定を確認してください。"}]
            }

        try:
            response = self.client.converse(
                modelId=self.model_id,
                messages=messages,
                system=[{"text": system_prompt}],
                toolConfig={"tools": self.get_tools()}
            )
            return response["output"]["message"]
        except Exception as e:
            return {"error": str(e), "content": [{"text": f"通信エラーが発生しました: {str(e)}"}]}