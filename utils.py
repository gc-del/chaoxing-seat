# -*- coding: utf-8 -*-
"""本地加密与签名工具（超星接口逆向算法实现）"""
import base64
import hashlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

AES_KEY_IV = b"u2oh6Vu^HWe4_AES"

DEFAULT_SALT = "%sd`~7^/>N4!Q#){''"


def aes_encrypt(text: str) -> str:
    """学习通登录参数加密：AES-128-CBC，key 与 iv 相同，PKCS7 填充，base64 输出"""
    padder = PKCS7(128).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(AES_KEY_IV), modes.CBC(AES_KEY_IV)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def enc_sign(params: dict, salt: str = DEFAULT_SALT) -> str:
    """超星接口 enc 签名：参数按 key 字典序拼 [k=v]，末尾追加 [盐值]，整体 MD5"""
    seq = "".join(f"[{k}={params[k]}]" for k in sorted(params))
    seq += f"[{salt}]"
    return md5_hex(seq)
