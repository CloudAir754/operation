"""OpenSSH 公钥格式校验、指纹和批次去重。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import asdict, dataclass
from typing import Any

ALLOWED_KEY_TYPES = frozenset(
    {
        "ssh-rsa",
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


@dataclass(frozen=True, slots=True)
class PublicKey:
    """表示已校验且不含 authorized_keys 选项的单条公钥。"""

    key_type: str
    encoded: str
    comment: str
    fingerprint: str

    def normalized(self) -> str:
        """返回单空格分隔的标准公钥行。"""

        return " ".join(value for value in (self.key_type, self.encoded, self.comment) if value)

    def public(self, duplicate: bool = False) -> dict[str, Any]:
        """返回预览字段，不回传完整密钥材料。"""

        result = asdict(self)
        result.pop("encoded")
        result["duplicate"] = duplicate
        result["comment"] = self.comment or None
        return result


def _embedded_key_type(decoded: bytes) -> str:
    """读取 SSH 二进制 blob 的首个 length-prefixed 类型。"""

    if len(decoded) < 4:
        raise ValueError("公钥 blob 过短")
    length = struct.unpack(">I", decoded[:4])[0]
    if length <= 0 or 4 + length > len(decoded):
        raise ValueError("公钥 blob 长度无效")
    try:
        return decoded[4 : 4 + length].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("公钥 blob 类型不是 ASCII") from exc


def parse_public_key(line: str) -> PublicKey:
    """解析单条无选项 OpenSSH 公钥，拒绝私钥和强制命令等前缀。"""

    value = line.strip()
    if "PRIVATE KEY" in value:
        raise ValueError("不接受私钥")
    fields = value.split(None, 2)
    if len(fields) < 2:
        raise ValueError("公钥必须包含类型和 base64 数据")
    key_type, encoded = fields[:2]
    if key_type not in ALLOWED_KEY_TYPES:
        if any(allowed in value for allowed in ALLOWED_KEY_TYPES):
            raise ValueError("不接受 authorized_keys 选项或前缀")
        raise ValueError(f"不支持的公钥类型 {key_type}")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("公钥 base64 数据无效") from exc
    if _embedded_key_type(decoded) != key_type:
        raise ValueError("公钥声明类型与 blob 类型不一致")
    fingerprint = base64.b64encode(hashlib.sha256(decoded).digest()).decode().rstrip("=")
    comment = fields[2].strip() if len(fields) == 3 else ""
    if any(character in comment for character in "\r\n\x00"):
        raise ValueError("公钥注释包含控制字符")
    return PublicKey(key_type, encoded, comment, f"SHA256:{fingerprint}")


def preview_public_keys(
    value: str, existing_fingerprints: set[str] | None = None
) -> dict[str, Any]:
    """逐行解析并按指纹标记当前批次及已有公钥重复。"""

    existing = existing_fingerprints or set()
    seen: set[str] = set()
    rows = []
    for source_row, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        try:
            key = parse_public_key(line)
        except ValueError as exc:
            rows.append(
                {
                    "source_row": source_row,
                    "status": "invalid",
                    "error": str(exc),
                    "key_type": None,
                    "comment": None,
                    "fingerprint": None,
                    "duplicate": False,
                }
            )
            continue
        duplicate = key.fingerprint in seen or key.fingerprint in existing
        seen.add(key.fingerprint)
        rows.append(
            {
                "source_row": source_row,
                "status": "duplicate" if duplicate else "valid",
                "error": None,
                **key.public(duplicate),
            }
        )
    return {
        "counts": {
            "valid": sum(row["status"] == "valid" for row in rows),
            "duplicate": sum(row["status"] == "duplicate" for row in rows),
            "invalid": sum(row["status"] == "invalid" for row in rows),
        },
        "rows": rows,
    }
