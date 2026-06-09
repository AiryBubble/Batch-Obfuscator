バッチを素早く難読化できます。
# 基本的な使用方法
## シンプルな実行
```
python batch_obfuscator.py script.bat
```
## オプション付き実行
### 難読化強度を上げる（VAR変数増加）
```
python batch_obfuscator.py script.bat --keys 10
```
### 詳細ログ表示
```
python batch_obfuscator.py script.bat -v
```
### 組み合わせ
```
python batch_obfuscator.py script.bat --keys 15 -v
```
