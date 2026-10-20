"""生成 assets/icon.ico（构建时调用）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yingsay.ui import make_icon_image  # noqa: E402

os.makedirs("assets", exist_ok=True)
img = make_icon_image().resize((256, 256))
img.save("assets/icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("assets/icon.ico")
