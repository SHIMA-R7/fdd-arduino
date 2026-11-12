# Windows用フロッピー・ツール

1.44 MB FAT12（3.5インチ2HD）のファイル一覧、取り出し、書き込み、削除、イメージ保存・復元、通常／クイックフォーマットを操作できます。テスト用のFDで書き込みを確認してから使ってください。

## 実行

Python 3（Tkinterを含むWindows版）とpySerialを使います。ATmega328PのFDD基板をUSBシリアルで接続し、GUIで該当するCOMポートを選びます。FTDIのD2XXドライバーが利用できれば低遅延接続を使い、それ以外では通常のCOM接続を使います。

```powershell
py -3 -m pip install pyserial
py -3 windows/fdd_gui.py
py -3 -m unittest discover -s windows -p test_fdd.py
```

EXEを作る場合はリポジトリのルートで実行します。

```powershell
py -3 -m pip install pyinstaller
py -3 -m PyInstaller --onefile --windowed --name FDDTool-Fast windows/fdd_gui.py
```

## ファームウェア

`firmware/transfer_fast.cpp` はArduino Uno相当のATmega328P（5 V・16 MHz）用です。Arduino AVRコアと [ArduinoFDC](https://github.com/dhansel/ArduinoFDC) を組み合わせてビルドします。ArduinoFDCはGPL-3.0で配布されている別プロジェクトです。ISPで書き込むときは、既存のフラッシュ、ブートローダー、ヒューズを事前に読み出してください。

高速接続は500 kbpsから試し、データ照合に失敗した場合は低い速度を選びます。対応ファームウェアでは隣接2セクタをまとめて返し、通常フォーマットを10トラック単位で処理します。通常フォーマットはディスク全体を初期化します。クイックは標準FAT12の管理情報を初期化し、記録済みの不良クラスタを引き継ぎます。

## 実機結果

Panasonic JU-256A046PC、ATmega328P 16 MHz、FTDI低遅延接続で計測しました。72 KiBの読み取りは115,200 bpsで11.81秒、500 kbpsで7.01秒でした。1 Mbpsの通信照合はタイムアウトし、500 kbpsへ戻りました。通常フォーマットは76.6秒、256 KiBの書き込みと照合は55.8秒、全2,880セクタの読み取りは144.6秒でした。取り出した256 KiBは元データと一致しました。
