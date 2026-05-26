import hashlib
from dataclasses import dataclass
from typing import Optional

@dataclass
class PQKeyPair:
    alg: str
    sk: bytes
    pk: bytes
    key_id: str

    @staticmethod
    def derive_key_id(pk: bytes, alg: str = "sphincs+") -> str:
        return hashlib.sha256(pk + alg.encode()).hexdigest()[:16]

    @classmethod
    def generate(cls, alg: str = "sphincs+") -> "PQKeyPair":
        sk = hashlib.sha256(b"pq-seed-" + alg.encode()).digest() * 4
        pk = hashlib.sha256(sk).digest()
        key_id = cls.derive_key_id(pk, alg)
        return cls(alg=alg, sk=sk, pk=pk, key_id=key_id)

    def to_dict(self) -> dict:
        return {"alg": self.alg, "key_id": self.key_id, "pk": self.pk.hex()}

    @classmethod
    def from_dict(cls, data: dict) -> "PQKeyPair":
        return cls(alg=data["alg"], sk=b"", pk=bytes.fromhex(data["pk"]), key_id=data.get("key_id", ""))

    def save(self, path: str):
        import json
        with open(path, "w") as f:
            json.dump({"alg": self.alg, "key_id": self.key_id, "pk": self.pk.hex()}, f)

    @classmethod
    def load(cls, path: str) -> Optional["PQKeyPair"]:
        import json
        try:
            with open(path) as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None
