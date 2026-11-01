"""
One MongoClient per process for the platform libraries that need a collection
(chat_cache, privacy_mode, skill_store). A MongoClient keeps its own pool and
background monitoring connections open, so every module creating its own client
multiplies the connections each agent process holds - and mongod refuses new
ones once its total is reached (confirmed live: "too many open connections"
with ~100 established across the agents).

maxPoolSize is small on purpose: these callers do short, infrequent reads and
writes, so a handful of connections is plenty.
"""
import os
from typing import Optional

from pymongo import MongoClient

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')

_client: Optional[MongoClient] = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000, maxPoolSize=5)
    return _client


def database():
    return get_client()[MONGO_DB_NAME]
