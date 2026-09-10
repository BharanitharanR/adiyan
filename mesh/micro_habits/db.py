import os, uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pymongo import MongoClient
from pymongo.collection import Collection

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
COLLECTION_NAME = 'micro_habit_entries'

_client: Optional[MongoClient] = None


def connect() -> Collection:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client[MONGO_DB_NAME][COLLECTION_NAME]


def add_entry(conn: Collection, entry: str, mood: Optional[str] = None) -> Dict[str, Any]:
    doc_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    conn.insert_one({"_id": doc_id, "entry": entry, "mood": mood, "created_at": created_at})
    return {"id": doc_id, "created_at": created_at}


def list_entries(conn: Collection, limit: int = 20) -> list:
    return [{**d, "id": d.pop("_id")} for d in conn.find({}).sort("created_at", -1).limit(limit)]