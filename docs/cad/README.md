# Panasonic JU-256A046PCのケースとベゼル

- `JU-256A046PC-U-tray.step` は底と左右の壁を持つケースです。公称外形は109.6 × 148.0 × 30.6 mmです。
- `JU-256A046PC-front-bezel-clip-r8.step` はケースに合わせた前面カバーです。外形は109.6 × 30.6 mmで、爪を含む奥行きは14.6 mmです。
- `JU-256A046PC-front-bezel-r8-face-down.stl` は前面がビルドプレートに接する向きです。Bambu Lab P1S・0.4 mmノズル、PLA、0.20 mm積層でスライスできます。

`generate_bezel.py` はOCP（OpenCascade Pythonバインディング）を使ってベゼルを生成し、ケース、爪の受け穴、挿入口、イジェクト穴、LED窓を照合します。受け穴の位置は現物写真を基にした寸法で、個体差は試作で確認してください。
