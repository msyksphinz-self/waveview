# waveview: AI と共有するブラウザ波形ビューワ

`tools/waveview.py` は GTKWave / Verdi と同じ感覚で操作できる波形ビューワです。
ただし画面はブラウザ、データはリモートのビルドサーバ上にあり、
**表示状態(信号リスト・カーソル・マーカ・ズーム・メモ)をサーバ側で 1 つ持つ**ので、
人とAIエージェントが同じ画面を同時に操作できます。

```
 リモートサーバ (ダンプがある場所)                 手元の PC
 ┌──────────────────────────────┐             ┌─────────────────┐
 │ tools/waveview.py serve w.fst│◀── HTTP ───▶│ ブラウザ          │
 │   共有ビュー状態               │  (ssh -L /  │ GTKWave 風の画面  │
 │   ▲            ▲             │   VS Code)  └─────────────────┘
 │   │ CLI        │ ファイル監視  │
 │ AI エージェント  シミュレータが   │
 │ (Claude Code)  w.fst を再生成   │
 └──────────────────────────────┘
```

## 起動

```sh
make WAVE=fst build-top
./obj_dir_top/tb_scariv_top ... +wave=w.fst +wave_from=1000 +wave_to=3000
tools/waveview.py serve w.fst            # URL(トークン付き)が表示される
```

- リモートなら `ssh -L 8765:localhost:8765 <host>` でポート転送して URL を開く。
  VS Code Remote は自動で転送する。X11 も GTKWave のインストールも不要
- Python 3 の標準ライブラリだけで動く。FST は Verilator 同梱の fstapi で読む
  (wave2img.py と同じ仕組み)
- 127.0.0.1 にだけ bind し、全リクエストにランダムトークンを要求する。
  共有サーバでも他ユーザーからは見えない
- シミュレーションをやり直してダンプが更新されると、表示状態を保ったまま自動で再読み込み
- 表示状態は `<dump>.waveview.json` に保存され、次回起動時に復元される
- `--gtkw save.gtkw` で GTKWave の保存ファイルを読み込める。ツールバーの `.gtkw` で書き出し

## 画面と操作 (GTKWave / Verdi nWave 準拠)

左に階層ツリーと信号リスト (GTKWave の SST)、中央に信号名と値 (カーソル位置)、右に波形。

| 操作 | 動作 |
|---|---|
| クリック | カーソル (行も選択) |
| Shift+クリック / 中ボタン | マーカ。カーソルとの差を時間とサイクル数で表示 |
| ドラッグ | 範囲ズーム |
| Ctrl+ホイール / Shift+ホイール / ホイール | ズーム / 時間方向スクロール / 行スクロール |
| `f` `z` `Z` | 全体表示 / ズームアウト / ズームイン |
| `←` `→` | 選択信号の前 / 次の変化点へ |
| `/` または Ctrl+F, `n` `N` | 条件式サーチ (例 `commit_valid && pc == 0x80000040`), 次 / 前 |
| `c` `m` | カーソルを中央に / カーソル位置にマーカ |
| `↑` `↓`, Alt+`↑` `↓`, Delete | 行選択 / 行の移動 / 削除 |
| `h` `d` `s` `b` `o` | 基数 hex / dec / signed / bin / oct (右クリックメニューにもある) |
| `a` | Send to AI |
| `?` | ショートカット一覧 |

信号はツリーでスコープを選び、リストからダブルクリックかドラッグで追加。
フィルタ欄で Enter を押すとヒットした信号をまとめて追加。時間軸にはクロック
立ち上がりごとのグリッドとサイクル番号が出る。

## AI エージェントとの連携

エージェント (や任意のシェル) から CLI で同じ画面を操作できる。実行中のサーバは
`~/.cache/scariv/waveview/` から自動で見つけるので、ポートやトークンの指定は要らない。

```sh
tools/waveview.py look            # 今ユーザーが見ている画面をテキストで (値・カーソル・マーカ)
tools/waveview.py add '*u_rob.commit_*'
tools/waveview.py goto --cycle 1234 --span 40
tools/waveview.py annotate --cycle 1230 "mispredict"
tools/waveview.py note "flush は 1230 の BRU mispredict が原因"
tools/waveview.py find 'rose(flush_valid)'
tools/waveview.py inbox --wait 600   # ユーザーの「Send to AI」を待つ
```

- エージェントの操作は即座にブラウザに反映され、ステータスバーに `agent: goto` のように出る
- エージェントのメモは画面上部のバナーに、ラベルは時間軸の旗として表示される
- 「Send to AI」ボタン (または右クリック → Ask AI) で、メッセージと**その時の画面状態**
  がエージェントに届く。エージェントが `inbox --wait` で待っている間はステータスバーに
  「AI listening」と表示される
- 深掘りのテキスト問い合わせは `tools/wavequery.py` (table / find / changes) を使う。
  Claude Code 向けの手順は `.claude/skills/wave-debug/SKILL.md`

HTTP API (`/api/cmd`, `/api/snapshot`, `/api/wave`, `/api/events` など) も
そのまま使えるので、他のエージェントやスクリプトからも同じ操作ができる。

## テスト

```sh
tools/waveview.py selftest
tools/wavequery.py --selftest
```
