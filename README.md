# Jev 汎用n択 Telegram bot

Jevがチャットのメッセージに対して適切な返答を選び、botは **設定された選択肢のいずれかをそのまま** 返信します。説明、確信度、選択肢に含まれない句読点などを付けません。Python 3.10以上で動作し、追加パッケージは不要です。

「好 / 不好 / 羡慕 / 不羡慕」は初期設定の一例です。選択肢を変更すれば、同じコードで別のn択botとして使えます。

## Jevに任せる判断

Jevへ渡す指示は次の1文だけです。

> チャットのメッセージへの返答として、提示された選択肢から最も適切なものを1つ選んでください。

選択肢ごとの意味、用途、判断基準、優先順位、期待回答の例は定義しません。キーワード分岐、中立時の既定回答、低確信度時の回答の差し替えもありません。意味の判断と返答の選択はJevが行い、コードは受信・API呼び出し・選択肢への完全一致の確認・送信を行います。

公式APIの`Choice`を使います。`criteria`はAPIが選択肢を受け取るフィールド名であり、各選択肢の値には`null`を指定して説明や判断基準を追加しません。

```json
{
  "model": "jev-latest",
  "state": "受信したチャットのメッセージ",
  "questions": {
    "reply": {
      "type": "choice",
      "instructions": "チャットのメッセージへの返答として、提示された選択肢から最も適切なものを1つ選んでください。",
      "criteria": {"好": null, "不好": null, "羡慕": null, "不羡慕": null}
    }
  }
}
```

`POST https://api.typesafe.ai/v1/systemone`へ送信し、`answers.reply.choice`の文字列をそのまま返信します。

## 初回設定と起動（Windows / PowerShell）

1. Telegramの公式[@BotFather](https://t.me/BotFather)に`/newbot`を送り、botを作成してtokenを取得します。
2. [TypeSafeのコンソール](https://console.typesafe.ai/)でJevのAPIキーを作成します。
3. このフォルダーで次を実行します。

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env`の2項目を実際の値に置き換えて保存します。

```dotenv
TELEGRAM_BOT_TOKEN=ここにBotFatherのtoken
TYPESAFE_API_KEY=ここにTypeSafeのAPIキー
JEV_MODEL=jev-latest
```

```powershell
python -X utf8 bot.py --check-config
python -X utf8 bot.py
```

`start.ps1`からも起動できます。Telegramでbotの個人チャットを開き、文章を送ると元のメッセージへの返信が届きます。停止は`Ctrl+C`。常時応答には、このプロセスとPCを起動したままにしてください。同じtokenのbotを複数起動しないでください。

`.env`は`bot.py`と同じフォルダーから読みます。OSの環境変数があればそちらを優先します。キーは返信やログに表示しません。

## 選択肢を変更する

botを停止し、`choices.json`のJSON配列を編集して再起動します。選択肢の説明は不要です。例えば3択にするなら：

```json
["了解です", "もう少し詳しく教えてください", "ありがとうございます"]
```

2〜255個の重複しない、空でない文字列を指定できます。各文字列はTelegramの本文上限4096文字以内にしてください。複数の単語や文、絵文字を選択肢として設定しても、選ばれた文字列を完全一致で送信します。

別のJSONファイルを使う場合：

```powershell
python -X utf8 bot.py --choices my-choices.json
```

選択肢は起動時に読み込むため、変更後には再起動が必要です。判定対象はそのメッセージ単体で、会話履歴は付けません。

## 動作確認

Telegramへ送らずJevだけ試す（TypeSafeキーが必要）：

```powershell
python -X utf8 bot.py --classify "今日は休みなので旅行に来ています。"
python -X utf8 bot.py --choices my-choices.json --classify "手伝ってくれてありがとう。"
```

APIキーなしで実装のテストを実行：

```powershell
python -X utf8 -m unittest discover -s tests -v
```

APIをモック化し、基準を含めないリクエスト、任意のn択への変更、設定した文字列だけの送信、範囲外の回答の拒否、障害時の動作、処理済み更新の扱いを確認します。Jevの意味判定やTelegram実接続はキー設定後に確認してください。

## 受信範囲と障害時

- 個人チャットとグループの新しいテキスト、画像等のキャプションに対応します。本文のない画像・音声・スタンプ、編集通知、bot自身や他のbotの投稿には返信しません。`/start`なども通常の文章としてJevで判定します。
- グループで一般の文章を受け取るには、BotFatherの`/setprivacy`を無効化し、必要ならbotをグループに追加し直します。フォーラムでは同じトピックに返信します。
- ロングポーリングを使い、公開URLやポート開放は不要です。既存のwebhookがある場合は起動を止めるので、新規botを使うか、既存の構成を確認して解除してください。
- Jevや受信APIの一時的な通信障害・429・5xxは再試行します。Jevで選べない間はその更新を未処理のまま保持し、代替の語やエラー文をTelegramへ送りません。認証エラー、選択肢以外の回答、壊れたレスポンスでは停止します。
- 送信APIは明示的な429だけを再試行します。タイムアウトや5xxは配信済みの可能性があるため自動再送せず処理済みとして記録します。この場合は返信が届かない可能性があります。送信401/409は停止し、429が続く場合は未処理のまま再試行します。
- `.state`に処理済み位置を保存します。通常の再起動では処理済み投稿を繰り返しませんが、送信直後・保存前の強制終了では重複の可能性があります。厳密な1回配信は保証しません。長時間停止するとTelegram側の更新保存期間を超える場合があります。
- 文章は判定のためTypeSafeへ送信され、API利用料は利用アカウントに適用されます。

## 公式資料

- [TypeSafe Quick start](https://docs.typesafe.ai/introduction/quickstart)
- [TypeSafe API reference：Choiceのnull説明と255個の上限](https://docs.typesafe.ai/api#choice)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram Bot FAQ：受信範囲](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get)
